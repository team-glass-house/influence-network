"""Helpers for clustering and anomaly detection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler


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
