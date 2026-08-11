"""Convert the SQLite database to Parquet files, keeping every file < 100 MB.

Standalone utility — does NOT touch the Streamlit app. It exports every
(non-empty) base table to Parquet with these rules:

  * Small tables            -> a single Parquet file.
  * Big tables WITH a year  -> one set of files per year (tax_year / filing_year
    / cycle / year), each further chunked if a single year would exceed the cap.
  * Big tables WITHOUT a year -> chunked by rowid into <cap files.

Every output file is zstd-compressed and kept under --max-mb (default 90),
comfortably below GitHub's 100 MB per-file hard limit.

Usage:
    python scripts/db_to_parquet.py
    python scripts/db_to_parquet.py --db data/irs990_full.db --out parquet_export --max-mb 90
    python scripts/db_to_parquet.py --tables committees,bills,irs990_filings
    python scripts/db_to_parquet.py --exclude-tables entity_observations
"""
from __future__ import annotations

import argparse
import io
import os
import sqlite3

import pandas as pd

YEAR_COLS = ["tax_year", "filing_year", "cycle", "year", "last_report_year"]
_SENTINEL = object()


def list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()]


def row_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM '{table}'").fetchone()[0]


def bytes_per_row(conn: sqlite3.Connection, table: str) -> float | None:
    """Estimate compressed parquet bytes/row from a head+tail sample.

    Sampling both ends (cheap, index-based) catches tables whose row size grows
    toward the end (e.g. long description/raw_json columns), which a head-only
    sample under-estimates.
    """
    head = pd.read_sql_query(f"SELECT * FROM '{table}' LIMIT 10000", conn)
    tail = pd.read_sql_query(f"SELECT * FROM '{table}' ORDER BY rowid DESC LIMIT 10000", conn)
    df = pd.concat([head, tail], ignore_index=True)
    if df.empty:
        return None
    buf = io.BytesIO()
    df.to_parquet(buf, compression="zstd", index=False)
    return max(1.0, buf.tell() / len(df))


def export_partition(conn, table, rows_per_file, outdir, label,
                     year_col=None, year_val=_SENTINEL):
    """Stream a (optionally year-filtered) partition to numbered parquet files,
    paginating by rowid so we never use a slow OFFSET."""
    where, params = "", []
    if year_col is not None:
        if year_val is None:
            where = f" AND {year_col} IS NULL"
        else:
            where = f" AND {year_col} = ?"
            params = [year_val]

    last_rowid, idx, written = -1, 0, []
    while True:
        q = (f"SELECT rowid AS _rid, * FROM '{table}' "
             f"WHERE rowid > ?{where} ORDER BY rowid LIMIT ?")
        df = pd.read_sql_query(q, conn, params=[last_rowid, *params, rows_per_file])
        if df.empty:
            break
        last_rowid = int(df["_rid"].iloc[-1])
        df = df.drop(columns=["_rid"])
        path = os.path.join(outdir, table, f"{table}__{label}__{idx:04d}.parquet")
        df.to_parquet(path, compression="zstd", index=False)
        written.append((path, os.path.getsize(path), len(df)))
        idx += 1
        if len(df) < rows_per_file:
            break
    return written


def export_table(conn, table, outdir, max_bytes):
    n = row_count(conn, table)
    if n == 0:
        return [], "empty (skipped)"
    bpr = bytes_per_row(conn, table)
    if bpr is None:
        return [], "empty (skipped)"

    # Safety margin: sampling can still under-estimate, so size files smaller
    # than the raw cap to stay safely under 100 MB.
    safe_bpr = bpr * 1.3
    rows_per_file = max(1, int(max_bytes / safe_bpr))
    est_total = n * safe_bpr
    ycol = next((y for y in YEAR_COLS if y in columns(conn, table)), None)
    os.makedirs(os.path.join(outdir, table), exist_ok=True)

    if est_total <= max_bytes:
        written = export_partition(conn, table, rows_per_file, outdir, "all")
        return written, "single file"
    if ycol:
        years = [r[0] for r in conn.execute(
            f"SELECT DISTINCT {ycol} FROM '{table}' ORDER BY {ycol} IS NULL, {ycol}"
        ).fetchall()]
        all_written = []
        for y in years:
            label = f"{ycol}={'NULL' if y is None else y}"
            all_written += export_partition(
                conn, table, rows_per_file, outdir, label, year_col=ycol, year_val=y)
        return all_written, f"partitioned by {ycol} ({len(years)} years)"
    written = export_partition(conn, table, rows_per_file, outdir, "all")
    return written, "chunked by rowid"


def main() -> None:
    ap = argparse.ArgumentParser(description="Export SQLite DB to <100MB Parquet files.")
    ap.add_argument("--db", default="data/irs990_full.db")
    ap.add_argument("--out", default="parquet_export")
    ap.add_argument("--max-mb", type=float, default=90.0,
                    help="Target max file size in MB (kept under GitHub's 100MB limit).")
    ap.add_argument("--tables", default="", help="Comma-separated subset (default: all).")
    ap.add_argument("--exclude-tables", default="",
                    help="Comma-separated tables to skip (default: none).")
    args = ap.parse_args()

    max_bytes = args.max_mb * 1_000_000
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    os.makedirs(args.out, exist_ok=True)

    tables = list_tables(conn)
    if args.tables:
        wanted = {t.strip() for t in args.tables.split(",")}
        tables = [t for t in tables if t in wanted]
    if args.exclude_tables:
        excluded = {t.strip() for t in args.exclude_tables.split(",")}
        tables = [t for t in tables if t not in excluded]

    grand_files, grand_bytes, offenders = 0, 0, []
    print(f"Exporting {len(tables)} tables to '{args.out}/' (cap {args.max_mb:.0f} MB)\n")
    for t in tables:
        written, mode = export_table(conn, t, args.out, max_bytes)
        tot = sum(b for _, b, _ in written)
        grand_files += len(written)
        grand_bytes += tot
        for path, b, _ in written:
            if b > 100_000_000:
                offenders.append((path, b))
        biggest = max((b for _, b, _ in written), default=0)
        print(f"  {t:42} {len(written):>4} files  {tot/1e6:>8.1f} MB total "
              f"(biggest {biggest/1e6:5.1f} MB)  [{mode}]")

    print(f"\nTotal: {grand_files} files, {grand_bytes/1e6:.1f} MB in '{args.out}/'")
    if offenders:
        print("\nWARNING: files over 100 MB (need smaller --max-mb):")
        for path, b in offenders:
            print(f"  {path}  {b/1e6:.1f} MB")
    else:
        print("All files are under 100 MB. \u2713")


if __name__ == "__main__":
    main()
