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
    INDEX_VERSION,
    calculate_index_components,
    get_transparency_index_data,
    get_website_candidates,
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
    scores_path: Path
    manifest_path: Path
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
) -> TransparencyRun:
    if max_sites == 0:
        max_sites = None
    if max_sites is not None and max_sites < 0:
        raise ValueError("max_sites cannot be negative")
    if workers < 1:
        raise ValueError("workers must be positive")
    init_db(db_path)
    config = config or CrawlConfig()
    source = get_transparency_index_data(db_path)
    output_dir = output_dir or settings.data_dir / "transparency_index"

    resumed = resume_run_id is not None
    crawl_count = 0
    reused_count = 0
    run_id: str
    urls: pd.DataFrame
    candidates: pd.DataFrame

    with _connection(db_path) as connection:
        if resumed:
            run = (
                _latest_resumable_run(connection)
                if resume_run_id == "latest"
                else _run_row(connection, resume_run_id)
            )
            if run["status"] not in RESUMABLE_STATUSES:
                raise ValueError(
                    f"Transparency crawl run {run['run_id']} is not resumable "
                    f"(status={run['status']})"
                )
            if (
                run["crawler_version"] != CRAWLER_VERSION
                or run["policy_hash"] != config.policy_hash
            ):
                raise ValueError(
                    "Resume configuration does not match the saved crawl policy"
                )
            run_id = run["run_id"]
            crawl_count = int(run["crawled_count"])
            reused_count = int(run["reused_count"])
            candidates = _run_candidates(connection, run_id)
            urls = candidates[["normalized_url", "submitted_url"]].drop_duplicates(
                "normalized_url"
            ).sort_values("normalized_url")
            logger.info(
                "Resuming transparency crawl %s: %d URLs remain in the snapshot",
                run_id,
                len(urls),
            )
        else:
            candidates = get_website_candidates(db_path)
            urls = candidates[["normalized_url", "submitted_url"]].drop_duplicates(
                "normalized_url"
            ).sort_values("normalized_url")
            if max_sites is not None:
                urls = urls.head(max_sites)
            candidates = candidates[
                candidates["normalized_url"].isin(set(urls["normalized_url"]))
            ].copy()
            run_id = _new_run_id()
            _insert_run(
                connection,
                run_id,
                config,
                max_sites,
                len(candidates),
                len(urls),
            )
            _insert_candidates(connection, run_id, candidates)
            connection.commit()
            logger.info(
                "Started transparency crawl %s: %d unique URLs, %d filing candidates",
                run_id,
                len(urls),
                len(candidates),
            )

        pending_urls: list[str] = []
        for row in urls.itertuples(index=False):
            existing = connection.execute(
                """
                SELECT o.*
                FROM transparency_website_candidates c
                JOIN transparency_website_observations o
                  ON o.observation_id = c.observation_id
                WHERE c.run_id = ? AND c.normalized_url = ?
                LIMIT 1
                """,
                (run_id, row.normalized_url),
            ).fetchone()
            if existing is not None:
                continue
            cached = existing or _read_cached(connection, row.normalized_url, config)
            if cached is not None:
                _link_observation(
                    connection,
                    run_id,
                    row.normalized_url,
                    int(cached["observation_id"]),
                )
                if existing is None:
                    reused_count += 1
                _update_run(
                    connection,
                    run_id,
                    crawled_count=crawl_count,
                    reused_count=reused_count,
                )
                connection.commit()
            elif crawl:
                pending_urls.append(row.normalized_url)

        if pending_urls:
            logger.info(
                "Crawling %d URLs with %d workers",
                len(pending_urls),
                workers,
            )
            executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="transparency-crawl",
            )
            futures = {
                executor.submit(crawler, url, config): url for url in pending_urls
            }
            try:
                for future in as_completed(futures):
                    url = futures[future]
                    try:
                        result = future.result()
                    except KeyboardInterrupt:
                        _update_run(
                            connection,
                            run_id,
                            status="interrupted",
                            crawled_count=crawl_count,
                            reused_count=reused_count,
                        )
                        connection.commit()
                        for pending in futures:
                            pending.cancel()
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise
                    except Exception as exc:
                        _update_run(
                            connection,
                            run_id,
                            status="interrupted",
                            crawled_count=crawl_count,
                            reused_count=reused_count,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        connection.commit()
                        for pending in futures:
                            pending.cancel()
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise
                    observation_id = _store_observation(connection, result, config)
                    _link_observation(connection, run_id, url, observation_id)
                    crawl_count += 1
                    _update_run(
                        connection,
                        run_id,
                        crawled_count=crawl_count,
                        reused_count=reused_count,
                    )
                    connection.commit()
                    if crawl_count == 1 or crawl_count % 25 == 0:
                        logger.info(
                            "Transparency crawl %s: %d/%d URLs processed",
                            run_id,
                            crawl_count,
                            len(urls),
                        )
            except KeyboardInterrupt:
                for pending in futures:
                    pending.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                executor.shutdown(wait=True)

        pending = connection.execute(
            """
            SELECT COUNT(DISTINCT normalized_url)
            FROM transparency_website_candidates
            WHERE run_id = ? AND observation_id IS NULL
            """,
            (run_id,),
        ).fetchone()[0]
        if pending and crawl:
            _update_run(
                connection,
                run_id,
                crawled_count=crawl_count,
                reused_count=reused_count,
            )
            connection.commit()
            raise RuntimeError(
                f"Transparency crawl {run_id} has {pending} unprocessed URLs"
            )

        website_scores_frame = _website_scores(connection, run_id)
        scores = calculate_index_components(
            source,
            website_scores=website_scores_frame,
            run_id=run_id,
        )
        _mark_selected(connection, run_id, website_scores_frame)
        _update_run(
            connection,
            run_id,
            status="completed",
            crawled_count=crawl_count,
            reused_count=reused_count,
        )
        connection.commit()

    persist_scores(scores, db_path)
    paths = write_index_outputs(
        scores,
        output_dir=output_dir,
        source=source,
        write_s3=write_s3,
    )
    return TransparencyRun(
        run_id=run_id,
        filings=len(source),
        website_candidates=len(candidates),
        websites_crawled=crawl_count,
        scores_path=paths["scores"],
        manifest_path=paths["manifest"],
        status="completed",
        resumed=resumed,
        websites_remaining=0,
    )
