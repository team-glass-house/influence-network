"""Congress.gov API v3 collector.

Pulls bills for a given Congress and bill type.

Design: collect_bills() uses stub-only mode by default; one paginated list
request per 250 bills, extracting title, introduced date, latest action, and
policy area directly from the list stub. This collects 16K+ bills in ~5 minutes
instead of 16 hours (which would require 4 sub-requests per bill).

For bills of interest identified after LDA matching, use collect_bill_detail()
to fetch full actions, sponsors, and text version URLs for specific bill IDs.

Docs: https://api.congress.gov/  |  https://github.com/LibraryOfCongress/api.congress.gov

Usage:
    congress = CongressCollector(db_path=DB_PATH)
    congress.collect_bills(congress=118, bill_type="hr")   # fast stub mode
    congress.collect_bill_detail("118-hr-1234")            # targeted detail
    congress.collect_members(congress=118)                 # member roster
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .db import connect, init_db, insert_many, upsert
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

        Extracts bill_id, title, introduced_date, latest_action, and policy_area
        from the list response. No per-bill sub-requests; 16K bills in ~5 min.
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
        """Fetch full detail (actions, sponsors, text versions) for one bill.

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
            # Actions
            insert_many(conn, "bill_actions", (
                {"bill_id": bill_id, "action_date": a.get("actionDate"),
                 "action_code": a.get("actionCode"), "action_text": a.get("text"), "raw_json": a}
                for a in self._paginate(f"bill/{congress}/{bill_type}/{number}/actions", "actions")
            ))
            # Sponsors
            for sp in detail.get("sponsors", []) or []:
                upsert(conn, "bill_sponsors", {
                    "bill_id": bill_id, "bioguide_id": sp.get("bioguideId"),
                    "full_name": sp.get("fullName"), "party": sp.get("party"),
                    "state": sp.get("state"), "is_original_cosponsor": 0, "raw_json": sp,
                })
            for cs in self._paginate(f"bill/{congress}/{bill_type}/{number}/cosponsors", "cosponsors"):
                upsert(conn, "bill_sponsors", {
                    "bill_id": bill_id, "bioguide_id": cs.get("bioguideId"),
                    "full_name": cs.get("fullName"), "party": cs.get("party"),
                    "state": cs.get("state"), "is_original_cosponsor": 1, "raw_json": cs,
                })
            # Text versions
            for tv in self._paginate(f"bill/{congress}/{bill_type}/{number}/text", "textVersions"):
                for fmt in tv.get("formats", []) or []:
                    upsert(conn, "bill_text_versions", {
                        "bill_id": bill_id, "version_code": tv.get("type"),
                        "version_date": tv.get("date"), "format_type": fmt.get("type"),
                        "url": fmt.get("url"),
                    })

    def collect_members(self, congress: int, limit: int | None = None) -> int:
        """Collect current members of Congress for a given congress number.

        Writes to the members table (bioguide_id, full_name, party, state, chamber).
        Used to join bill_sponsors to member records after collecting bills.
        Returns number of members written.
        """
        init_db(self.db_path)
        count = 0
        with connect(self.db_path) as conn:
            for member in self._paginate(f"member/congress/{congress}", "members", limit=limit):
                bioguide_id = member.get("bioguideId")
                if not bioguide_id:
                    continue
                terms = member.get("terms") or {}
                # terms is a dict with an "item" list; pick the most recent entry
                term_items = terms.get("item") if isinstance(terms, dict) else []
                chamber = None
                if term_items:
                    chamber = term_items[-1].get("chamber")
                upsert(conn, "members", {
                    "bioguide_id": bioguide_id,
                    "full_name": member.get("name"),
                    "party": member.get("partyName"),
                    "state": member.get("state"),
                    "chamber": chamber,
                    "raw_json": member,
                })
                count += 1
        logger.info("Collected %d members for congress %d", count, congress)
        return count

    def close(self) -> None:
        self.client.close()
