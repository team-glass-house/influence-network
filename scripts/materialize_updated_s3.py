"""Build performance-critical Parquet datasets from the updated S3 export.

The dashboard reads only ``parquet_updated``. This script derives and uploads
the sorted grant datasets and reviewer-approved organization-policy links that
are intentionally not part of the raw database export.

Run:
    python scripts/materialize_updated_s3.py
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import boto3
import duckdb
import pyarrow.parquet as pq

BUCKET = "irs-990-263839540825-us-east-2-an"
PREFIX = "parquet_updated"
REGION = "us-east-2"


def sql_string(value: str) -> str:
    return value.replace("'", "''")


def connect() -> duckdb.DuckDBPyConnection:
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("AWS credentials are required")
    frozen = credentials.get_frozen_credentials()
    connection = duckdb.connect()
    connection.execute("INSTALL httpfs; LOAD httpfs;")
    secret = (
        "CREATE SECRET s3_materialize (TYPE S3, "
        f"KEY_ID '{sql_string(frozen.access_key)}', "
        f"SECRET '{sql_string(frozen.secret_key)}', "
        f"REGION '{REGION}'"
    )
    if frozen.token:
        secret += f", SESSION_TOKEN '{sql_string(frozen.token)}'"
    connection.execute(secret + ")")
    return connection


def parquet_glob(table: str) -> str:
    return f"s3://{BUCKET}/{PREFIX}/{table}/*.parquet"


def register_source(connection: duckdb.DuckDBPyConnection, table: str) -> None:
    connection.execute(
        f"CREATE VIEW {table} AS SELECT * FROM "
        f"read_parquet('{parquet_glob(table)}', union_by_name=true)"
    )


def build_views(connection: duckdb.DuckDBPyConnection) -> None:
    for table in (
        "irs990_filings",
        "irs990_filing_grants",
        "irs990_filing_people",
        "entity_observations",
        "entity_match_candidates",
        "entity_match_decisions",
        "lobbying_bill_links",
        "lda_filings",
        "lda_lobbying_activities",
        "bills",
    ):
        register_source(connection, table)

    connection.execute("""
        CREATE VIEW lobbying_bill_facts AS
        SELECT filing.filing_uuid, filing.filing_year, filing.client_name,
               filing.registrant_name,
               COALESCE(NULLIF(filing.income, 0), NULLIF(filing.expenses, 0), 0)
                   AS reported_lobbying_amount,
               link.bill_type, link.bill_number, bill.bill_id, bill.title,
               bill.policy_area, activity.general_issue_code, activity.description
        FROM lobbying_bill_links AS link
        JOIN lda_filings AS filing USING (filing_uuid)
        JOIN bills AS bill
          ON bill.bill_type = link.bill_type
         AND bill.bill_number = link.bill_number
         AND bill.congress = CAST((filing.filing_year - 1789) // 2 AS INTEGER) + 1
        LEFT JOIN lda_lobbying_activities AS activity USING (filing_uuid)
    """)
    connection.execute("""
        CREATE VIEW entity_match_candidate_diagnostics AS
        WITH latest_decisions AS (
            SELECT decision.*
            FROM entity_match_decisions AS decision
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY candidate_id ORDER BY decision_id DESC
            ) = 1
        ),
        irs_name_counts AS (
            SELECT normalized_name,
                   COUNT(DISTINCT native_identifier) AS irs_entity_count
            FROM entity_observations
            WHERE source_system = 'IRS990'
            GROUP BY normalized_name
        ),
        external_name_counts AS (
            SELECT source_system, normalized_name,
                   COUNT(DISTINCT COALESCE(
                       NULLIF(native_identifier, ''), source_record_id
                   )) AS external_entity_count
            FROM entity_observations
            WHERE source_system IN ('FEC', 'LDA')
            GROUP BY source_system, normalized_name
        )
        SELECT candidate.candidate_id, candidate.matcher_name, candidate.score,
               candidate.evidence_json, candidate.is_current,
               left_obs.source_system AS left_source_system,
               left_obs.source_record_id AS left_source_record_id,
               left_obs.native_identifier AS left_native_identifier,
               right_obs.source_system AS right_source_system,
               right_obs.source_record_id AS right_source_record_id,
               right_obs.native_identifier AS right_native_identifier,
               COALESCE(irs_left.irs_entity_count, irs_right.irs_entity_count, 0)
                   AS irs_entity_count,
               COALESCE(ext_left.external_entity_count, ext_right.external_entity_count, 0)
                   AS external_entity_count,
               CASE
                   WHEN candidate.is_current = 0 THEN 'retired'
                   WHEN left_obs.source_system = 'IRS990'
                    AND right_obs.source_system IN ('FEC', 'LDA')
                    AND json_extract(candidate.evidence_json, '$.comparison') IS NOT NULL
                    AND json_extract_string(
                        candidate.evidence_json, '$.comparison.confidence_tier'
                    ) IN ('corroborated', 'location_supported')
                    AND candidate.score >= 0.92
                    AND COALESCE(irs_left.irs_entity_count, irs_right.irs_entity_count, 0) <= 1
                    AND COALESCE(ext_left.external_entity_count, ext_right.external_entity_count, 0) <= 1
                       THEN 'eligible_for_approval'
                   ELSE 'manual_review'
               END AS approval_status,
               latest_decisions.decision AS latest_decision
        FROM entity_match_candidates AS candidate
        JOIN entity_observations AS left_obs
          ON left_obs.observation_id = candidate.left_observation_id
        JOIN entity_observations AS right_obs
          ON right_obs.observation_id = candidate.right_observation_id
        LEFT JOIN irs_name_counts AS irs_left
          ON irs_left.normalized_name = left_obs.normalized_name
         AND left_obs.source_system = 'IRS990'
        LEFT JOIN irs_name_counts AS irs_right
          ON irs_right.normalized_name = right_obs.normalized_name
         AND right_obs.source_system = 'IRS990'
        LEFT JOIN external_name_counts AS ext_left
          ON ext_left.normalized_name = left_obs.normalized_name
         AND ext_left.source_system = left_obs.source_system
        LEFT JOIN external_name_counts AS ext_right
          ON ext_right.normalized_name = right_obs.normalized_name
         AND ext_right.source_system = right_obs.source_system
        LEFT JOIN latest_decisions
          ON latest_decisions.candidate_id = candidate.candidate_id
    """)
    connection.execute("""
        CREATE VIEW approved_external_entity_links AS
        SELECT DISTINCT
               CASE WHEN left_source_system = 'IRS990'
                    THEN left_native_identifier ELSE right_native_identifier END AS ein,
               CASE WHEN left_source_system = 'IRS990'
                    THEN right_source_system ELSE left_source_system END AS external_source_system,
               CASE WHEN left_source_system = 'IRS990'
                    THEN right_source_record_id ELSE left_source_record_id END AS external_source_record_id,
               candidate_id
        FROM entity_match_candidate_diagnostics
        WHERE is_current = 1
          AND latest_decision = 'accepted'
          AND approval_status = 'eligible_for_approval'
    """)


def build_and_upload(
    connection: duckdb.DuckDBPyConnection,
    s3: boto3.client,
    output_dir: Path,
    table: str,
    query: str,
) -> None:
    output = output_dir / f"{table}.parquet"
    connection.execute(
        f"COPY ({query}) TO '{output.as_posix()}' "
        "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)"
    )
    rows = pq.read_metadata(output).num_rows
    key = f"{PREFIX}/{table}/{table}__all__0000.parquet"
    s3.upload_file(str(output), BUCKET, key)
    print(f"{table}: {rows:,} rows, {output.stat().st_size / 1e6:.1f} MB -> s3://{BUCKET}/{key}")


def build_people_indexes(
    connection: duckdb.DuckDBPyConnection,
    s3: boto3.client,
    output_dir: Path,
) -> None:
    base = output_dir / "shared_people_base.parquet"
    connection.execute(f"""
        COPY (
            SELECT DISTINCT filing.ein, filing.filer_name, person.person_name
            FROM irs990_filings AS filing
            JOIN irs990_filing_people AS person USING (filing_id)
            WHERE (person.is_officer = 1 OR person.is_indiv_trustee_or_director = 1)
              AND person.person_name IS NOT NULL
              AND length(person.person_name) > 6
        ) TO '{base.as_posix()}'
          (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
    """)
    connection.execute(
        f"CREATE VIEW shared_people_base AS SELECT * FROM read_parquet('{base.as_posix()}')"
    )
    build_and_upload(
        connection, s3, output_dir, "shared_people_by_ein",
        "SELECT * FROM shared_people_base ORDER BY ein, person_name",
    )
    build_and_upload(
        connection, s3, output_dir, "shared_people_by_name",
        "SELECT * FROM shared_people_base ORDER BY person_name, ein",
    )


def diagnose_policy_links(connection: duckdb.DuckDBPyConnection) -> None:
    checks = {
        "all decisions": """
            SELECT decision, COUNT(*) AS rows
            FROM entity_match_decisions GROUP BY decision ORDER BY decision
        """,
        "latest decisions": """
            SELECT decision, COUNT(*) AS rows
            FROM (
                SELECT decision
                FROM entity_match_decisions
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY candidate_id ORDER BY decision_id DESC
                ) = 1
            ) GROUP BY decision ORDER BY decision
        """,
        "accepted current source pairs": """
            SELECT left_obs.source_system AS left_source,
                   right_obs.source_system AS right_source,
                   COUNT(*) AS rows
            FROM entity_match_candidates AS candidate
            JOIN entity_match_decisions AS decision USING (candidate_id)
            JOIN entity_observations AS left_obs
              ON left_obs.observation_id = candidate.left_observation_id
            JOIN entity_observations AS right_obs
              ON right_obs.observation_id = candidate.right_observation_id
            WHERE candidate.is_current = 1 AND decision.decision = 'accepted'
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY candidate.candidate_id ORDER BY decision.decision_id DESC
            ) = 1
            GROUP BY left_source, right_source
            ORDER BY rows DESC
        """,
        "approved links by external source": """
            SELECT external_source_system, COUNT(*) AS rows
            FROM approved_external_entity_links
            GROUP BY external_source_system ORDER BY external_source_system
        """,
        "organization policy links": """
            SELECT COUNT(*) AS rows
            FROM approved_external_entity_links AS links
            JOIN lobbying_bill_facts AS facts
              ON links.external_source_system = 'LDA'
             AND links.external_source_record_id = facts.filing_uuid
        """,
    }
    for label, query in checks.items():
        print(f"\n{label}")
        print(connection.execute(query).fetchdf().to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--diagnose-policy", action="store_true",
        help="Print policy-link filter counts without writing or uploading data.",
    )
    parser.add_argument(
        "--networks-only", action="store_true",
        help="Build only the shared-personnel lookup indexes.",
    )
    args = parser.parse_args()
    connection = connect()
    build_views(connection)
    if args.diagnose_policy:
        diagnose_policy_links(connection)
        return
    s3 = boto3.client("s3", region_name=REGION)
    with tempfile.TemporaryDirectory(prefix="influence-materialized-") as temp:
        output_dir = Path(temp)
        if args.networks_only:
            build_people_indexes(connection, s3, output_dir)
            return
        build_and_upload(connection, s3, output_dir, "org_grants", """
            SELECT filing.ein AS grantor_ein, filing.tax_year,
                   grant_row.grantee_name, grant_row.grantee_ein, grant_row.amount
            FROM irs990_filings AS filing
            JOIN irs990_filing_grants AS grant_row USING (filing_id)
            WHERE grant_row.amount IS NOT NULL
            ORDER BY grantor_ein
        """)
        build_and_upload(connection, s3, output_dir, "grant_network_edges", """
            SELECT filing.ein AS source_ein, grant_row.grantee_ein AS target_ein,
                   'grant' AS edge_type, SUM(grant_row.amount) AS amount,
                   COUNT(*) AS supporting_rows
            FROM irs990_filings AS filing
            JOIN irs990_filing_grants AS grant_row USING (filing_id)
            WHERE grant_row.grantee_ein IS NOT NULL AND grant_row.grantee_ein <> ''
            GROUP BY filing.ein, grant_row.grantee_ein
            ORDER BY source_ein
        """)
        build_and_upload(connection, s3, output_dir, "organization_policy_links", """
            SELECT DISTINCT links.ein, links.candidate_id, facts.filing_uuid,
                   facts.filing_year, facts.client_name, facts.bill_id, facts.title,
                   facts.policy_area, facts.reported_lobbying_amount
            FROM approved_external_entity_links AS links
            JOIN lobbying_bill_facts AS facts
              ON links.external_source_system = 'LDA'
             AND links.external_source_record_id = facts.filing_uuid
            ORDER BY ein
        """)


if __name__ == "__main__":
    main()