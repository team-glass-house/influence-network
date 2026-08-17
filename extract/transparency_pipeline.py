from __future__ import annotations

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

from .config import settings
from .db import init_db
from .transparency_index import (
    calculate_index_components,
    get_transparency_index_data,
    ingest_transparency_index_source,
    persist_scores,
    write_index_outputs,
)
from .website_crawler import CRAWLER_VERSION, CrawlConfig, CrawlResult, crawl_website

logger = logging.getLogger(__name__)
RESUMABLE_STATUSES = {"running", "interrupted"}


@dataclass(frozen=True)
class TransparencyRun:
    run_id: str
    filings: int
    website_candidates: int
    websites_crawled: int
    scores_path: Path | None
    manifest_path: Path | None
    status: str = "completed"
    resumed: bool = False
    websites_remaining: int = 0


def _connection(db_path: Path | None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else settings.db_path
    connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    return connection


def _read_cached(
    connection: sqlite3.Connection,
    normalized_url: str,
    config: CrawlConfig,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM transparency_website_observations
        WHERE normalized_url = ? AND crawler_version = ? AND policy_hash = ?
        """,
        (normalized_url, CRAWLER_VERSION, config.policy_hash),
    ).fetchone()


def _store_observation(
    connection: sqlite3.Connection,
    result: CrawlResult,
    config: CrawlConfig,
) -> int:
    connection.execute(
        """
        INSERT INTO transparency_website_observations (
            requested_url, normalized_url, final_url, status, http_status,
            word_count, capped_word_count, pages_crawled, page_urls_json,
            error, robots_allowed, crawler_version, policy_hash, policy_json,
            retrieved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(normalized_url, crawler_version, policy_hash) DO UPDATE SET
            requested_url=excluded.requested_url,
            final_url=excluded.final_url,
            status=excluded.status,
            http_status=excluded.http_status,
            word_count=excluded.word_count,
            capped_word_count=excluded.capped_word_count,
            pages_crawled=excluded.pages_crawled,
            page_urls_json=excluded.page_urls_json,
            error=excluded.error,
            robots_allowed=excluded.robots_allowed,
            policy_json=excluded.policy_json,
            retrieved_at=excluded.retrieved_at
        """,
        (
            result.requested_url,
            result.normalized_url,
            result.final_url,
            result.status,
            result.http_status,
            result.word_count,
            result.capped_word_count,
            result.pages_crawled,
            json.dumps(result.page_urls),
            result.error,
            result.robots_allowed,
            result.crawler_version,
            config.policy_hash,
            config.policy_json,
            result.retrieved_at,
        ),
    )
    return int(
        connection.execute(
            """
            SELECT observation_id FROM transparency_website_observations
            WHERE normalized_url = ? AND crawler_version = ? AND policy_hash = ?
            """,
            (result.normalized_url, result.crawler_version, config.policy_hash),
        ).fetchone()[0]
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _run_row(
    connection: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM transparency_crawl_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Transparency crawl run not found: {run_id}")
    return row


def _latest_resumable_run(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT *
        FROM transparency_crawl_runs
        WHERE status IN ('running', 'interrupted')
        ORDER BY updated_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise ValueError("No running or interrupted transparency crawl run found")
    return row


def _insert_run(
    connection: sqlite3.Connection,
    run_id: str,
    config: CrawlConfig,
    max_sites: int | None,
    candidate_count: int,
    unique_url_count: int,
) -> None:
    now = _now()
    connection.execute(
        """
        INSERT INTO transparency_crawl_runs (
            run_id, status, crawler_version, policy_hash, policy_json,
            max_sites, candidate_count, unique_url_count, started_at, updated_at
        ) VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            CRAWLER_VERSION,
            config.policy_hash,
            config.policy_json,
            max_sites,
            candidate_count,
            unique_url_count,
            now,
            now,
        ),
    )


def _update_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    status: str | None = None,
    crawled_count: int | None = None,
    reused_count: int | None = None,
    error: str | None = None,
) -> None:
    assignments = ["updated_at = ?"]
    values: list[object] = [_now()]
    if status is not None:
        assignments.append("status = ?")
        values.append(status)
    if crawled_count is not None:
        assignments.append("crawled_count = ?")
        values.append(crawled_count)
    if reused_count is not None:
        assignments.append("reused_count = ?")
        values.append(reused_count)
    if status == "completed":
        assignments.append("completed_at = ?")
        values.append(_now())
        assignments.append("error = NULL")
    if error is not None:
        assignments.append("error = ?")
        values.append(error)
    values.append(run_id)
    connection.execute(
        f"UPDATE transparency_crawl_runs SET {', '.join(assignments)} WHERE run_id = ?",
        values,
    )


