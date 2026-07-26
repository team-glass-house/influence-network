"""SQLite storage layer: schema definition + idempotent upsert helpers.

The schema is intentionally normalized but lightweight, one DB file that
all collectors write to. Raw API payloads are also stored as JSON so you can
re-parse later without re-pulling from rate-limited APIs.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import settings

# --- Schema -----------------------------------------------------------------
# Tables are grouped by source. `raw_json` columns preserve the full payload
# for re-parsing; structured columns hold the fields we join on.

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ===================== Congress.gov =====================
CREATE TABLE IF NOT EXISTS bills (
    bill_id        TEXT PRIMARY KEY,      -- e.g. "118-hr-1234"
    congress       INTEGER NOT NULL,
    bill_type      TEXT NOT NULL,         -- hr, s, hjres, ...
    bill_number    INTEGER NOT NULL,
    title          TEXT,
    introduced_date TEXT,
    latest_action  TEXT,
    policy_area    TEXT,
    raw_json       TEXT,
    fetched_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bill_actions (
    bill_id     TEXT NOT NULL REFERENCES bills(bill_id),
    action_date TEXT,
    action_code TEXT,
    action_text TEXT,
    raw_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_bill ON bill_actions(bill_id);

CREATE TABLE IF NOT EXISTS bill_sponsors (
    bill_id    TEXT NOT NULL REFERENCES bills(bill_id),
    bioguide_id TEXT,
    full_name  TEXT,
    party      TEXT,
    state      TEXT,
    is_original_cosponsor INTEGER,  -- 0 = sponsor, 1 = cosponsor
    raw_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sponsors_bill ON bill_sponsors(bill_id);

CREATE TABLE IF NOT EXISTS bill_text_versions (
    bill_id      TEXT NOT NULL REFERENCES bills(bill_id),
    version_code TEXT,                 -- e.g. "Introduced in House"
    version_date TEXT,
    format_type  TEXT,                 -- "Formatted Text", "XML", ...
    url          TEXT,
    PRIMARY KEY (bill_id, version_code, format_type)
);

CREATE TABLE IF NOT EXISTS members (
    bioguide_id TEXT PRIMARY KEY,
    full_name   TEXT,
    party       TEXT,
    state       TEXT,
    chamber     TEXT,
    raw_json    TEXT
);

-- ===================== FEC (openFEC) =====================
CREATE TABLE IF NOT EXISTS committees (
    committee_id              TEXT PRIMARY KEY,
    name                      TEXT,
    committee_type            TEXT,
    designation               TEXT,
    party                     TEXT,
    -- Parsed cycle-level totals from totals/pac-party endpoint.
    -- NULL for committees loaded via collect_committees() only.
    cycle                     INTEGER,
    total_receipts            REAL,
    total_disbursements       REAL,
    independent_expenditures  REAL,
    cash_on_hand_end_period   REAL,
    raw_json                  TEXT
);

CREATE TABLE IF NOT EXISTS fec_disbursements (
    sub_id          TEXT PRIMARY KEY,    -- FEC unique transaction id
    committee_id    TEXT,
    recipient_name  TEXT,
    disbursement_amount REAL,
    disbursement_date   TEXT,
    disbursement_description TEXT,
    raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_disb_committee ON fec_disbursements(committee_id);

-- ===================== Senate LDA =====================
CREATE TABLE IF NOT EXISTS lda_filings (
    filing_uuid    TEXT PRIMARY KEY,
    client_name    TEXT,
    registrant_name TEXT,
    filing_year    INTEGER,
    filing_period  TEXT,
    income         REAL,
    expenses       REAL,
    raw_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_lda_client ON lda_filings(client_name);

CREATE TABLE IF NOT EXISTS lda_lobbying_activities (
    filing_uuid   TEXT NOT NULL REFERENCES lda_filings(filing_uuid),
    general_issue_code TEXT,
    description   TEXT,             -- free-text issue description (NLP target)
    raw_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_lda_act_filing ON lda_lobbying_activities(filing_uuid);

-- ===================== IRS Exempt Organization Master File (BMF) =====================
-- Source: IRS Publication 78 / BMF extract (eo1.csv, eo_ca.csv).
-- One row per EIN across all IRS-recognized tax-exempt orgs (not just 990 filers).
-- Loaded via collect_irs_master() in extract/irs_master.py.
CREATE TABLE IF NOT EXISTS irs_master (
    ein             TEXT PRIMARY KEY,
    name            TEXT,
    state           TEXT,
    ntee_code       TEXT,        -- e.g. "A69Z" -- mission sector classification
    subsection_code TEXT,        -- IRS 501(c) subsection number, e.g. "3", "4", "6"
    foundation_code TEXT,        -- IRS foundation type code
    status_code     TEXT,        -- IRS status: "01"=active, "06"=terminated, etc.
    ruling_date     TEXT,        -- YYYYMM of first determination letter
    asset_code      TEXT,        -- asset size band (0-9)
    income_code     TEXT,        -- income size band (0-9)
    asset_amt       REAL,        -- most recent reported total assets (may be 0)
    income_amt      REAL,        -- most recent reported total income
    revenue_amt     REAL,        -- most recent reported total revenue
    tax_period      TEXT         -- most recent tax period on file (YYYYMM)
);
CREATE INDEX IF NOT EXISTS idx_irs_master_ntee ON irs_master(ntee_code);
CREATE INDEX IF NOT EXISTS idx_irs_master_state ON irs_master(state);

-- ===================== OpenSecrets Dark Money Crosswalk =====================
-- Source: drive/Dark Money Dataset Investigation/upd.crp_ein_list.csv
-- 371 EINs flagged by OpenSecrets as dark money / politically active nonprofits.
CREATE TABLE IF NOT EXISTS crp_dark_money (
    ein         TEXT NOT NULL,
    crp_name    TEXT,   -- name used by OpenSecrets CRP database
    org_name    TEXT,   -- IRS name on file
    year        INTEGER,
    PRIMARY KEY (ein, year)
);

"""

