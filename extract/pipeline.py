"""Build reviewable analysis layers for the project database."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .db import connect, init_db, upsert
from .entities import (
    compare_organization_identities,
    generate_exact_name_match_candidates,
    generate_relationship_name_match_candidates,
    sync_entity_observations,
)


@dataclass(frozen=True)
class AnalysisRefresh:
    observations_seen: int
    exact_candidates_seen: int
    fuzzy_candidates_seen: int
    lobbying_bill_links_seen: int
    relationship_candidates_seen: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def generate_fuzzy_name_match_candidates(
    db_path: Path | None = None,
    minimum_score: float = 0.92,
    minimum_anchor_length: int = 5,
    max_candidates_per_observation: int = 25,
) -> int:
    """Generate review candidates using token-blocked name similarity."""
    if not 0 < minimum_score <= 1:
        raise ValueError("minimum_score must be in (0, 1]")
    init_db(db_path)
    with connect(db_path) as conn:
        external_rows = conn.execute("""
            SELECT observation_id, source_system, observed_name, normalized_name
                   , address, city, state, zip_code
            FROM entity_observations
            WHERE source_system IN ('FEC', 'LDA')
              AND normalized_name <> ''
        """).fetchall()
        by_token: dict[str, list[Any]] = {}
        for row in external_rows:
            for token in set(row["normalized_name"].split()):
                if len(token) >= minimum_anchor_length:
                    by_token.setdefault(token, []).append(row)

        count = 0
        irs_rows = conn.execute("""
            SELECT observation_id, observed_name, normalized_name
                   , address, city, state, zip_code
            FROM entity_observations
            WHERE source_system = 'IRS990' AND normalized_name <> ''
        """)
        for irs in irs_rows:
            candidates: dict[int, Any] = {}
            for token in set(irs["normalized_name"].split()):
                if len(token) >= minimum_anchor_length:
                    for external in by_token.get(token, []):
                        candidates[external["observation_id"]] = external
            scored = []
            for external in candidates.values():
                if external["normalized_name"] == irs["normalized_name"]:
                    continue
                comparison = compare_organization_identities(
                    irs["observed_name"], external["observed_name"],
                    left_address=irs["address"], left_city=irs["city"],
                    left_state=irs["state"], left_zip_code=irs["zip_code"],
                    right_address=external["address"], right_city=external["city"],
                    right_state=external["state"], right_zip_code=external["zip_code"],
                )
                if comparison.score >= minimum_score:
                    scored.append((comparison.score, external, comparison))
            for score, external, comparison in sorted(
                scored, reverse=True, key=lambda item: item[0]
            )[
                :max_candidates_per_observation
            ]:
                left_id, right_id = sorted((irs["observation_id"], external["observation_id"]))
                upsert(conn, "entity_match_candidates", {
                    "left_observation_id": left_id,
                    "right_observation_id": right_id,
                    "matcher_name": "token_blocked_similarity_v1",
                    "score": score,
                    "is_current": 1,
                    "invalidated_at": None,
                    "evidence_json": {
                        "irs_name": irs["observed_name"],
                        "external_name": external["observed_name"],
                        "normalized_irs_name": irs["normalized_name"],
                        "normalized_external_name": external["normalized_name"],
                        "comparison": comparison.evidence,
                    },
                })
                count += 1
    return count


def record_match_decision(
    candidate_id: int,
    decision: str,
    reviewer: str | None = None,
    rationale: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Record a human decision; only current accepted candidates become joins."""
    if decision not in {"accepted", "rejected", "needs_review"}:
        raise ValueError("decision must be accepted, rejected, or needs_review")
    with connect(db_path) as conn:
        candidate = conn.execute("""
            SELECT candidate_id FROM entity_match_candidates
            WHERE candidate_id = ? AND is_current = 1
        """, (candidate_id,)).fetchone()
        if candidate is None:
            raise ValueError(f"Candidate {candidate_id} is not current")
        conn.execute("""
            INSERT INTO entity_match_decisions (candidate_id, decision, reviewer, rationale)
            VALUES (?, ?, ?, ?)
        """, (candidate_id, decision, reviewer, rationale))


