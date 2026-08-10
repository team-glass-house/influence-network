"""Load IRS Business Master File data into the project database."""
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
                    "address":         row.get("STREET", "").strip() or None,
                    "city":            row.get("CITY", "").strip() or None,
                    "state":           row.get("STATE", "").strip() or None,
                    "zip_code":        row.get("ZIP", "").strip() or None,
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
