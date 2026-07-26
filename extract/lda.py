"""Senate LDA (Lobbying Disclosure Act) API collector.

Pulls lobbying filings and their lobbying-activity descriptions (the free-text
issue summaries that are the NLP target) into SQLite.

The LDA API hard-caps results at 25 per page regardless of page_size. With ~97K
filings per year that means ~3,900 requests. We use a ThreadPoolExecutor with a
rate-limiting semaphore to fetch pages concurrently while respecting the API's
120 requests/min limit (authenticated). Failed/rate-limited pages are retried
with exponential backoff.

Docs: https://lda.senate.gov/api/redoc/v1/

Usage:
    from extract.lda import LdaCollector
    LdaCollector().collect_filings(filing_year=2024)
"""
from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

from .config import settings
from .db import connect, init_db, insert_many, upsert

logger = logging.getLogger(__name__)

BASE_URL = "https://lda.senate.gov/api/v1"
PAGE_SIZE = 25  # LDA hard cap


class _RateLimiter:
    """Token-bucket rate limiter for concurrent threads."""

    def __init__(self, requests_per_minute: int) -> None:
        self._delay = 60.0 / requests_per_minute
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._last + self._delay - now
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


class LdaCollector:
    def __init__(
        self,
        api_key: str | None = None,
        db_path: Path | None = None,
        workers: int = 4,
        requests_per_minute: int = 100,
    ) -> None:
        self.db_path = db_path
        self.workers = workers
        self._limiter = _RateLimiter(requests_per_minute)
        key = api_key or settings.lda_api_key
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "influence-network-capstone/0.1 (educational research)",
        })
        if key:
            self._session.headers["Authorization"] = f"Token {key}"

    def _fetch_page(self, filing_year: int, page: int, max_retries: int = 5) -> dict[str, Any]:
        """Fetch one page with rate limiting and retry on 429."""
        for attempt in range(max_retries):
            self._limiter.acquire()
            try:
                r = self._session.get(
                    f"{BASE_URL}/filings/",
                    params={"filing_year": filing_year, "page": page, "page_size": PAGE_SIZE},
                    timeout=30,
                )
                if r.status_code == 429:
                    wait = 2 ** attempt * 5
                    logger.debug("429 on page %d (attempt %d), sleeping %ds", page, attempt + 1, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.exceptions.Timeout:
                wait = 2 ** attempt * 2
                logger.debug("Timeout on page %d (attempt %d), sleeping %ds", page, attempt + 1, wait)
                time.sleep(wait)
        raise RuntimeError(f"Page {page} failed after {max_retries} retries")

    def _total_pages(self, filing_year: int) -> tuple[int, int]:
        """Return (total_filings, total_pages) for a year."""
        data = self._fetch_page(filing_year, 1)
        total = data.get("count", 0)
        return total, math.ceil(total / PAGE_SIZE)

    def collect_filings(self, filing_year: int, limit: int | None = None) -> int:
        """Fetch all filings for a year using concurrent page requests.

        Returns the number of filings written to the database.
        """
        init_db(self.db_path)

        total_filings, total_pages = self._total_pages(filing_year)
        if limit:
            total_pages = min(total_pages, math.ceil(limit / PAGE_SIZE))
        logger.info("LDA %d: %d filings across %d pages (%d workers)",
                    filing_year, total_filings, total_pages, self.workers)

        count = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self._fetch_page, filing_year, p): p
                for p in range(1, total_pages + 1)
            }
            for future in as_completed(futures):
                page_num = futures[future]
                try:
                    data = future.result()
                except Exception as exc:
                    logger.warning("Page %d failed permanently: %s", page_num, exc)
                    continue

                filings = data.get("results") or []
                with connect(self.db_path) as conn:
                    for f in filings:
                        if limit and count >= limit:
                            break
                        uuid = f.get("filing_uuid")
                        client_info = f.get("client") or {}
                        registrant = f.get("registrant") or {}
                        upsert(conn, "lda_filings", {
                            "filing_uuid": uuid,
                            "client_name": client_info.get("name"),
                            "registrant_name": registrant.get("name"),
                            "filing_year": f.get("filing_year"),
                            "filing_period": f.get("filing_period"),
                            "income": f.get("income"),
                            "expenses": f.get("expenses"),
                            "raw_json": f,
                        })
                        insert_many(conn, "lda_lobbying_activities", (
                            {
                                "filing_uuid": uuid,
                                "general_issue_code": a.get("general_issue_code"),
                                "description": a.get("description"),
                                "raw_json": a,
                            }
                            for a in (f.get("lobbying_activities") or [])
                        ))
                        count += 1

                if count % 5000 == 0 and count > 0:
                    logger.info("LDA %d: %d/%d filings written...", filing_year, count, total_filings)

        logger.info("LDA %d: done, %d filings written", filing_year, count)
        return count

    def close(self) -> None:
        self._session.close()