IRS990_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    ein             TEXT PRIMARY KEY,
    current_name    TEXT,
    normalized_name TEXT,
    first_seen_at   TEXT DEFAULT (datetime('now')),
    last_seen_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_organizations_normalized_name
    ON organizations(normalized_name);

CREATE TABLE IF NOT EXISTS irs990_source_objects (
    source_object_id TEXT PRIMARY KEY,
    file_name        TEXT NOT NULL,
    file_path        TEXT,
    content_sha256   TEXT NOT NULL,
    byte_size        INTEGER NOT NULL,
    parser_version   TEXT NOT NULL,
    ingest_status    TEXT NOT NULL,
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    first_attempt_at TEXT DEFAULT (datetime('now')),
    last_attempt_at  TEXT DEFAULT (datetime('now')),
    completed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_irs990_source_status
    ON irs990_source_objects(ingest_status, parser_version);

CREATE TABLE IF NOT EXISTS irs990_filings (
    filing_id                INTEGER PRIMARY KEY,
    source_object_id         TEXT NOT NULL UNIQUE REFERENCES irs990_source_objects(source_object_id),
    ein                      TEXT NOT NULL REFERENCES organizations(ein),
    tax_year                 INTEGER,
    tax_period_end_date      TEXT,
    return_timestamp         TEXT,
    form_type                TEXT,
    filer_name               TEXT,
    exempt_organization_type TEXT,
    total_revenue            REAL,
    total_expenses           REAL,
    total_assets             REAL,          -- EOY total assets (990/990EZ/990PF)
    voting_members_governing_body   INTEGER, -- VotingMembersGoverningBodyCnt
    voting_members_independent      INTEGER, -- VotingMembersIndependentCnt
    total_volunteers                INTEGER, -- TotalVolunteersCnt
    website                         TEXT,    -- WebsiteAddressTxt
    total_salaries                  REAL,    -- CYSalariesCompEmpBnftPaidAmt
    unrestricted_net_assets_eoy     REAL,    -- NoDonorRestrictionNetAssetsGrp/EOYAmt (new) or UnrestrictedNetAssetsGrp/EOYAmt (old)
    fundraising_expenses            REAL,    -- CYTotalProfFndrsngExpnsAmt
    political_activity_flag  INTEGER,
    mission                  TEXT,
    parsed_at                TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_irs990_filings_ein_year ON irs990_filings(ein, tax_year);
CREATE INDEX IF NOT EXISTS idx_irs990_filings_form_year ON irs990_filings(form_type, tax_year);

CREATE TABLE IF NOT EXISTS irs990_filing_grants (
    filing_id    INTEGER NOT NULL REFERENCES irs990_filings(filing_id),
    line_no      INTEGER NOT NULL,
    grantee_ein  TEXT,
    grantee_name TEXT,
    amount       REAL,
    PRIMARY KEY (filing_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_irs990_grants_grantee ON irs990_filing_grants(grantee_ein);

CREATE TABLE IF NOT EXISTS irs990_filing_people (
    filing_id INTEGER NOT NULL REFERENCES irs990_filings(filing_id),
    line_no INTEGER NOT NULL,
    person_name TEXT, title TEXT,
    is_indiv_trustee_or_director REAL, is_institutional_trustee REAL,
    is_officer REAL, is_key_employee REAL, is_highest_compensated_employee REAL,
    is_former_employee REAL, avg_weekly_hours_worked_org REAL,
    avg_weekly_hours_worked_related_org REAL, compensation_from_org REAL,
    compensation_from_related_org REAL, compensation_other REAL,
    PRIMARY KEY (filing_id, line_no)
);

CREATE TABLE IF NOT EXISTS irs990_filing_contractors (
    filing_id INTEGER NOT NULL REFERENCES irs990_filings(filing_id),
    line_no INTEGER NOT NULL,
    contractor_name TEXT, address TEXT, state TEXT, city TEXT, zip_code TEXT,
    compensation REAL, services_description TEXT,
    PRIMARY KEY (filing_id, line_no)
);

CREATE TABLE IF NOT EXISTS irs990_filing_lobbying (
    filing_id INTEGER PRIMARY KEY REFERENCES irs990_filings(filing_id),
    total_exempt_function_expend_amt REAL, total_lobbying_expend_amt REAL,
    total_exempt_purpose_expend_amt REAL, lobbying_nontaxable_amt REAL,
    grassroots_nontaxable_amt REAL, lobbying_ceiling_amt REAL,
    grassroots_ceiling_amt REAL, total_lobbying_expenditures_amt REAL,
    direct_contact_legislators_amt REAL, other_lobbying_activities_amt REAL,
    lobbying_activity_types TEXT, nondeductible_lobbying_pltcl_amt REAL,
    taxable_amt REAL
);

CREATE TABLE IF NOT EXISTS irs990_filing_527_orgs (
    filing_id INTEGER NOT NULL REFERENCES irs990_filings(filing_id),
    line_no INTEGER NOT NULL, ein TEXT, name TEXT, address TEXT, city TEXT,
    state_code TEXT, zip_code TEXT, paid_internal_funds REAL,
    contributions_transferred REAL,
    PRIMARY KEY (filing_id, line_no)
);

CREATE TABLE IF NOT EXISTS irs990_filing_related_orgs (
    filing_id INTEGER NOT NULL REFERENCES irs990_filings(filing_id),
    line_no INTEGER NOT NULL, ein TEXT, name TEXT, entity_type TEXT,
    primary_activities TEXT, direct_controlling_entity TEXT, address TEXT,
    state_code TEXT, city TEXT, zip_code TEXT,
    PRIMARY KEY (filing_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_irs990_related_org_ein ON irs990_filing_related_orgs(ein);

CREATE TABLE IF NOT EXISTS irs990_filing_related_org_transactions (
    filing_id INTEGER NOT NULL REFERENCES irs990_filings(filing_id),
    line_no INTEGER NOT NULL, related_org_name TEXT, type TEXT, amount REAL,
    amount_determination_method TEXT,
    PRIMARY KEY (filing_id, line_no)
);

-- Cross-source name matching is stored as evidence and review decisions; a
-- candidate score alone never makes two entities equivalent.
CREATE TABLE IF NOT EXISTS entity_observations (
    observation_id INTEGER PRIMARY KEY,
    source_system TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    subject_role TEXT NOT NULL,
    native_identifier TEXT,
    observed_name TEXT,
    normalized_name TEXT,
    irs_filing_id INTEGER REFERENCES irs990_filings(filing_id),
    observed_at TEXT,
    UNIQUE(source_system, source_record_id, subject_role)
);
CREATE INDEX IF NOT EXISTS idx_entity_observations_normalized_name
    ON entity_observations(normalized_name);

CREATE TABLE IF NOT EXISTS entity_match_candidates (
    candidate_id INTEGER PRIMARY KEY,
    left_observation_id INTEGER NOT NULL REFERENCES entity_observations(observation_id),
    right_observation_id INTEGER NOT NULL REFERENCES entity_observations(observation_id),
    matcher_name TEXT NOT NULL,
    score REAL NOT NULL,
    evidence_json TEXT,
    is_current INTEGER NOT NULL DEFAULT 1,
    invalidated_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    CHECK(left_observation_id < right_observation_id),
    UNIQUE(left_observation_id, right_observation_id, matcher_name)
);

CREATE TABLE IF NOT EXISTS entity_match_decisions (
    decision_id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES entity_match_candidates(candidate_id),
    decision TEXT NOT NULL CHECK(decision IN ('accepted', 'rejected', 'needs_review')),
    reviewer TEXT,
    rationale TEXT,
    decided_at TEXT DEFAULT (datetime('now'))
);
"""


@contextmanager
def connect(db_path: Path | None = None, timeout: float = 30.0) -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection, committing on success and rolling back on error.

    timeout is how long to wait for a write lock before raising. 30s works fine
    for concurrent ingestion jobs.
    """
    path = Path(db_path) if db_path else settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    """Create all tables if they do not yet exist."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.executescript(IRS990_V2_SCHEMA)
        candidate_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(entity_match_candidates)")
        }
        for column, definition in (
            ("is_current", "INTEGER NOT NULL DEFAULT 1"),
            ("invalidated_at", "TEXT"),
        ):
            if column not in candidate_columns:
                conn.execute(f"ALTER TABLE entity_match_candidates ADD COLUMN {column} {definition}")
        # Add parsed financial columns to committees for older databases.
        committee_columns = {row["name"] for row in conn.execute("PRAGMA table_info(committees)")}
        for column, definition in (
            ("cycle", "INTEGER"),
            ("total_receipts", "REAL"),
            ("total_disbursements", "REAL"),
            ("independent_expenditures", "REAL"),
            ("cash_on_hand_end_period", "REAL"),
        ):
            if column not in committee_columns:
                conn.execute(f"ALTER TABLE committees ADD COLUMN {column} {definition}")
        # Add new columns to irs990_filings (schema migrations for existing databases).
        filings_columns = {row["name"] for row in conn.execute("PRAGMA table_info(irs990_filings)")}
        for col, defn in (
            ("total_assets",                    "REAL"),
            ("voting_members_governing_body",   "INTEGER"),
            ("voting_members_independent",      "INTEGER"),
            ("total_volunteers",                "INTEGER"),
            ("website",                         "TEXT"),
            ("total_salaries",                  "REAL"),
            ("unrestricted_net_assets_eoy",     "REAL"),
            ("fundraising_expenses",            "REAL"),
        ):
            if col not in filings_columns:
                conn.execute(f"ALTER TABLE irs990_filings ADD COLUMN {col} {defn}")

        # Create irs_master and crp_dark_money if they do not exist yet.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS irs_master (
                ein TEXT PRIMARY KEY, name TEXT, state TEXT, ntee_code TEXT,
                subsection_code TEXT, foundation_code TEXT, status_code TEXT,
                ruling_date TEXT, asset_code TEXT, income_code TEXT,
                asset_amt REAL, income_amt REAL, revenue_amt REAL, tax_period TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_irs_master_ntee ON irs_master(ntee_code);
            CREATE INDEX IF NOT EXISTS idx_irs_master_state ON irs_master(state);
            CREATE TABLE IF NOT EXISTS crp_dark_money (
                ein TEXT NOT NULL, crp_name TEXT, org_name TEXT, year INTEGER,
                PRIMARY KEY (ein, year)
            );
        """)


def _coerce(value: Any) -> Any:
    """Make a Python value safe for sqlite binding (dict/list -> JSON)."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    """Insert or update a row without deleting referenced parent records."""
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    values = [_coerce(row[c]) for c in cols]
    updates = ", ".join(f"{column} = excluded.{column}" for column in cols)
    conn.execute(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT DO UPDATE SET {updates}",
        values,
    )


def insert_many(conn: sqlite3.Connection, table: str, rows: Iterable[dict[str, Any]]) -> int:
    """Insert-or-replace many rows; returns the count written."""
    count = 0
    for row in rows:
        upsert(conn, table, row)
        count += 1
    return count
