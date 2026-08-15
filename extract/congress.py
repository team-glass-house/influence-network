"""Congress.gov API v3 collector.

Pulls bills for a given Congress and bill type.

Design: collect_bills() uses stub-only mode by default; one paginated list
request per 250 bills, extracting the fields available in the list stub. This
collects 16K+ bills in ~5 minutes. Missing detail fields can be filled by the
targeted congress-details command for linked bills.

For bills of interest identified after LDA matching, use collect_bill_detail()
to fetch the full bill detail and backfill fields that are absent from list
responses.

Docs: https://api.congress.gov/  |  https://github.com/LibraryOfCongress/api.congress.gov

Usage:
    congress = CongressCollector(db_path=DB_PATH)
    congress.collect_bills(congress=118, bill_type="hr")   # fast stub mode
    congress.collect_bill_detail("118-hr-1234")            # targeted detail
    congress.backfill_bill_details()                       # linked bills first
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .db import connect, init_db, upsert
from .http import ApiClient

logger = logging.getLogger(__name__)

BASE_URL = "https://api.congress.gov/v3"
REQUESTS_PER_HOUR = 4500


class CongressCollector:
    def __init__(self, api_key: str | None = None, db_path: Path | None = None) -> None:
        self.db_path = db_path
        key = api_key or settings.require("congress_api_key")
        self.client = ApiClient(
            BASE_URL,
            requests_per_hour=REQUESTS_PER_HOUR,
            default_params={"api_key": key, "format": "json"},
        )

    def _paginate(self, path: str, item_key: str, params: dict[str, Any] | None = None,
                  limit: int | None = None) -> Iterator[dict[str, Any]]:
        offset = 0
        page_size = 250
        yielded = 0
        while True:
            page_params = {"offset": offset, "limit": page_size, **(params or {})}
            data = self.client.get(path, page_params)
            items = data.get(item_key, []) or []
            if not items:
                break
            for item in items:
                yield item
                yielded += 1
                if limit and yielded >= limit:
                    return
            if len(items) < page_size:
                break
            offset += page_size

    def collect_bills(self, congress: int, bill_type: str, limit: int | None = None) -> int:
        """Collect bills using stub-only mode. One request per 250 bills.

        Extracts bill_id, title, and latest action from the list response. No
        per-bill sub-requests; 16K bills in ~5 min.
        Returns number of bills written.
        """
        init_db(self.db_path)
        bill_type = bill_type.lower()
        count = 0
        with connect(self.db_path) as conn:
            for stub in self._paginate(f"bill/{congress}/{bill_type}", "bills", limit=limit):
                number = stub.get("number")
                if number is None:
                    continue
                bill_id = f"{congress}-{bill_type}-{number}"
                latest = stub.get("latestAction") or {}
                policy = stub.get("policyArea") or {}
                upsert(conn, "bills", {
                    "bill_id": bill_id,
                    "congress": congress,
                    "bill_type": bill_type,
                    "bill_number": number,
                    "title": stub.get("title"),
                    "introduced_date": stub.get("introducedDate"),
                    "latest_action": latest.get("text"),
                    "policy_area": policy.get("name"),
                    "raw_json": stub,
                })
                count += 1
                if count % 1000 == 0:
                    logger.info("Collected %d bills (%s %s)...", count, bill_type, number)
        logger.info("Done: %d %s bills for congress %d", count, bill_type, congress)
        return count

    def collect_bill_detail(self, bill_id: str) -> None:
        """Fetch full detail for one bill and update the parent bill row.

        Use this after entity matching to enrich specific bills referenced in LDA
        filings. bill_id format: '118-hr-1234'.
        """
        parts = bill_id.split("-")
        congress, bill_type, number = int(parts[0]), parts[1], int(parts[2])
        with connect(self.db_path) as conn:
            detail = self.client.get(f"bill/{congress}/{bill_type}/{number}").get("bill", {}) or {}
            latest = detail.get("latestAction") or {}
            policy = detail.get("policyArea") or {}
            upsert(conn, "bills", {
                "bill_id": bill_id, "congress": congress, "bill_type": bill_type,
                "bill_number": number, "title": detail.get("title"),
                "introduced_date": detail.get("introducedDate"),
                "latest_action": latest.get("text"),
                "policy_area": policy.get("name"), "raw_json": detail,
            })
    def backfill_bill_details(
        self,
        limit: int | None = None,
        linked_only: bool = True,
    ) -> int:
        """Backfill missing bill fields from Congress.gov detail responses.

        Linked bills are the default scope because they are consumed by the
        lobbying-to-bill dashboard and avoid pulling details for unused bills.
        """
        init_db(self.db_path)
        query = """
            SELECT b.bill_id
            FROM bills AS b
            WHERE (b.introduced_date IS NULL OR b.policy_area IS NULL)
        """
        params: list[Any] = []
        if linked_only:
            query += """
              AND EXISTS (
                  SELECT 1
                  FROM lobbying_bill_links AS link
                  JOIN lda_filings AS filing
                    ON filing.filing_uuid = link.filing_uuid
                  WHERE link.bill_type = b.bill_type
                    AND link.bill_number = b.bill_number
                    AND b.congress = CAST((filing.filing_year - 1789) / 2 AS INTEGER) + 1
              )
            """
        query += " ORDER BY b.bill_id"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        with connect(self.db_path) as conn:
            bill_ids = [row["bill_id"] for row in conn.execute(query, params)]

        for count, bill_id in enumerate(bill_ids, start=1):
            self.collect_bill_detail(bill_id)
            if count % 100 == 0:
                logger.info("Backfilled %d bill details...", count)
        logger.info("Backfilled %d bill details", len(bill_ids))
        return len(bill_ids)

    def close(self) -> None:
        self.client.close()
