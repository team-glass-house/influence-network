"""Load IRS Exempt Organization Master File (BMF) and OpenSecrets dark-money
crosswalk into the influence-network database.

Sources:
  eo1.csv / eo_ca.csv  -- IRS Business Master File extract (Publication 78)
  upd.crp_ein_list.csv -- OpenSecrets dark-money EIN list

Usage (from notebook 01 or CLI):
    from extract.irs_master import load_irs_master, load_crp_dark_money

    loaded = load_irs_master("drive/Dark Money Dataset Investigation/eo1.csv")
    print(f"irs_master rows loaded: {loaded:,}")

    crp_loaded = load_crp_dark_money(
        "drive/Dark Money Dataset Investigation/upd.crp_ein_list.csv"
    )
    print(f"crp_dark_money rows loaded: {crp_loaded:,}")
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

from .db import connect, init_db, upsert
from .config import settings


def _parse_real(value: str) -> Optional[float]:
    """Convert a string to float; return None if empty or unparseable."""
    v = value.strip()
    if not v:
        return None
    try:
        return float(v.replace(",", ""))
    except ValueError:
        return None


def load_irs_master(
    csv_path: str | Path,
    db_path: str | Path | None = None,
    batch_size: int = 5_000,
) -> int:
    """Load an IRS BMF CSV (eo1.csv or eo_ca.csv) into the irs_master table.

    Idempotent: re-running updates existing rows with the latest values.
    Returns the number of rows written.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"BMF file not found: {csv_path}")

    init_db(db_path)
    count = 0
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        batch: list[dict] = []
        with connect(db_path) as conn:
            for row in reader:
                ein = row.get("EIN", "").strip().zfill(9)
                if not ein or ein == "000000000":
                    continue
                batch.append({
                    "ein":             ein,
                    "name":            row.get("NAME", "").strip() or None,
                    "state":           row.get("STATE", "").strip() or None,
                    "ntee_code":       row.get("NTEE_CD", "").strip() or None,
                    "subsection_code": row.get("SUBSECTION", "").strip() or None,
                    "foundation_code": row.get("FOUNDATION", "").strip() or None,
                    "status_code":     row.get("STATUS", "").strip() or None,
                    "ruling_date":     row.get("RULING", "").strip() or None,
                    "asset_code":      row.get("ASSET_CD", "").strip() or None,
                    "income_code":     row.get("INCOME_CD", "").strip() or None,
                    "asset_amt":       _parse_real(row.get("ASSET_AMT", "")),
                    "income_amt":      _parse_real(row.get("INCOME_AMT", "")),
                    "revenue_amt":     _parse_real(row.get("REVENUE_AMT", "")),
                    "tax_period":      row.get("TAX_PERIOD", "").strip() or None,
                })
                if len(batch) >= batch_size:
                    for record in batch:
                        upsert(conn, "irs_master", record)
                    count += len(batch)
                    batch = []
                    conn.connection.commit() if hasattr(conn, "connection") else None
            for record in batch:
                upsert(conn, "irs_master", record)
            count += len(batch)
    return count


def load_crp_dark_money(
    csv_path: str | Path,
    db_path: str | Path | None = None,
) -> int:
    """Load the OpenSecrets dark-money EIN crosswalk into crp_dark_money.

    Idempotent. Returns the number of rows written.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CRP file not found: {csv_path}")

    init_db(db_path)
    count = 0
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        with connect(db_path) as conn:
            for row in reader:
                ein = row.get("EIN", "").strip().replace("-", "").zfill(9)
                if not ein or ein == "000000000":
                    continue
                year_str = row.get("year", "").strip()
                try:
                    year = int(year_str)
                except (ValueError, TypeError):
                    year = None
                upsert(conn, "crp_dark_money", {
                    "ein":      ein,
                    "crp_name": row.get("crp.names", "").strip() or None,
                    "org_name": row.get("NAME", "").strip() or None,
                    "year":     year,
                })
                count += 1
    return count