def create_analysis_views(db_path: Path | None = None) -> None:
    """Create read-only views whose joins have explicit evidence rules."""
    init_db(db_path)
    with connect(db_path) as conn:
        conn.executescript("""
        DROP VIEW IF EXISTS organization_policy_links;
        DROP VIEW IF EXISTS organization_fec_disbursements;
        DROP VIEW IF EXISTS committee_spending_summary;
        DROP VIEW IF EXISTS lobbying_bill_facts;
        DROP VIEW IF EXISTS approved_external_entity_links;
        DROP VIEW IF EXISTS entity_match_candidate_diagnostics;
        DROP VIEW IF EXISTS entity_match_review_queue;
        DROP VIEW IF EXISTS grant_network_edges;
        DROP VIEW IF EXISTS related_organization_edges;
        DROP VIEW IF EXISTS org_sector_summary;

        CREATE VIEW entity_match_candidate_diagnostics AS
        WITH latest_decisions AS (
            SELECT d.* FROM entity_match_decisions AS d
            JOIN (
                SELECT candidate_id, MAX(decision_id) AS decision_id
                FROM entity_match_decisions GROUP BY candidate_id
            ) AS latest USING (candidate_id, decision_id)
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
               left_obs.observed_name AS left_name,
               left_obs.normalized_name AS left_normalized_name,
               left_obs.address AS left_address, left_obs.city AS left_city,
               left_obs.state AS left_state, left_obs.zip_code AS left_zip_code,
               right_obs.source_system AS right_source_system,
               right_obs.source_record_id AS right_source_record_id,
               right_obs.native_identifier AS right_native_identifier,
               right_obs.observed_name AS right_name,
               right_obs.normalized_name AS right_normalized_name,
               right_obs.address AS right_address, right_obs.city AS right_city,
               right_obs.state AS right_state, right_obs.zip_code AS right_zip_code,
               COALESCE(irs_left.irs_entity_count, irs_right.irs_entity_count, 0)
                   AS irs_entity_count,
               COALESCE(ext_left.external_entity_count, ext_right.external_entity_count, 0)
                   AS external_entity_count,
               CASE
                   WHEN json_extract(candidate.evidence_json, '$.comparison') IS NULL
                       THEN 'name_only'
                   ELSE COALESCE(
                       json_extract(candidate.evidence_json, '$.comparison.confidence_tier'),
                       'unclassified'
                   )
               END AS evidence_tier,
               CASE
                   WHEN COALESCE(irs_left.irs_entity_count, irs_right.irs_entity_count, 0) > 1
                     OR COALESCE(ext_left.external_entity_count, ext_right.external_entity_count, 0) > 1
                       THEN 'ambiguous_name'
                   ELSE 'unique_name'
               END AS collision_status,
               CASE
                   WHEN candidate.is_current = 0 THEN 'retired'
                   WHEN left_obs.source_system = 'IRS990'
                    AND right_obs.source_system IN ('FEC', 'LDA')
                    AND json_extract(candidate.evidence_json, '$.comparison') IS NOT NULL
                    AND json_extract(candidate.evidence_json, '$.comparison.confidence_tier')
                        IN ('corroborated', 'location_supported')
                    AND candidate.score >= 0.92
                    AND COALESCE(irs_left.irs_entity_count, irs_right.irs_entity_count, 0) <= 1
                    AND COALESCE(ext_left.external_entity_count, ext_right.external_entity_count, 0) <= 1
                       THEN 'eligible_for_approval'
                   ELSE 'manual_review'
               END AS approval_status,
               latest_decisions.decision AS latest_decision,
               latest_decisions.reviewer, latest_decisions.rationale,
               latest_decisions.decided_at
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
          ON latest_decisions.candidate_id = candidate.candidate_id;

        CREATE VIEW approved_external_entity_links AS
        SELECT DISTINCT
               CASE WHEN left_source_system = 'IRS990'
                    THEN left_native_identifier ELSE right_native_identifier END AS ein,
               CASE WHEN left_source_system = 'IRS990'
                    THEN right_source_system ELSE left_source_system END AS external_source_system,
               CASE WHEN left_source_system = 'IRS990'
                    THEN right_source_record_id ELSE left_source_record_id END AS external_source_record_id,
               candidate_id, matcher_name, score, evidence_json,
               reviewer, rationale, decided_at
        FROM entity_match_candidate_diagnostics
        WHERE is_current = 1
          AND latest_decision = 'accepted'
          AND approval_status = 'eligible_for_approval';

        -- Accepted decisions remain visible in the review queue even when
        -- evidence is weak or names collide; only eligible candidates become
        -- analysis joins.
        CREATE VIEW entity_match_review_queue AS
        SELECT candidate_id, matcher_name, score, evidence_json,
               left_source_system, left_source_record_id, left_native_identifier,
               left_name, left_address, left_city, left_state, left_zip_code,
               right_source_system, right_source_record_id, right_native_identifier,
               right_name, right_address, right_city, right_state, right_zip_code,
               irs_entity_count, external_entity_count, evidence_tier,
               collision_status, approval_status, latest_decision,
               reviewer, rationale, decided_at
        FROM entity_match_candidate_diagnostics
        WHERE is_current = 1;

        CREATE VIEW lobbying_bill_facts AS
        SELECT l.filing_uuid, l.filing_year, l.client_name, l.registrant_name,
               COALESCE(NULLIF(l.income, 0), NULLIF(l.expenses, 0), 0)
                   AS reported_lobbying_amount,
               link.bill_type, link.bill_number, bills.bill_id, bills.title,
               bills.policy_area, activity.general_issue_code, activity.description
        FROM lobbying_bill_links AS link
        JOIN lda_filings AS l ON l.filing_uuid = link.filing_uuid
        JOIN bills ON bills.bill_type = link.bill_type
          AND bills.bill_number = link.bill_number
          AND bills.congress = CAST((l.filing_year - 1789) / 2 AS INTEGER) + 1
        LEFT JOIN lda_lobbying_activities AS activity ON activity.filing_uuid = l.filing_uuid;

        CREATE VIEW organization_policy_links AS
        SELECT DISTINCT links.ein, links.candidate_id, facts.filing_uuid,
               facts.filing_year, facts.client_name, facts.bill_id, facts.title,
               facts.policy_area, facts.reported_lobbying_amount
        FROM approved_external_entity_links AS links
        JOIN lobbying_bill_facts AS facts
          ON links.external_source_system = 'LDA'
         AND links.external_source_record_id = facts.filing_uuid;

        -- Requires accepted IRS<->FEC entity match decisions to return rows.
        -- Use committee_spending_summary for a direct overview without matching.
        CREATE VIEW organization_fec_disbursements AS
        SELECT links.ein, links.candidate_id, committee.committee_id,
               committee.name AS committee_name, disbursement.sub_id,
               disbursement.disbursement_date, disbursement.recipient_name,
               disbursement.disbursement_amount,
               disbursement.disbursement_description
        FROM approved_external_entity_links AS links
        JOIN committees AS committee
          ON links.external_source_system = 'FEC'
         AND links.external_source_record_id = committee.committee_id
        JOIN fec_disbursements AS disbursement
          ON disbursement.committee_id = committee.committee_id;

        -- Direct view of Super PAC cycle-level totals. Does not require entity
        -- matching. Populated after collect_committee_totals() runs.
        CREATE VIEW committee_spending_summary AS
        SELECT committee_id, name, committee_type, cycle,
               total_receipts, total_disbursements,
               independent_expenditures, cash_on_hand_end_period
        FROM committees
        WHERE total_disbursements IS NOT NULL
        ORDER BY total_disbursements DESC;

        -- Joins irs990_filings with IRS BMF master to attach NTEE sector codes.
        -- Returns NULL ntee_code / subsection_code for orgs not in irs_master.
        -- Requires load_irs_master() to have been run (irs_master table populated).
        CREATE VIEW org_sector_summary AS
        SELECT
            f.ein,
            f.filer_name,
            m.ntee_code,
            m.subsection_code,
            m.state,
            m.asset_amt,
            m.income_amt,
            SUM(f.total_revenue)  AS total_revenue_all_years,
            SUM(f.total_expenses) AS total_expenses_all_years,
            MAX(f.tax_year)       AS latest_tax_year,
            COUNT(*)              AS filing_count
        FROM irs990_filings AS f
        LEFT JOIN irs_master AS m ON m.ein = f.ein
        GROUP BY f.ein;

        CREATE VIEW grant_network_edges AS
        SELECT filing.ein AS source_ein, grant_row.grantee_ein AS target_ein,
               'grant' AS edge_type, SUM(grant_row.amount) AS amount,
               COUNT(*) AS supporting_rows
        FROM irs990_filings AS filing
        JOIN irs990_filing_grants AS grant_row USING (filing_id)
        WHERE grant_row.grantee_ein IS NOT NULL AND grant_row.grantee_ein <> ''
        GROUP BY filing.ein, grant_row.grantee_ein;

        CREATE VIEW related_organization_edges AS
        SELECT filing.ein AS source_ein, related.ein AS target_ein,
               'related_organization' AS edge_type, COUNT(*) AS supporting_rows
        FROM irs990_filings AS filing
        JOIN irs990_filing_related_orgs AS related USING (filing_id)
        WHERE related.ein IS NOT NULL AND related.ein <> ''
        GROUP BY filing.ein, related.ein;
        """)


