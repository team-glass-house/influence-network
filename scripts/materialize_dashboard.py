"""Build summary tables for the SQLite dashboard."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def materialize(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            DROP TABLE IF EXISTS dash_filings_by_year;
            DROP TABLE IF EXISTS dash_most_lobbied_bills;
            DROP TABLE IF EXISTS dash_political_orgs;

            CREATE TABLE dash_filings_by_year AS
            SELECT tax_year, COUNT(*) AS filings
            FROM irs990_filings
            GROUP BY tax_year;
            CREATE INDEX idx_dash_filings_by_year_tax_year
                ON dash_filings_by_year(tax_year);

            CREATE TABLE dash_most_lobbied_bills AS
            SELECT bill_id, title, policy_area,
                   COUNT(DISTINCT filing_uuid) AS lobbying_filings,
                   COUNT(DISTINCT client_name) AS distinct_clients
            FROM lobbying_bill_facts
            WHERE bill_id IS NOT NULL
            GROUP BY bill_id, title, policy_area;
            CREATE INDEX idx_dash_most_lobbied_bills_filings
                ON dash_most_lobbied_bills(lobbying_filings DESC);

            CREATE TABLE dash_political_orgs AS
            WITH latest_filer_names AS (
                SELECT ein, filer_name
                FROM (
                    SELECT ein, filer_name,
                           ROW_NUMBER() OVER (
                               PARTITION BY ein
                               ORDER BY tax_year DESC, filing_id DESC
                           ) AS row_number
                    FROM irs990_filings
                )
                WHERE row_number = 1
            )
            SELECT f.ein, latest.filer_name,
                   MAX(f.political_activity_flag) AS political_flag,
                   MAX(f.tax_year) AS latest_year,
                   SUM(
                       COALESCE(l.total_lobbying_expenditures_amt, 0)
                       + COALESCE(l.total_lobbying_expend_amt, 0)
                       + COALESCE(l.fees_for_services_lobbying_amt, 0)
                   ) AS lobbying_spend,
                   COUNT(*) AS filings
            FROM irs990_filing_lobbying AS l
            JOIN irs990_filings AS f USING (filing_id)
            JOIN latest_filer_names AS latest USING (ein)
            GROUP BY f.ein, latest.filer_name;
            CREATE INDEX idx_dash_political_orgs_spend
                ON dash_political_orgs(lobbying_spend DESC);
            CREATE INDEX idx_dash_political_orgs_flag
                ON dash_political_orgs(political_flag);
            """
        )
        conn.commit()
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "dash_filings_by_year",
                "dash_most_lobbied_bills",
                "dash_political_orgs",
            )
        }
        print(counts)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Materialize SQLite dashboard summary tables."
    )
    parser.add_argument("--db", type=Path, default=Path("data/irs990_full.db"))
    args = parser.parse_args()
    materialize(args.db)


if __name__ == "__main__":
    main()
