"""Helpers for clustering and anomaly detection."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN
from sklearn.compose import ColumnTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.ensemble import IsolationForest
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.neighbors import NearestNeighbors
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


TRANSPARENCY_COMPONENTS = (
    "board_members",
    "volunteers",
    "website_words",
    "related_to_527s",
    "related_to_c3s",
    "political_expenses",
    "total_salaries",
    "unrestricted_net_assets",
    "fundraising_expenses",
)
MODEL_FEATURE_VERSION = "modeling-v1"
PLACEHOLDER_EINS = frozenset({"000000000", "111111111", "999999999"})


@dataclass
class ClassificationResult:
    """Classifier output."""

    model: Pipeline
    metrics: dict[str, float]
    predictions: pd.DataFrame
    train_rows: int
    test_rows: int
    feature_columns: tuple[str, ...]
    split_strategy: str
    test_year: int | None


@dataclass
class NetworkAnalysisResult:
    """Graph summary and node features."""

    graph: nx.Graph
    node_features: pd.DataFrame
    summary: dict[str, int | float]


def _score_run_clause(run_id: str | None) -> tuple[str, tuple[Any, ...]]:
    if run_id is not None:
        return "s.run_id = ?", (run_id,)
    return (
        "s.run_id = ("
        "SELECT run_id FROM transparency_index_scores "
        "ORDER BY generated_at DESC LIMIT 1"
        ")",
        (),
    )


def load_modeling_features(
    db_path: Path | str,
    run_id: str | None = None,
    complete_only: bool = True,
    min_observed_components: int | None = None,
) -> pd.DataFrame:
    """Load scores with covariates and missingness flags."""
    if min_observed_components is not None:
        if not 1 <= min_observed_components <= len(TRANSPARENCY_COMPONENTS):
            raise ValueError("min_observed_components must be between 1 and 9")
        if complete_only:
            raise ValueError("choose complete_only or min_observed_components")
    threshold = 9 if complete_only else min_observed_components
    run_clause, run_params = _score_run_clause(run_id)
    query = f"""
    WITH disclosed AS (
        SELECT filing_id,
               CASE
                   WHEN MAX(
                       total_exempt_function_expend_amt IS NOT NULL
                       OR total_lobbying_expend_amt IS NOT NULL
                       OR total_lobbying_expenditures_amt IS NOT NULL
                       OR fees_for_services_lobbying_amt IS NOT NULL
                   ) = 0 THEN NULL
                   ELSE COALESCE(MAX(total_exempt_function_expend_amt), 0)
                      + COALESCE(
                          MAX(total_lobbying_expend_amt),
                          MAX(total_lobbying_expenditures_amt),
                          MAX(fees_for_services_lobbying_amt),
                          0
                        )
               END AS disclosed_political_expenses
        FROM irs990_filing_lobbying
        GROUP BY filing_id
    )
    SELECT s.*,
           f.filer_name, f.mission, f.form_type,
           f.total_revenue, f.total_expenses, f.total_assets,
           f.grants_and_contributions, f.voting_members_governing_body,
           f.voting_members_independent,
           f.total_volunteers, f.website,
           f.total_salaries AS raw_total_salaries,
           f.unrestricted_net_assets_eoy AS raw_unrestricted_net_assets_eoy,
           f.fundraising_expenses AS raw_fundraising_expenses,
           f.political_activity_flag,
           disclosed.disclosed_political_expenses,
           o.status AS website_status, o.http_status AS website_http_status,
           o.capped_word_count AS website_capped_word_count,
           o.pages_crawled AS website_pages_crawled,
           o.robots_allowed AS website_robots_allowed,
           o.crawler_version AS website_crawler_version,
           o.policy_hash AS website_policy_hash,
           o.retrieved_at AS website_retrieved_at
    FROM transparency_index_scores AS s
    JOIN irs990_filings AS f ON f.filing_id = s.filing_id
    LEFT JOIN disclosed ON disclosed.filing_id = s.filing_id
    LEFT JOIN transparency_website_observations AS o
      ON o.observation_id = s.website_observation_id
    WHERE {run_clause}
      AND (? IS NULL OR s.observed_components >= ?)
    ORDER BY s.ein, s.tax_year, s.filing_id
    """
    with sqlite3.connect(db_path) as connection:
        frame = pd.read_sql_query(
            query,
            connection,
            params=(*run_params, threshold, threshold),
        )
    for component in TRANSPARENCY_COMPONENTS:
        frame[f"{component}_missing"] = frame[component].isna().astype("int8")
    frame["score_available_fraction"] = (
        frame["observed_components"] / len(TRANSPARENCY_COMPONENTS)
    )
    frame["score_is_partial"] = (frame["complete"] == 0).astype("int8")
    frame["website_observed"] = frame["website_observation_id"].notna().astype("int8")
    for column in ("total_revenue", "total_expenses", "total_assets"):
        values = pd.to_numeric(frame[column], errors="coerce").clip(lower=0)
        frame[f"log_{column}"] = np.log1p(values)
    valid_target = (
        frame["disclosed_political_expenses"].notna()
        & (frame["disclosed_political_expenses"] >= 0)
    )
    frame["political_activity_target"] = pd.Series(pd.NA, index=frame.index, dtype="Int64")
    frame.loc[valid_target, "political_activity_target"] = (
        frame.loc[valid_target, "disclosed_political_expenses"] > 0
    ).astype("int8")
    frame["model_feature_version"] = MODEL_FEATURE_VERSION
    return frame


def transparency_coverage_summary(features: pd.DataFrame) -> pd.DataFrame:
    """Summarize score coverage."""
    rows = []
    total = len(features)
    for threshold in (7, 8, 9):
        count = int((features["observed_components"] >= threshold).sum())
        rows.append({
            "threshold": threshold,
            "rows": count,
            "share": count / total if total else 0.0,
        })
    for component in TRANSPARENCY_COMPONENTS:
        observed = int(features[component].notna().sum())
        rows.append({
            "threshold": component,
            "rows": observed,
            "share": observed / total if total else 0.0,
        })
    return pd.DataFrame(rows)


def fit_political_activity_classifier(
    features: pd.DataFrame,
    *,
    test_size: float = 0.25,
    random_state: int = 42,
    split_strategy: str = "grouped",
    test_year: int | None = None,
    include_structured_features: bool = True,
    include_missingness: bool = True,
) -> ClassificationResult:
    """Fit a classifier for disclosed activity."""
    if split_strategy not in {"grouped", "temporal"}:
        raise ValueError("split_strategy must be grouped or temporal")
    if split_strategy == "temporal" and test_year is None:
        raise ValueError("test_year is required for temporal validation")
    required = {"ein", "mission"}
    missing = required - set(features.columns)
    if missing:
        raise ValueError(f"Missing classifier columns: {sorted(missing)}")
    if "political_activity_target" not in features:
        if "disclosed_political_expenses" not in features:
            raise ValueError(
                "features require political_activity_target or "
                "disclosed_political_expenses"
            )
        data = features.copy()
        valid_target = (
            data["disclosed_political_expenses"].notna()
            & (data["disclosed_political_expenses"] >= 0)
        )
        data["political_activity_target"] = pd.Series(
            pd.NA, index=data.index, dtype="Int64"
        )
        data.loc[valid_target, "political_activity_target"] = (
            data.loc[valid_target, "disclosed_political_expenses"] > 0
        ).astype("int8")
        data = data.loc[data["political_activity_target"].notna()].copy()
    else:
        data = features.loc[features["political_activity_target"].notna()].copy()
    if data.empty or data["political_activity_target"].nunique() < 2:
        raise ValueError("classifier requires two observed target classes")
    data["mission"] = data["mission"].fillna("").astype(str)
    numeric: list[str] = []
    if include_structured_features:
        numeric.extend([
            column for column in (
                "log_total_revenue",
                "log_total_expenses",
                "log_total_assets",
            )
            if column in data
        ])
    if include_missingness:
        numeric.extend([
            f"{component}_missing"
            for component in TRANSPARENCY_COMPONENTS
            if component != "political_expenses"
            and f"{component}_missing" in data
        ])
    if split_strategy == "grouped":
        splitter = GroupShuffleSplit(
            n_splits=1, test_size=test_size, random_state=random_state
        )
        train_idx, test_idx = next(splitter.split(data, groups=data["ein"]))
        train = data.iloc[train_idx]
        test = data.iloc[test_idx]
    else:
        if "tax_year" not in data:
            raise ValueError("temporal validation requires tax_year")
        test = data.loc[data["tax_year"] >= test_year].copy()
        train = data.loc[data["tax_year"] < test_year].copy()
        train = train.loc[~train["ein"].isin(test["ein"])]
        if train.empty or test.empty:
            raise ValueError("temporal validation produced an empty split")
    if train["political_activity_target"].nunique() < 2:
        raise ValueError("training split contains one target class")
    preprocessor = ColumnTransformer(
        transformers=[
            (
                "mission",
                TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1),
                "mission",
            ),
            (
                "numeric",
                Pipeline([
                    ("impute", SimpleImputer(strategy="median", add_indicator=True)),
                    ("scale", StandardScaler(with_mean=False)),
                ]),
                numeric,
            ),
        ]
    )
    model = Pipeline([
        ("features", preprocessor),
        ("classifier", LogisticRegression(
            max_iter=1000, class_weight="balanced", random_state=random_state
        )),
    ])
    target_name = "political_activity_target"
    model.fit(train, train[target_name].astype(int))
    probabilities = model.predict_proba(test)[:, 1]
    predictions = pd.DataFrame({
        "ein": test["ein"].to_numpy(),
        "filing_id": test.get("filing_id", pd.Series(test.index)).to_numpy(),
        "tax_year": test.get("tax_year", pd.Series(pd.NA, index=test.index)).to_numpy(),
        "observed": test[target_name].astype(int).to_numpy(),
        "predicted_probability": probabilities,
        "predicted": (probabilities >= 0.5).astype("int8"),
    })
    observed = predictions["observed"]
    predicted = predictions["predicted"]
    metrics = {
        "roc_auc": float(roc_auc_score(observed, probabilities)),
        "average_precision": float(average_precision_score(observed, probabilities)),
        "brier_score": float(brier_score_loss(observed, probabilities)),
        "precision_at_0.5": float(precision_score(observed, predicted, zero_division=0)),
        "recall_at_0.5": float(recall_score(observed, predicted, zero_division=0)),
        "test_prevalence": float(observed.mean()),
        "average_precision_lift": float(
            average_precision_score(observed, probabilities) / observed.mean()
        ) if observed.mean() > 0 else float("nan"),
    }
    return ClassificationResult(
        model=model,
        metrics=metrics,
        predictions=predictions,
        train_rows=len(train),
        test_rows=len(test),
        feature_columns=tuple(numeric) + ("mission",),
        split_strategy=split_strategy,
        test_year=test_year,
    )


def analyze_organization_network(
    db_path: Path | str,
    *,
    max_edges_per_type: int = 250,
) -> NetworkAnalysisResult:
    """Build a bounded grant and related-organization graph."""
    if max_edges_per_type < 1:
        raise ValueError("max_edges_per_type must be positive")
    with sqlite3.connect(db_path) as connection:
        grants = pd.read_sql_query(
            """
            SELECT source_ein, target_ein, edge_type, amount, supporting_rows
            FROM grant_network_edges
            WHERE source_ein IS NOT NULL AND target_ein IS NOT NULL
            ORDER BY amount DESC LIMIT ?
            """,
            connection,
            params=(max_edges_per_type,),
        )
        related = pd.read_sql_query(
            """
            SELECT source_ein, target_ein, edge_type,
                   CAST(NULL AS REAL) AS amount, supporting_rows
            FROM related_organization_edges
            WHERE source_ein IS NOT NULL AND target_ein IS NOT NULL
            ORDER BY supporting_rows DESC LIMIT ?
            """,
            connection,
            params=(max_edges_per_type,),
        )
    edge_records = grants.to_dict("records") + related.to_dict("records")
    edges = pd.DataFrame.from_records(
        edge_records,
        columns=["source_ein", "target_ein", "edge_type", "amount", "supporting_rows"],
    )
    excluded_placeholder_edges = 0
    if not edges.empty:
        valid_edges = (
            ~edges["source_ein"].astype(str).isin(PLACEHOLDER_EINS)
            & ~edges["target_ein"].astype(str).isin(PLACEHOLDER_EINS)
        )
        excluded_placeholder_edges = int((~valid_edges).sum())
        edges = edges.loc[valid_edges].copy()
    graph = nx.Graph()
    if not edges.empty:
        for row in edges.itertuples(index=False):
            grant_amount = (
                float(row.amount)
                if row.edge_type == "grant"
                and pd.notna(row.amount)
                and float(row.amount) > 0
                else 0.0
            )
            related_support = (
                int(row.supporting_rows or 0)
                if row.edge_type == "related_organization"
                else 0
            )
            if graph.has_edge(row.source_ein, row.target_ein):
                edge = graph[row.source_ein][row.target_ein]
                edge["grant_amount"] += grant_amount
                edge["related_supporting_rows"] += related_support
                edge["edge_types"].add(row.edge_type)
            else:
                graph.add_edge(
                    row.source_ein,
                    row.target_ein,
                    grant_amount=grant_amount,
                    related_supporting_rows=related_support,
                    edge_types={row.edge_type},
                )
    if graph.number_of_nodes():
        degree = dict(graph.degree())
        grant_degree = {
            node: sum(
                1 for _, _, data in graph.edges(node, data=True)
                if "grant" in data["edge_types"]
            )
            for node in graph
        }
        grant_amount = {
            node: sum(data["grant_amount"] for _, _, data in graph.edges(node, data=True))
            for node in graph
        }
        related_degree = {
            node: sum(
                1 for _, _, data in graph.edges(node, data=True)
                if "related_organization" in data["edge_types"]
            )
            for node in graph
        }
        related_support = {
            node: sum(
                data["related_supporting_rows"]
                for _, _, data in graph.edges(node, data=True)
            )
            for node in graph
        }
        betweenness = nx.betweenness_centrality(graph, normalized=True)
        pagerank = nx.pagerank(graph)
        communities = list(
            nx.community.greedy_modularity_communities(graph)
        )
    else:
        degree = grant_degree = grant_amount = related_degree = related_support = {}
        betweenness = pagerank = {}
        communities = []
    community_by_node = {
        node: community_id
        for community_id, members in enumerate(communities)
        for node in members
    }
    node_features = pd.DataFrame({
        "ein": list(graph.nodes),
        "degree": [degree[node] for node in graph.nodes],
        "grant_degree": [grant_degree[node] for node in graph.nodes],
        "grant_amount": [grant_amount[node] for node in graph.nodes],
        "grant_log_strength": [
            np.log1p(grant_amount[node]) for node in graph.nodes
        ],
        "related_degree": [related_degree[node] for node in graph.nodes],
        "related_supporting_rows": [
            related_support[node] for node in graph.nodes
        ],
        "betweenness": [betweenness[node] for node in graph.nodes],
        "pagerank": [pagerank[node] for node in graph.nodes],
        "community": [community_by_node.get(node, -1) for node in graph.nodes],
    }).sort_values(["grant_amount", "degree"], ascending=False)
    summary: dict[str, int | float] = {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "communities": len(communities),
        "density": float(nx.density(graph)) if graph.number_of_nodes() > 1 else 0.0,
        "connected_components": nx.number_connected_components(graph),
        "largest_component": max(
            (len(component) for component in nx.connected_components(graph)),
            default=0,
        ),
        "grant_edges": int(
            sum("grant" in data["edge_types"] for _, _, data in graph.edges(data=True))
        ),
        "related_edges": int(sum(
            "related_organization" in data["edge_types"]
            for _, _, data in graph.edges(data=True)
        )),
        "excluded_placeholder_edges": excluded_placeholder_edges,
    }
    return NetworkAnalysisResult(graph, node_features, summary)


@dataclass
class DbscanResult:
    """Results from a DBSCAN run."""

    labels: np.ndarray
    model: DBSCAN
    transformed_features: np.ndarray
    feature_names: tuple[str, ...]
    n_clusters: int
    noise_count: int


@dataclass
class IsolationForestResult:
    """Results from an Isolation Forest run."""

    labels: np.ndarray
    anomaly_scores: np.ndarray
    model: IsolationForest
    transformed_features: np.ndarray
    feature_names: tuple[str, ...]
    anomaly_count: int


def _prepare_features(
    features: Any,
    *,
    scale: bool,
    log_transform: bool,
) -> tuple[np.ndarray, tuple[str, ...]]:
    """Validate and optionally transform feature values."""
    feature_names: tuple[str, ...]
    if hasattr(features, "columns"):
        feature_names = tuple(str(column) for column in features.columns)
    else:
        feature_names = ()
    try:
        matrix = np.asarray(features, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("features must contain only numeric values") from exc
    if matrix.ndim != 2 or matrix.shape[0] == 0 or matrix.shape[1] == 0:
        raise ValueError("features must be a non-empty two-dimensional matrix")
    if not np.isfinite(matrix).all():
        raise ValueError("features must not contain NaN or infinite values")
    if log_transform:
        if (matrix < 0).any():
            raise ValueError("log_transform requires non-negative features")
        matrix = np.log1p(matrix)
    if scale:
        matrix = StandardScaler().fit_transform(matrix)
    return matrix, feature_names or tuple(f"feature_{i}" for i in range(matrix.shape[1]))


def dbscan_k_distances(
    features: Any,
    *,
    min_samples: int = 5,
    scale: bool = True,
    log_transform: bool = False,
    metric: str = "euclidean",
) -> np.ndarray:
    """Return sorted nearest-neighbor distances."""
    if min_samples < 2:
        raise ValueError("min_samples must be at least 2")
    matrix, _ = _prepare_features(
        features, scale=scale, log_transform=log_transform
    )
    neighbors = NearestNeighbors(
        n_neighbors=min_samples, metric=metric
    ).fit(matrix)
    distances, _ = neighbors.kneighbors(matrix)
    return np.sort(distances[:, -1])


def dbscan_clusters(
    features: Any,
    *,
    eps: float = 0.8,
    min_samples: int = 5,
    scale: bool = True,
    log_transform: bool = False,
    metric: str = "euclidean",
    **kwargs: Any,
) -> DbscanResult:
    """Run DBSCAN on feature rows."""
    if eps <= 0:
        raise ValueError("eps must be greater than 0")
    if min_samples < 2:
        raise ValueError("min_samples must be at least 2")
    matrix, feature_names = _prepare_features(
        features, scale=scale, log_transform=log_transform
    )
    model = DBSCAN(
        eps=eps, min_samples=min_samples, metric=metric, **kwargs
    ).fit(matrix)
    labels = model.labels_
    return DbscanResult(
        labels=labels,
        model=model,
        transformed_features=matrix,
        feature_names=feature_names,
        n_clusters=len(set(labels)) - (1 if -1 in labels else 0),
        noise_count=int(np.count_nonzero(labels == -1)),
    )


def isolation_forest_anomalies(
    features: Any,
    *,
    contamination: str | float = "auto",
    n_estimators: int = 300,
    random_state: int = 42,
    scale: bool = False,
    log_transform: bool = False,
    **kwargs: Any,
) -> IsolationForestResult:
    """Run Isolation Forest and return anomaly scores."""
    if isinstance(contamination, float) and not 0 < contamination <= 0.5:
        raise ValueError("contamination must be in (0, 0.5]")
    if n_estimators < 1:
        raise ValueError("n_estimators must be positive")
    matrix, feature_names = _prepare_features(
        features, scale=scale, log_transform=log_transform
    )
    model = IsolationForest(
        contamination=contamination,
        n_estimators=n_estimators,
        random_state=random_state,
        **kwargs,
    ).fit(matrix)
    labels = model.predict(matrix)
    scores = -model.decision_function(matrix)
    return IsolationForestResult(
        labels=labels,
        anomaly_scores=scores,
        model=model,
        transformed_features=matrix,
        feature_names=feature_names,
        anomaly_count=int(np.count_nonzero(labels == -1)),
    )
