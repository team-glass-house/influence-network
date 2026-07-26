"""openFEC (FEC) API collector.

Pulls Super PAC / hybrid PAC committees and their financial totals.

Design:
  - collect_committees(): all registered Super PACs (type O) and hybrid PACs (type U)
  - collect_committee_totals(): cycle-level financial summary per committee from the
    totals/pac-party endpoint. Much faster than Schedule B:
    ~2,100 active committees, ~21 API pages, ~1 minute. Gives total disbursements,
    receipts, independent expenditures, and cash-on-hand per committee per cycle.

The full Schedule B disbursements table has 157M rows and cannot be pulled in bulk
for dark money research. Individual-committee schedule_b calls are available via
collect_disbursements_for_committee() for targeted lookups after entity matching.

Docs: https://api.open.fec.gov/developers/

Usage:
    fec = FecCollector(db_path=DB_PATH)
    fec.collect_committees(committee_type="O")
    fec.collect_committees(committee_type="U")
    fec.collect_committee_totals(cycle=2024)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .db import connect, init_db, upsert
from .http import ApiClient

logger = logging.getLogger(__name__)

BASE_URL = "https://api.open.fec.gov/v1"
REQUESTS_PER_HOUR = 900


class FecCollector:
    def __init__(self, api_key: str | None = None, db_path: Path | None = None) -> None:
        self.db_path = db_path
        key = api_key or settings.fec_api_key or "DEMO_KEY"
        self.client = ApiClient(
            BASE_URL,
            requests_per_hour=REQUESTS_PER_HOUR,
            default_params={"api_key": key},
        )

    def _paginate(self, path: str, params: dict[str, Any] | None = None,
                  limit: int | None = None) -> Iterator[dict[str, Any]]:
        """Yield results across openFEC's page-based pagination."""
        page = 1
        per_page = 100
        yielded = 0
        while True:
            page_params = {"per_page": per_page, "page": page, **(params or {})}
            data = self.client.get(path, page_params)
            results = data.get("results", []) or []
            if not results:
                break
            for item in results:
                yield item
                yielded += 1
                if limit and yielded >= limit:
                    return
            pagination = data.get("pagination", {}) or {}
            if page >= pagination.get("pages", page):
                break
            page += 1

    def collect_committees(self, committee_type: str | None = None,
                           limit: int | None = None) -> int:
        """Collect committees filtered by type.

        'O' = Super PAC (independent-expenditure-only)
        'U' = Hybrid PAC (can make both direct contributions and IEs)
        """
        init_db(self.db_path)
        params: dict[str, Any] = {}
        if committee_type:
            params["committee_type"] = committee_type
        count = 0
        with connect(self.db_path) as conn:
            for c in self._paginate("committees", params, limit=limit):
                upsert(conn, "committees", {
                    "committee_id": c.get("committee_id"),
                    "name": c.get("name"),
                    "committee_type": c.get("committee_type"),
                    "designation": c.get("designation"),
                    "party": c.get("party"),
                    "raw_json": c,
                })
                count += 1
        logger.info("Collected %d committees (type=%s)", count, committee_type)
        return count

    def collect_committee_totals(self, cycle: int, committee_type: str = "O",
                                 limit: int | None = None) -> int:
        """Collect cycle-level financial totals for active committees.

        Uses totals/pac-party which returns one row per committee that filed
        financial reports. Much faster than Schedule B: ~2,100 rows, ~21 pages,
        ~1 minute for a full cycle.

        Stored in fec_disbursements table as a summary row with sub_id=<committee_id>-<cycle>.
        """
        init_db(self.db_path)
        params: dict[str, Any] = {"cycle": cycle, "committee_type": committee_type,
                                   "sort": "-disbursements"}
        count = 0
        with connect(self.db_path) as conn:
            for row in self._paginate("totals/pac-party", params, limit=limit):
                cid = row.get("committee_id")
                # Upsert the committee record in case it wasn't in collect_committees
                upsert(conn, "committees", {
                    "committee_id": cid,
                    "name": row.get("committee_name"),
                    "committee_type": committee_type,
                    "designation": None,
                    "party": row.get("party"),
                    "cycle": cycle,
                    "total_receipts": row.get("receipts"),
                    "total_disbursements": row.get("disbursements"),
                    "independent_expenditures": row.get("independent_expenditures"),
                    "cash_on_hand_end_period": row.get("last_cash_on_hand_end_period"),
                    "raw_json": row,
                })
                # Store totals as a single summary disbursement row
                upsert(conn, "fec_disbursements", {
                    "sub_id": f"{cid}-{cycle}-totals",
                    "committee_id": cid,
                    "recipient_name": "__CYCLE_TOTALS__",
                    "disbursement_amount": row.get("disbursements"),
                    "disbursement_date": str(cycle),
                    "disbursement_description": (
                        f"receipts={row.get('receipts')} "
                        f"independent_expenditures={row.get('independent_expenditures')} "
                        f"cash_on_hand_end_period={row.get('cash_on_hand_end_period')}"
                    ),
                    "raw_json": row,
                })
                count += 1
        logger.info("Collected totals for %d committees (cycle %d, type=%s)", count, cycle, committee_type)
        return count

    def collect_disbursements_for_committee(self, committee_id: str, cycle: int,
                                            limit: int | None = None) -> int:
        """Collect individual Schedule B disbursements for one committee.

        Use this for targeted lookups after entity matching identifies a committee
        of interest. Not suitable for bulk collection across all committees.
        """
        params: dict[str, Any] = {
            "committee_id": committee_id,
            "two_year_transaction_period": cycle,
            "sort": "-disbursement_date",
        }
        count = 0
        with connect(self.db_path) as conn:
            for d in self._paginate("schedules/schedule_b", params, limit=limit):
                upsert(conn, "fec_disbursements", {
                    "sub_id": d.get("sub_id"),
                    "committee_id": d.get("committee_id"),
                    "recipient_name": d.get("recipient_name"),
                    "disbursement_amount": d.get("disbursement_amount"),
                    "disbursement_date": d.get("disbursement_date"),
                    "disbursement_description": d.get("disbursement_description"),
                    "raw_json": d,
                })
                count += 1
        return count

    def close(self) -> None:
        self.client.close()
