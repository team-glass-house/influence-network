"""Command-line entry point for the extraction pipeline."""
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
    p_con_detail = sub.add_parser(
        "congress-details",
        help="Backfill missing bill fields from Congress.gov detail responses",
    )
    p_con_detail.add_argument("--limit", type=int, default=None)
    p_con_detail.add_argument(
        "--all",
        dest="linked_only",
        action="store_false",
        help="Backfill every bill with a missing detail field, not just linked bills",
    )

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
    p_reingest = sub.add_parser(
        "reingest-irs990",
        help="Refresh available IRS 990 source objects with the current parser",
    )
    p_reingest.add_argument("--root", type=Path, default=Path("."))
    p_reingest.add_argument("--batch-size", type=int, default=250)
    p_reingest.add_argument("--limit", type=int, default=None)
    p_reingest.add_argument("--path-prefix", default=None)
    p_reingest.add_argument("--eligible-only", action="store_true")
    p_reingest.add_argument("--force", action="store_true")
    p_refresh = sub.add_parser(
        "refresh-analysis",
        help="Sync entity observations, generate candidates, and rebuild views",
    )
    p_refresh.add_argument(
        "--no-fuzzy",
        action="store_true",
        help="Generate exact candidates only",
    )
    p_refresh.add_argument(
        "--include-relationships",
        action="store_true",
        help="Include broader parent/subsidiary and regional discovery candidates",
    )
    p_transparency = sub.add_parser(
        "transparency-index",
        help="Crawl organization websites and build transparency scores",
    )
    p_transparency.add_argument("--db", dest="transparency_db", type=Path, default=None)
    p_transparency.add_argument("--output-dir", type=Path, default=None)
    p_transparency.add_argument(
        "--max-sites",
        type=int,
        default=25,
        help="Unique URLs to crawl; 0 means all candidates",
    )
    p_transparency.add_argument("--max-pages", type=int, default=10)
    p_transparency.add_argument("--timeout", type=float, default=15.0)
    p_transparency.add_argument("--delay", type=float, default=0.25)
    p_transparency.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Concurrent site crawls; keep bounded to respect remote servers",
    )
    p_transparency.add_argument("--no-crawl", action="store_true")
    p_transparency.add_argument("--s3", action="store_true")
    p_transparency.add_argument(
        "--export-parquet",
        action="store_true",
        help="Export the SQL run to Parquet after scoring",
    )
    p_transparency.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="RUN_ID",
        help="Resume a running/interrupted crawl, or the latest one if omitted",
    )
    p_export = sub.add_parser(
        "transparency-export-parquet",
        help="Export a SQL transparency-index run to Parquet",
    )
    p_export.add_argument("--run-id", default=None)
    p_export.add_argument("--output-dir", type=Path, default=None)
    p_export.add_argument("--s3", action="store_true")
    p_import = sub.add_parser(
        "transparency-import-parquet",
        help="Import the current transparency Parquet files into SQLite",
    )
    p_import.add_argument("--scores", type=Path, required=True)
    p_import.add_argument("--source", type=Path, required=True)
    p_import.add_argument("--run-id", default=None)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "init-db":
        from .db import init_db
        init_db(args.db)
        print("Database initialized.")
        return 0

    if args.command == "congress":
        from .congress import CongressCollector
        collector = CongressCollector(db_path=args.db)
        try:
            n = collector.collect_bills(args.congress, args.bill_type, args.limit)
        finally:
            collector.close()
        print(f"Collected {n} bills.")
        return 0

    if args.command == "congress-details":
        from .congress import CongressCollector
        collector = CongressCollector(db_path=args.db)
        try:
            n = collector.backfill_bill_details(args.limit, args.linked_only)
        finally:
            collector.close()
        print(f"Backfilled {n} bill details.")
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

    if args.command == "reingest-irs990":
        from .irs990 import reingest_990_sources
        result = reingest_990_sources(
            root=args.root,
            db_path=args.db,
            batch_size=args.batch_size,
            limit=args.limit,
            path_prefix=args.path_prefix,
            eligible_only=args.eligible_only,
            force=args.force,
        )
        print(result)
        return 0

    if args.command == "refresh-analysis":
        from .pipeline import refresh_analysis_layers
        result = refresh_analysis_layers(
            args.db,
            include_fuzzy_candidates=not args.no_fuzzy,
            include_relationship_candidates=args.include_relationships,
        )
        print(result.as_dict())
        return 0

    if args.command == "transparency-export-parquet":
        from .transparency_index import export_transparency_index_run

        paths = export_transparency_index_run(
            db_path=args.db,
            output_dir=args.output_dir,
            run_id=args.run_id,
            write_s3=args.s3,
        )
        print({name: str(path) for name, path in paths.items()})
        return 0

    if args.command == "transparency-import-parquet":
        from .transparency_index import import_transparency_parquet

        result = import_transparency_parquet(
            scores_path=args.scores,
            source_path=args.source,
            db_path=args.db,
            run_id=args.run_id,
        )
        print(result)
        return 0

    if args.command == "transparency-index":
        from .transparency_pipeline import run_transparency_index
        from .website_crawler import CrawlConfig

        result = run_transparency_index(
            db_path=args.transparency_db or args.db,
            output_dir=args.output_dir,
            max_sites=args.max_sites,
            crawl=not args.no_crawl,
            config=CrawlConfig(
                max_pages=args.max_pages,
                timeout_seconds=args.timeout,
                delay_seconds=args.delay,
            ),
            write_s3=args.s3,
            resume_run_id=args.resume,
            workers=args.workers,
            export_parquet=args.export_parquet,
        )
        print({
            "run_id": result.run_id,
            "filings": result.filings,
            "website_candidates": result.website_candidates,
            "websites_crawled": result.websites_crawled,
            "resumed": result.resumed,
            "status": result.status,
            "scores_path": str(result.scores_path) if result.scores_path else None,
            "manifest_path": str(result.manifest_path) if result.manifest_path else None,
        })
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
