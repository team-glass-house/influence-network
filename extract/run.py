"""Command-line entry point for the extraction pipeline.

Examples
--------
Initialize the database schema:
    python -m extract.run init-db

Pull House bills from the 118th Congress (cap for a quick test):
    python -m extract.run congress --congress 118 --bill-type hr --limit 25

Pull Super PACs and 2024 disbursements:
    python -m extract.run fec-committees --committee-type O --limit 200
    python -m extract.run fec-disbursements --cycle 2024 --limit 500

Pull 2024 lobbying filings:
    python -m extract.run lda --year 2024 --limit 200

Parse a folder of IRS 990 XML files:
    python -m extract.run irs990 --dir data/irs990_xml
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="extract", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--db", type=Path, default=None,
                        help="SQLite database path (defaults to DB_PATH in config)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create the SQLite schema")

    p_con = sub.add_parser("congress", help="Collect Congress.gov bills")
    p_con.add_argument("--congress", type=int, required=True)
    p_con.add_argument("--bill-type", default="hr")
    p_con.add_argument("--limit", type=int, default=None)

    p_fc = sub.add_parser("fec-committees", help="Collect FEC committees")
    p_fc.add_argument("--committee-type", default=None, help="e.g. O for Super PAC")
    p_fc.add_argument("--limit", type=int, default=None)

    p_fd = sub.add_parser("fec-disbursements", help="Collect FEC disbursements")
    p_fd.add_argument("--cycle", type=int, required=True)
    p_fd.add_argument("--committee-id", default=None)
    p_fd.add_argument("--limit", type=int, default=None)

    p_lda = sub.add_parser("lda", help="Collect Senate LDA lobbying filings")
    p_lda.add_argument("--year", type=int, required=True)
    p_lda.add_argument("--limit", type=int, default=None)

    p_irs = sub.add_parser("irs990", help="Parse IRS 990 XML files in a folder")
    p_irs.add_argument("--dir", required=True)
    p_irs.add_argument("--pattern", default="*.xml")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "init-db":
        from .db import init_db
        init_db(args.db)
        print("Database initialized.")
        return 0

    if args.command == "congress":
        from .congress import CongressCollector
        n = CongressCollector(db_path=args.db).collect_bills(
            args.congress, args.bill_type, args.limit
        )
        print(f"Collected {n} bills.")
        return 0

    if args.command == "fec-committees":
        from .fec import FecCollector
        n = FecCollector(db_path=args.db).collect_committees(args.committee_type, args.limit)
        print(f"Collected {n} committees.")
        return 0

    if args.command == "fec-disbursements":
        from .fec import FecCollector
        n = FecCollector(db_path=args.db).collect_disbursements(
            args.cycle, args.committee_id, args.limit
        )
        print(f"Collected {n} disbursements.")
        return 0

    if args.command == "lda":
        from .lda import LdaCollector
        n = LdaCollector(db_path=args.db).collect_filings(args.year, args.limit)
        print(f"Collected {n} LDA filings.")
        return 0

    if args.command == "irs990":
        from .irs990 import ingest_990_directory
        n = ingest_990_directory(args.dir, args.pattern, db_path=args.db)
        print(f"Ingested {n} 990 files.")
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
