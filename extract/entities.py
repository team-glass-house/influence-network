"""Conservative organization-name normalization for cross-source matching.

Normalized names support candidate generation only. They are not identity proof;
persisted match decisions belong in the database review tables.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

# Only strip true legal suffixes and stop words.
# Content words like "foundation", "society", "institute", "action", "group",
# "company", "trust", and "fund" are intentionally kept: they distinguish
# organizations and their removal caused false exact matches (e.g.
# "THE APPLE GROUP INC" normalizing to "apple" and matching "APPLE INC.").
_NOISE = {
    # Pure legal-form suffixes: interchangeable, carry no identity signal
    "inc", "incorporated", "llc", "lp", "llp", "corp", "corporation",
    # Articles and prepositions: structural words, not identity-bearing
    "the", "of", "and", "for", "a", "an",
    # PAC/committee filing suffixes
    "pac", "cmte",
}

# Normalized strings shorter than this are too ambiguous to auto-match.
# A single shared token like "apple" or "block" is not enough evidence.
_MIN_MATCH_TOKENS = 2


def normalize_organization_name(name: str | None) -> str:
    """Return a normalized match key for candidate generation."""
    if not name:
        return ""
    tokens = re.sub(r"[^a-z0-9\s]", " ", name.lower()).split()
    return " ".join(token for token in tokens if token not in _NOISE)


def organization_name_similarity(left: str | None, right: str | None) -> float:
    """Score normalized names for review-queue ordering, from 0.0 to 1.0."""
    return SequenceMatcher(
        None, normalize_organization_name(left), normalize_organization_name(right)
    ).ratio()


def sync_entity_observations(db_path: Any = None) -> int:
    """Project IRS, FEC, and LDA names into auditable match observations."""
    from .db import connect, init_db, upsert

    init_db(db_path)
    count = 0
    with connect(db_path) as conn:
        sources = (
            ("IRS990", "filer", """
                SELECT source_object_id AS source_record_id, ein AS native_identifier,
                       filer_name AS observed_name, filing_id AS irs_filing_id,
                       return_timestamp AS observed_at
                FROM irs990_filings
            """),
            ("FEC", "committee", """
                SELECT committee_id AS source_record_id, committee_id AS native_identifier,
                       name AS observed_name, NULL AS irs_filing_id, NULL AS observed_at
                FROM committees
            """),
            ("LDA", "client", """
                SELECT filing_uuid AS source_record_id, NULL AS native_identifier,
                       client_name AS observed_name, NULL AS irs_filing_id, NULL AS observed_at
                FROM lda_filings
            """),
        )
        for source_system, subject_role, query in sources:
            for row in conn.execute(query):
                if not row["observed_name"]:
                    continue
                normalized_name = normalize_organization_name(row["observed_name"])
                existing = conn.execute(
                    "SELECT observation_id, normalized_name FROM entity_observations "
                    "WHERE source_system = ? AND source_record_id = ? AND subject_role = ?",
                    (source_system, row["source_record_id"], subject_role),
                ).fetchone()
                if existing and existing["normalized_name"] != normalized_name:
                    conn.execute("""
                        UPDATE entity_match_candidates
                        SET is_current = 0, invalidated_at = datetime('now')
                        WHERE left_observation_id = ? OR right_observation_id = ?
                    """, (existing["observation_id"], existing["observation_id"]))
                upsert(conn, "entity_observations", {
                    "source_system": source_system,
                    "source_record_id": row["source_record_id"],
                    "subject_role": subject_role,
                    "native_identifier": row["native_identifier"],
                    "observed_name": row["observed_name"],
                    "normalized_name": normalized_name,
                    "irs_filing_id": row["irs_filing_id"],
                    "observed_at": row["observed_at"],
                })
                count += 1
    return count


def generate_exact_name_match_candidates(db_path: Any = None) -> int:
    """Store normalized exact-name IRS↔FEC/LDA candidates for human review."""
    from .db import connect, upsert

    count = 0
    with connect(db_path) as conn:
        rows = conn.execute("""
            SELECT irs.observation_id AS irs_id, external.observation_id AS external_id,
                   irs.observed_name AS irs_name, external.observed_name AS external_name,
                   irs.normalized_name
            FROM entity_observations AS irs
            JOIN entity_observations AS external
              ON external.normalized_name = irs.normalized_name
            WHERE irs.source_system = 'IRS990'
              AND external.source_system IN ('FEC', 'LDA')
              AND irs.normalized_name <> ''
        """)
        for row in rows:
            # Skip matches where the shared normalized name is too short to be
            # meaningful. A single token like "apple" or "block" matching across
            # sources is not reliable evidence of shared identity.
            if len(row["normalized_name"].split()) < _MIN_MATCH_TOKENS:
                continue
            left_id, right_id = sorted((row["irs_id"], row["external_id"]))
            upsert(conn, "entity_match_candidates", {
                "left_observation_id": left_id,
                "right_observation_id": right_id,
                "matcher_name": "normalized_exact_v1",
                "score": 1.0,
                "is_current": 1,
                "invalidated_at": None,
                "evidence_json": {
                    "irs_name": row["irs_name"],
                    "external_name": row["external_name"],
                },
            })
            count += 1
    return count