def _insert_candidates(
    connection: sqlite3.Connection,
    run_id: str,
    candidates: pd.DataFrame,
) -> None:
    connection.executemany(
        """
        INSERT INTO transparency_website_candidates (
            run_id, filing_id, source_type, source_ein, submitted_url,
            normalized_url, observation_id, selected
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0)
        ON CONFLICT(run_id, filing_id, source_type, source_ein, normalized_url)
        DO UPDATE SET submitted_url = excluded.submitted_url
        """,
        [
            (
                run_id,
                int(row.filing_id),
                row.source_type,
                row.source_ein or "",
                row.submitted_url,
                row.normalized_url,
            )
            for row in candidates.itertuples(index=False)
        ],
    )


def _run_candidates(
    connection: sqlite3.Connection,
    run_id: str,
) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT filing_id, source_type, source_ein, submitted_url,
               normalized_url, observation_id
        FROM transparency_website_candidates
        WHERE run_id = ?
        """,
        connection,
        params=(run_id,),
    )


def _link_observation(
    connection: sqlite3.Connection,
    run_id: str,
    normalized_url: str,
    observation_id: int,
) -> None:
    connection.execute(
        """
        UPDATE transparency_website_candidates
        SET observation_id = ?
        WHERE run_id = ? AND normalized_url = ?
        """,
        (observation_id, run_id, normalized_url),
    )


def _website_scores(
    connection: sqlite3.Connection,
    run_id: str,
) -> pd.DataFrame:
    observations = pd.read_sql_query(
        """
        SELECT c.filing_id, c.source_type, c.normalized_url,
               o.observation_id, o.status, o.capped_word_count
        FROM transparency_website_candidates c
        LEFT JOIN transparency_website_observations o
          ON o.observation_id = c.observation_id
        WHERE c.run_id = ?
          AND o.status IN ('success', 'partial')
          AND o.capped_word_count IS NOT NULL
        """,
        connection,
        params=(run_id,),
    )
    if observations.empty:
        return pd.DataFrame(
            columns=["filing_id", "website_words", "website_observation_id"]
        )
    observations["source_priority"] = (observations["source_type"] == "c4").astype(int)
    selected = (
        observations.sort_values(
            ["filing_id", "capped_word_count", "source_priority", "observation_id"]
        )
        .drop_duplicates("filing_id", keep="last")
        .rename(
            columns={
                "capped_word_count": "website_words",
                "observation_id": "website_observation_id",
            }
        )
    )
    return selected[["filing_id", "website_words", "website_observation_id"]]


def _mark_selected(
    connection: sqlite3.Connection,
    run_id: str,
    website_scores: pd.DataFrame,
) -> None:
    connection.execute(
        "UPDATE transparency_website_candidates SET selected = 0 WHERE run_id = ?",
        (run_id,),
    )
    connection.executemany(
        """
        UPDATE transparency_website_candidates
        SET selected = 1
        WHERE run_id = ? AND filing_id = ? AND observation_id = ?
        """,
        [
            (run_id, int(row.filing_id), int(row.website_observation_id))
            for row in website_scores.itertuples(index=False)
        ],
    )


def run_transparency_index(
    db_path: Path | None = None,
    output_dir: Path | None = None,
    max_sites: int | None = 25,
    crawl: bool = True,
    config: CrawlConfig | None = None,
    write_s3: bool = False,
    crawler: Callable[[str, CrawlConfig], CrawlResult] = crawl_website,
    resume_run_id: str | None = None,
    workers: int = 1,
    export_parquet: bool = False,
) -> TransparencyRun:
    """Refresh the transparency index without collecting website data.

    The website crawler and its arguments remain in the signature so existing
    callers can transition without breaking, but website observations are no
    longer part of the index refresh.
    """
    if resume_run_id is not None:
        raise ValueError("Transparency website crawl resumption is no longer supported")
    init_db(db_path)
    output_dir = output_dir or settings.data_dir / "transparency_index"
    run_id = _new_run_id()
    source = ingest_transparency_index_source(run_id, db_path)
    logger.info("Refreshing transparency index %s for %d filings", run_id, len(source))
    scores = calculate_index_components(source, run_id=run_id)
    persist_scores(scores, db_path)
    paths: dict[str, Path] = {}
    if export_parquet or write_s3:
        paths = write_index_outputs(
            scores,
            output_dir=output_dir,
            source=source,
            write_s3=write_s3,
        )
    return TransparencyRun(
        run_id=run_id,
        filings=len(source),
        website_candidates=0,
        websites_crawled=0,
        scores_path=paths.get("scores"),
        manifest_path=paths.get("manifest"),
        status="completed",
        resumed=False,
        websites_remaining=0,
    )