def _build_lobbying_bill_links(db_path: Path | None = None) -> int:
    """Extract explicit bill references from LDA activity descriptions."""
    import re
    bill_pattern = re.compile(
        r'\b(H\.?R\.?|S\.?|H\.?Res\.?|S\.?Res\.?|H\.?Con\.?Res\.?|S\.?Con\.?Res\.?)\s*(\d+)\b',
        re.IGNORECASE,
    )
    type_map = {
        "hr": "hr", "h.r.": "hr", "h.r": "hr",
        "s.": "s", "s": "s",
        "hres": "hres", "h.res.": "hres", "h.res": "hres",
        "sres": "sres", "s.res.": "sres", "s.res": "sres",
        "hconres": "hconres", "h.con.res.": "hconres",
        "sconres": "sconres", "s.con.res.": "sconres",
    }
    count = 0
    with connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS lobbying_bill_links (
                link_id INTEGER PRIMARY KEY,
                filing_uuid TEXT NOT NULL,
                bill_type TEXT NOT NULL,
                bill_number INTEGER NOT NULL,
                UNIQUE (filing_uuid, bill_type, bill_number)
            )
        """)
        activities = conn.execute(
            "SELECT filing_uuid, description FROM lda_lobbying_activities WHERE description IS NOT NULL"
        ).fetchall()
        for filing_uuid, description in activities:
            for match in bill_pattern.finditer(description):
                raw_type = match.group(1).lower().rstrip(".")
                normalized = type_map.get(raw_type.replace(".", ""), raw_type)
                bill_number = int(match.group(2))
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO lobbying_bill_links (filing_uuid, bill_type, bill_number) VALUES (?, ?, ?)",
                        (filing_uuid, normalized, bill_number),
                    )
                    count += conn.execute("SELECT changes()").fetchone()[0]
                except Exception:
                    pass
    return count


def refresh_analysis_layers(
    db_path: Path | None = None,
    include_fuzzy_candidates: bool = True,
    include_relationship_candidates: bool = False,
) -> AnalysisRefresh:
    """Refresh candidates, bill links, and analysis views."""
    observations = sync_entity_observations(db_path)
    exact = generate_exact_name_match_candidates(db_path)
    fuzzy = generate_fuzzy_name_match_candidates(db_path) if include_fuzzy_candidates else 0
    relationship = (
        generate_relationship_name_match_candidates(db_path)
        if include_relationship_candidates else 0
    )
    bill_links = _build_lobbying_bill_links(db_path)
    create_analysis_views(db_path)
    return AnalysisRefresh(
        observations, exact, fuzzy, bill_links, relationship
    )
