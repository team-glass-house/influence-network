from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .config import settings
from .db import init_db
from .s3_manager import df_to_s3
from .website_crawler import normalize_url

INDEX_VERSION = "irvin-9-v2"
WEBSITE_WORD_CAP = 100_000
VOTING_MEMBER_CAP = 25
UNRESTRICTED_ASSET_EXPENSE_MULTIPLE = 3
SCORE_COLUMNS = (
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

SOURCE_QUERY = """
WITH related_c3 AS (
    SELECT
        r.filing_id,
        COUNT(DISTINCT r.ein) AS num_c3s,
        MAX(f2.voting_members_governing_body) AS max_board_size
    FROM irs990_filing_related_orgs r
    JOIN irs990_filings f1 ON f1.filing_id = r.filing_id
    LEFT JOIN irs990_filings f2
      ON f2.ein = r.ein
     AND f2.tax_year = f1.tax_year
     AND f2.exempt_organization_type = '501(c)(3)'
    WHERE r.ein IS NOT NULL
      AND r.entity_type LIKE '%501%(3)%'
    GROUP BY r.filing_id
),
related_527 AS (
    SELECT filing_id, COUNT(*) AS num_527s
    FROM (
        SELECT filing_id
        FROM irs990_filing_related_orgs
        WHERE entity_type LIKE '%527%'
        UNION ALL
        SELECT filing_id
        FROM irs990_filing_527_orgs
    )
    GROUP BY filing_id
),
political AS (
    SELECT
        filing_id,
        CASE
            WHEN MAX(
                total_exempt_function_expend_amt IS NOT NULL
                OR fees_for_services_lobbying_amt IS NOT NULL
            ) = 0 THEN NULL
            ELSE COALESCE(MAX(total_exempt_function_expend_amt), 0)
                + COALESCE(MAX(fees_for_services_lobbying_amt), 0)
        END AS political_expenses
    FROM irs990_filing_lobbying
    GROUP BY filing_id
)
SELECT
    f.filing_id,
    f.ein,
    f.tax_year,
    f.filer_name,
    f.total_revenue,
    f.total_expenses,
    f.total_assets,
    f.voting_members_governing_body,
    f.total_volunteers,
    f.website,
    f.total_salaries,
    f.unrestricted_net_assets_eoy,
    f.fundraising_expenses,
    f.grants_and_contributions,
    COALESCE(r527.num_527s, 0) AS num_527s,
    COALESCE(c3.num_c3s, 0) AS num_c3s,
    c3.max_board_size,
    political.political_expenses
FROM irs990_filings f
LEFT JOIN related_527 r527 ON r527.filing_id = f.filing_id
LEFT JOIN related_c3 c3 ON c3.filing_id = f.filing_id
LEFT JOIN political ON political.filing_id = f.filing_id
WHERE f.form_type <> '990T'
  AND f.exempt_organization_type = '501(c)(4)'
"""

RELATED_C3_WEBSITE_QUERY = """
WITH c3_matches AS (
    SELECT
        r.filing_id,
        MIN(r.ein) AS source_ein,
        LOWER(TRIM(f2.website)) AS submitted_url
    FROM irs990_filing_related_orgs r
    JOIN irs990_filings f1 ON f1.filing_id = r.filing_id
    JOIN irs990_filings f2
      ON f2.ein = r.ein
     AND f2.tax_year = f1.tax_year
     AND f2.exempt_organization_type = '501(c)(3)'
    WHERE r.ein IS NOT NULL
      AND r.entity_type LIKE '%501%(3)%'
      AND f2.website IS NOT NULL
      AND f1.form_type <> '990T'
      AND f1.exempt_organization_type = '501(c)(4)'
    GROUP BY r.filing_id, LOWER(TRIM(f2.website))
)
SELECT filing_id, 'c3' AS source_type, source_ein, submitted_url
FROM c3_matches
"""


def _connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else settings.db_path
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def get_transparency_index_data(
    db_path: Path | None = None,
    store: bool = False,
) -> pd.DataFrame:
    init_db(db_path)
    with _connection(db_path) as connection:
        frame = pd.read_sql_query(SOURCE_QUERY, connection)
    if store:
        df_to_s3(frame, "parquet/transparency_source_data.parquet")
    return frame


def get_website_candidates(db_path: Path | None = None) -> pd.DataFrame:
    init_db(db_path)
    with _connection(db_path) as connection:
        c4 = pd.read_sql_query(
            """
            SELECT filing_id, 'c4' AS source_type, ein AS source_ein, website AS submitted_url
            FROM irs990_filings
            WHERE form_type <> '990T'
              AND exempt_organization_type = '501(c)(4)'
              AND website IS NOT NULL
            """,
            connection,
        )
        c3 = pd.read_sql_query(RELATED_C3_WEBSITE_QUERY, connection)
    candidates = pd.concat([c4, c3], ignore_index=True)
    if candidates.empty:
        candidates["normalized_url"] = pd.Series(dtype="object")
        return candidates
    candidates["normalized_url"] = candidates["submitted_url"].map(normalize_url)
    return candidates.dropna(subset=["normalized_url"]).drop_duplicates(
        subset=["filing_id", "source_type", "source_ein", "normalized_url"]
    )


def _bounded(value: pd.Series, lower: float = 0, upper: float = 1) -> pd.Series:
    return value.clip(lower=lower, upper=upper)


def _ratio_score(
    numerator: pd.Series,
    denominator: pd.Series,
    formula: str,
) -> pd.Series:
    result = pd.Series(pd.NA, index=numerator.index, dtype="Float64")
    valid = numerator.notna() & denominator.notna() & (denominator > 0)
    if formula == "one_minus":
        result.loc[valid] = 1 - numerator.loc[valid] / denominator.loc[valid]
    else:
        result.loc[valid] = numerator.loc[valid] / denominator.loc[valid]
    return _bounded(result)


def calculate_index_components(
    transparency_df: pd.DataFrame,
    website_scores: pd.DataFrame | None = None,
    store: bool = False,
    db_path: Path | None = None,
    run_id: str | None = None,
) -> pd.DataFrame:
    required = {
        "filing_id",
        "ein",
        "tax_year",
        "filer_name",
        "voting_members_governing_body",
        "max_board_size",
        "total_volunteers",
        "num_527s",
        "num_c3s",
        "political_expenses",
        "total_expenses",
        "total_salaries",
        "unrestricted_net_assets_eoy",
        "fundraising_expenses",
        "grants_and_contributions",
    }
    missing = required - set(transparency_df.columns)
    if missing:
        raise ValueError(f"Missing transparency source columns: {sorted(missing)}")
    result = transparency_df[["filing_id", "ein", "tax_year", "filer_name"]].copy()
    result["index_version"] = INDEX_VERSION

    board = transparency_df[["voting_members_governing_body", "max_board_size"]].max(
        axis=1, skipna=True
    )
    board_missing = transparency_df[
        ["voting_members_governing_body", "max_board_size"]
    ].isna().all(axis=1)
    result["board_members"] = _bounded(
        1 - board.clip(upper=VOTING_MEMBER_CAP) / VOTING_MEMBER_CAP
    ).astype("Float64")
    result.loc[board_missing, "board_members"] = pd.NA

    volunteers = transparency_df["total_volunteers"]
    result["volunteers"] = volunteers.map(
        lambda value: pd.NA if pd.isna(value) else float(value == 0)
    ).astype("Float64")

    if website_scores is not None:
        website_columns = ["filing_id", "website_words", "website_observation_id"]
        available = [column for column in website_columns if column in website_scores]
        if "website_words" in available:
            result = result.merge(website_scores[available], on="filing_id", how="left")
        else:
            result["website_words"] = pd.NA
            result["website_observation_id"] = pd.NA
    else:
        result["website_words"] = pd.NA
        result["website_observation_id"] = pd.NA

    result["related_to_527s"] = transparency_df["num_527s"].map(
        lambda value: pd.NA if pd.isna(value) else float(value > 0)
    ).astype("Float64")
    result["related_to_c3s"] = transparency_df["num_c3s"].map(
        lambda value: pd.NA if pd.isna(value) else float(value == 0)
    ).astype("Float64")
    result["political_expenses"] = _ratio_score(
        transparency_df["political_expenses"],
        transparency_df["total_expenses"],
        "ratio",
    )
    result["total_salaries"] = _ratio_score(
        transparency_df["total_salaries"],
        transparency_df["total_expenses"],
        "one_minus",
    )
    result["unrestricted_net_assets"] = pd.Series(
        pd.NA, index=transparency_df.index, dtype="Float64"
    )
    valid_assets = (
        transparency_df["unrestricted_net_assets_eoy"].notna()
        & transparency_df["total_expenses"].notna()
        & (transparency_df["total_expenses"] > 0)
    )
    net_assets = transparency_df["unrestricted_net_assets_eoy"].clip(lower=0)
    result.loc[valid_assets, "unrestricted_net_assets"] = _bounded(
        1
        - net_assets.loc[valid_assets]
        / (
            UNRESTRICTED_ASSET_EXPENSE_MULTIPLE
            * transparency_df.loc[valid_assets, "total_expenses"]
        )
    )
    result["fundraising_expenses"] = _ratio_score(
        transparency_df["fundraising_expenses"],
        transparency_df["grants_and_contributions"],
        "one_minus",
    )

    if "website_words" in result:
        result["website_words"] = result["website_words"].astype("Float64")
        result["website_words"] = result["website_words"].map(
            lambda value: pd.NA
            if pd.isna(value)
            else float(1 - min(value, WEBSITE_WORD_CAP) / WEBSITE_WORD_CAP)
        ).astype("Float64")
    score_frame = result[list(SCORE_COLUMNS)].astype("Float64")
    result["observed_components"] = score_frame.notna().sum(axis=1).astype("int64")
    result["index_score"] = score_frame.sum(axis=1, skipna=True).astype("Float64")
    result.loc[result["observed_components"] == 0, "index_score"] = pd.NA
    result["normalized_index_score"] = (
        result["index_score"] / result["observed_components"] * len(SCORE_COLUMNS)
    ).where(result["observed_components"] > 0)
    result["complete"] = (result["observed_components"] == len(SCORE_COLUMNS)).astype("int64")
    result["run_id"] = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    result["generated_at"] = datetime.now(UTC).isoformat()

    if store:
        write_index_outputs(result, output_dir=settings.data_dir / "transparency_index")
    return result


def write_index_outputs(
    scores: pd.DataFrame,
    output_dir: Path,
    source: pd.DataFrame | None = None,
    write_s3: bool = False,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(scores["run_id"].iloc[0])
    score_path = output_dir / f"transparency_index__{run_id}.parquet"
    scores.to_parquet(score_path, index=False, compression="zstd")
    paths = {"scores": score_path}
    if source is not None:
        source_path = output_dir / f"transparency_source__{run_id}.parquet"
        source.to_parquet(source_path, index=False, compression="zstd")
        paths["source"] = source_path
    manifest_path = output_dir / f"manifest__{run_id}.json"
    manifest = {
        "run_id": run_id,
        "index_version": INDEX_VERSION,
        "score_path": str(score_path),
        "source_path": str(paths.get("source", "")),
        "rows": int(len(scores)),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    paths["manifest"] = manifest_path
    if write_s3:
        df_to_s3(scores, f"parquet/transparency_index/transparency_index__{run_id}.parquet")
        if source is not None:
            df_to_s3(
                source,
                f"parquet/transparency_source/transparency_source__{run_id}.parquet",
            )
    return paths


def persist_scores(
    scores: pd.DataFrame,
    db_path: Path | None = None,
) -> None:
    init_db(db_path)
    columns = [
        "run_id",
        "index_version",
        "filing_id",
        "ein",
        "tax_year",
        "index_score",
        "normalized_index_score",
        "observed_components",
        "complete",
        "website_observation_id",
        *SCORE_COLUMNS,
        "generated_at",
    ]
    rows: list[tuple[Any, ...]] = []
    for row in scores[columns].itertuples(index=False, name=None):
        rows.append(tuple(None if pd.isna(value) else value for value in row))
    with _connection(db_path) as connection:
        connection.executemany(
            f"""
            INSERT INTO transparency_index_scores ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            ON CONFLICT(run_id, filing_id) DO UPDATE SET
                {", ".join(f"{column}=excluded.{column}" for column in columns[1:])}
            """,
            rows,
        )
