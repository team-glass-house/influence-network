"""Prepare a project database for influence-network analysis.

Load each source into the same SQLite database, then call
refresh_analysis_layers. It creates name-match candidates,
lobbying-to-bill links, and SQL views for notebooks and graphs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .db import connect, init_db, upsert
from .entities import (
    generate_exact_name_match_candidates,
    organization_name_similarity,
    sync_entity_observations,
)


@dataclass(frozen=True)
class AnalysisRefresh:
    observations_seen: int
    exact_candidates_seen: int
    fuzzy_candidates_seen: int
    lobbying_bill_links_seen: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def generate_fuzzy_name_match_candidates(
    db_path: Path | None = None,
    minimum_score: float = 0.92,
    minimum_anchor_length: int = 5,
    max_candidates_per_observation: int = 25,
) -> int:
    """Queue high-similarity IRS< - >external pairs behind a shared name token.

    Shared-token blocking avoids an all-pairs comparison on the IRS corpus.
    Results are evidence for review, not automatic links.
    """
    if not 0 < minimum_score <= 1:
        raise ValueError("minimum_score must be in (0, 1]")
    init_db(db_path)
    with connect(db_path) as conn:
        external_rows = conn.execute("""
            SELECT observation_id, source_system, observed_name, normalized_name
            FROM entity_observations
            WHERE source_system IN ('FEC', 'LDA') AND normalized_name <> ''
        """).fetchall()
        by_token: dict[str, list[Any]] = {}
        for row in external_rows:
            for token in set(row["normalized_name"].split()):
                if len(token) >= minimum_anchor_length:
                    by_token.setdefault(token, []).append(row)

        count = 0
        irs_rows = conn.execute("""
            SELECT observation_id, observed_name, normalized_name
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
                score = organization_name_similarity(
                    irs["observed_name"], external["observed_name"]
                )
                if score >= minimum_score:
                    scored.append((score, external))
            for score, external in sorted(scored, reverse=True, key=lambda item: item[0])[
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
        DROP VIEW IF EXISTS grant_network_edges;
        DROP VIEW IF EXISTS related_organization_edges;
        DROP VIEW IF EXISTS org_sector_summary;

        CREATE VIEW approved_external_entity_links AS
        WITH latest_decisions AS (
            SELECT d.* FROM entity_match_decisions AS d
            JOIN (
                SELECT candidate_id, MAX(decision_id) AS decision_id
                FROM entity_match_decisions GROUP BY candidate_id
            ) AS latest USING (candidate_id, decision_id)
            WHERE d.decision = 'accepted'
        )
        SELECT DISTINCT
            CASE WHEN left_obs.source_system = 'IRS990'
                 THEN left_obs.native_identifier ELSE right_obs.native_identifier END AS ein,
            CASE WHEN left_obs.source_system = 'IRS990'
                 THEN right_obs.source_system ELSE left_obs.source_system END AS external_source_system,
            CASE WHEN left_obs.source_system = 'IRS990'
                 THEN right_obs.source_record_id ELSE left_obs.source_record_id END AS external_source_record_id,
            candidate.candidate_id, candidate.matcher_name, candidate.score,
            latest_decisions.decision_id, latest_decisions.reviewer,
            latest_decisions.rationale, latest_decisions.decided_at
        FROM entity_match_candidates AS candidate
        JOIN latest_decisions ON latest_decisions.candidate_id = candidate.candidate_id
        JOIN entity_observations AS left_obs ON left_obs.observation_id = candidate.left_observation_id
        JOIN entity_observations AS right_obs ON right_obs.observation_id = candidate.right_observation_id
        WHERE (left_obs.source_system = 'IRS990' AND right_obs.source_system IN ('FEC', 'LDA'))
           OR (right_obs.source_system = 'IRS990' AND left_obs.source_system IN ('FEC', 'LDA'));

        CREATE VIEW lobbying_bill_facts AS
        SELECT l.filing_uuid, l.filing_year, l.client_name, l.registrant_name,
               COALESCE(l.income, l.expenses, 0) AS reported_lobbying_amount,
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
            COUNT(*)              AS filing_count,
            CASE WHEN dm.ein IS NOT NULL THEN 1 ELSE 0 END AS is_dark_money_flagged
        FROM irs990_filings AS f
        LEFT JOIN irs_master AS m ON m.ein = f.ein
        LEFT JOIN crp_dark_money AS dm ON dm.ein = f.ein
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
    db_path: Path | None = None, include_fuzzy_candidates: bool = True
) -> AnalysisRefresh:
    """Refresh candidates, deterministic policy links, and analysis views."""
    observations = sync_entity_observations(db_path)
    exact = generate_exact_name_match_candidates(db_path)
    fuzzy = generate_fuzzy_name_match_candidates(db_path) if include_fuzzy_candidates else 0
    bill_links = _build_lobbying_bill_links(db_path)
    create_analysis_views(db_path)
    return AnalysisRefresh(observations, exact, fuzzy, bill_links)
