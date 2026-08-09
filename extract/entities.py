"""Utilities for matching organization names across sources."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Any

# Only strip legal-form suffixes and structural words. Content words such as
# "foundation", "society", "institute", "action", "group", "company", "trust",
# and "fund" remain identity-bearing.
_NOISE = {
    "inc", "incorporated", "llc", "lp", "llp", "corp", "corporation",
    "co", "ltd", "limited", "plc", "pvt", "private", "gmbh", "ag", "sa",
    "nv", "bv", "sarl", "pte", "pty", "kk", "holdco",
    "the", "of", "and", "for", "a", "an",
    "pac", "cmte",
}

_REGIONAL_QUALIFIERS = {
    "africa", "asia", "australia", "brazil", "canada", "china", "europe",
    "france", "germany", "global", "india", "international", "japan",
    "latin", "mexico", "middle", "north", "america", "pacific", "singapore",
    "south", "korea", "uk", "united", "states", "usa", "us",
}

# Normalized strings shorter than this are too ambiguous to auto-match.
# A single shared token like "apple" or "block" is not enough evidence.
_MIN_MATCH_TOKENS = 2

_ADDRESS_EQUIVALENTS = {
    "street": "st", "avenue": "ave", "road": "rd", "boulevard": "blvd",
    "drive": "dr", "lane": "ln", "highway": "hwy", "parkway": "pkwy",
    "place": "pl", "court": "ct", "circle": "cir", "terrace": "ter",
    "trail": "trl", "suite": "ste", "apartment": "apt",
}


@dataclass(frozen=True)
class OrganizationNameProfile:
    """Name components used to generate match candidates."""

    observed_name: str
    normalized_name: str
    tokens: tuple[str, ...]
    core_tokens: tuple[str, ...]
    qualifier_tokens: tuple[str, ...]

    @property
    def core_name(self) -> str:
        return " ".join(self.core_tokens)

    @property
    def anchor_tokens(self) -> tuple[str, ...]:
        return self.core_tokens or self.tokens


@dataclass(frozen=True)
class OrganizationNameMatch:
    """Scored comparison of two organization names."""

    score: float
    relationship: str
    evidence: dict[str, Any]


def _normalize_component(value: str | None) -> str:
    if not value:
        return ""
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    tokens = re.sub(r"[^a-z0-9\s]", " ", folded.lower()).split()
    return "".join(_ADDRESS_EQUIVALENTS.get(token, token) for token in tokens)


def normalize_address(
    address: str | None,
    city: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
) -> dict[str, str]:
    """Normalize address components without treating proxy addresses as identity."""
    line = _normalize_component(address)
    city_key = _normalize_component(city)
    state_key = _normalize_component(state)
    postal_key = re.sub(r"[^0-9]", "", zip_code or "")[:5]
    return {
        "address": line,
        "city": city_key,
        "state": state_key,
        "zip_code": postal_key,
        "signature": "|".join((line, city_key, state_key, postal_key)),
    }


def compare_organization_identities(
    left_name: str | None,
    right_name: str | None,
    *,
    left_address: str | None = None,
    left_city: str | None = None,
    left_state: str | None = None,
    left_zip_code: str | None = None,
    right_address: str | None = None,
    right_city: str | None = None,
    right_state: str | None = None,
    right_zip_code: str | None = None,
) -> OrganizationNameMatch:
    """Compare names plus available location evidence.

    Location data is corroborating evidence only. Missing addresses are common,
    and an exact name remains a review candidate rather than an automatic join.
    """
    name_match = compare_organization_names(left_name, right_name)
    left_location = normalize_address(left_address, left_city, left_state, left_zip_code)
    right_location = normalize_address(right_address, right_city, right_state, right_zip_code)

    location_fields = ("address", "city", "state", "zip_code")
    available = {
        field: bool(left_location[field] and right_location[field])
        for field in location_fields
    }
    state_match = (
        left_location["state"] == right_location["state"]
        if available["state"] else None
    )
    postal_match = (
        left_location["zip_code"] == right_location["zip_code"]
        if available["zip_code"] else None
    )
    full_address_match = (
        left_location["signature"] == right_location["signature"]
        and all(available[field] for field in location_fields)
    )
    city_state_match = (
        left_location["city"] == right_location["city"]
        and left_location["state"] == right_location["state"]
        if available["city"] and available["state"] else False
    )

    if full_address_match:
        location_score = 1.0
        location_relationship = "exact_address"
    elif available["address"] and available["zip_code"] and postal_match and state_match:
        location_score = 0.85
        location_relationship = "same_postal_address"
    elif city_state_match:
        location_score = 0.6
        location_relationship = "same_city_state"
    elif state_match:
        location_score = 0.25
        location_relationship = "same_state"
    elif state_match is False or postal_match is False:
        location_score = 0.0
        location_relationship = "conflicting_location"
    else:
        location_score = None
        location_relationship = "location_unavailable"

    score = name_match.score
    if location_score is not None:
        if location_relationship == "conflicting_location":
            score = min(score, 0.84)
        elif location_relationship in {"exact_address", "same_postal_address"}:
            # A location can move a fuzzy candidate across the review threshold,
            # but it cannot rescue a poor name match by itself.
            score = 0.8 * score + 0.2 * location_score

    evidence = dict(name_match.evidence)
    evidence.update({
        "name_score": name_match.score,
        "left_address": left_location,
        "right_address": right_location,
        "location_score": location_score,
        "location_relationship": location_relationship,
        "state_match": state_match,
        "postal_match": postal_match,
        "full_address_match": full_address_match,
        "confidence_tier": (
            "conflicting_location"
            if location_relationship == "conflicting_location"
            else "corroborated"
            if location_relationship in {"exact_address", "same_postal_address"}
            else "location_supported"
            if location_relationship in {"same_city_state", "same_state"}
            else "name_only"
        ),
    })
    return OrganizationNameMatch(round(score, 6), name_match.relationship, evidence)


def _raw_payload(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _observation_identity(row: Any, source_system: str) -> dict[str, Any]:
    """Read structured identity fields, with raw-payload fallback for old DBs."""
    payload = _raw_payload(row["raw_json"] if "raw_json" in row.keys() else None)
    if source_system == "IRS990":
        return {
            "address": row["address"],
            "city": row["city"],
            "state": row["state"],
            "zip_code": row["zip_code"],
            "native_identifier": row["native_identifier"],
        }
    if source_system == "FEC":
        # FEC committee payloads expose the committee's registration state. The
        # designated-agent address is intentionally not used as the committee
        # address because it may belong to a filing service or treasurer.
        return {
            "address": None,
            "city": None,
            "state": (
                row["state"]
                or payload.get("state")
                or payload.get("committee_state")
            ),
            "zip_code": None,
            "native_identifier": row["native_identifier"],
        }
    client = payload.get("client") or {}
    return {
        "address": row["address"],
        "city": row["city"],
        "state": row["state"] or client.get("state"),
        "zip_code": row["zip_code"],
        "native_identifier": (
            row["native_identifier"]
            or client.get("client_id")
            or client.get("id")
        ),
    }


def _tokens(name: str | None) -> list[str]:
    if not name:
        return []
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    folded = folded.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9\s]", " ", folded).split()


def profile_organization_name(name: str | None) -> OrganizationNameProfile:
    """Return legal-form-stripped and qualifier-aware name components."""
    raw = name or ""
    tokens = _tokens(raw)
    normalized_tokens = [token for token in tokens if token not in _NOISE]
    qualifiers = [token for token in normalized_tokens if token in _REGIONAL_QUALIFIERS]
    core = [token for token in normalized_tokens if token not in _REGIONAL_QUALIFIERS]
    return OrganizationNameProfile(
        observed_name=raw,
        normalized_name=" ".join(normalized_tokens),
        tokens=tuple(tokens),
        core_tokens=tuple(core),
        qualifier_tokens=tuple(qualifiers),
    )


def normalize_organization_name(name: str | None) -> str:
    """Return a normalized match key for candidate generation."""
    return profile_organization_name(name).normalized_name


def compare_organization_names(
    left: str | None, right: str | None
) -> OrganizationNameMatch:
    """Compare names and classify likely relationships."""
    left_profile = profile_organization_name(left)
    right_profile = profile_organization_name(right)
    if not left_profile.normalized_name or not right_profile.normalized_name:
        return OrganizationNameMatch(0.0, "insufficient_evidence", {
            "reason": "missing_name",
        })

    left_core = left_profile.core_name or left_profile.normalized_name
    right_core = right_profile.core_name or right_profile.normalized_name
    left_set, right_set = set(left_profile.anchor_tokens), set(right_profile.anchor_tokens)
    intersection = left_set & right_set
    union = left_set | right_set
    jaccard = len(intersection) / len(union) if union else 0.0
    containment = (
        len(intersection) / min(len(left_set), len(right_set))
        if left_set and right_set else 0.0
    )
    core_similarity = SequenceMatcher(None, left_core, right_core).ratio()
    full_similarity = SequenceMatcher(
        None, left_profile.normalized_name, right_profile.normalized_name
    ).ratio()
    score = max(full_similarity, 0.6 * core_similarity + 0.4 * jaccard)

    qualifier_conflict = (
        bool(left_profile.qualifier_tokens and right_profile.qualifier_tokens)
        and set(left_profile.qualifier_tokens) != set(right_profile.qualifier_tokens)
    )
    if qualifier_conflict:
        score = min(score, 0.84)
        relationship = "possible_regional_affiliate"
    elif left_profile.qualifier_tokens != right_profile.qualifier_tokens:
        score = min(score, 0.89)
        relationship = "possible_parent_or_subsidiary"
    elif left_profile.normalized_name == right_profile.normalized_name:
        relationship = "same_normalized_name"
    elif containment >= 0.8 and core_similarity >= 0.8:
        relationship = "likely_alias"
    else:
        relationship = "similar_name"

    evidence = asdict(left_profile)
    evidence.update({
        "right_normalized_name": right_profile.normalized_name,
        "right_core_name": right_profile.core_name,
        "right_tokens": list(right_profile.tokens),
        "right_core_tokens": list(right_profile.core_tokens),
        "right_qualifier_tokens": list(right_profile.qualifier_tokens),
        "shared_anchor_tokens": sorted(intersection),
        "jaccard": round(jaccard, 6),
        "containment": round(containment, 6),
        "core_similarity": round(core_similarity, 6),
        "full_similarity": round(full_similarity, 6),
        "qualifier_conflict": qualifier_conflict,
    })
    return OrganizationNameMatch(round(score, 6), relationship, evidence)


def organization_name_similarity(left: str | None, right: str | None) -> float:
    """Score normalized names for review-queue ordering, from 0.0 to 1.0."""
    return compare_organization_names(left, right).score


def sync_entity_observations(db_path: Any = None) -> int:
    """Project source names into auditable cross-source observations."""
    from .db import connect, init_db, upsert

    init_db(db_path)
    with connect(db_path) as conn:
        conn.create_function(
            "normalize_org_name", 1, normalize_organization_name
        )
        conn.executescript("""
            DROP TABLE IF EXISTS temp.projected_entity_observations;
            CREATE TEMP TABLE projected_entity_observations (
                source_system TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                subject_role TEXT NOT NULL,
                native_identifier TEXT,
                observed_name TEXT,
                normalized_name TEXT,
                irs_filing_id INTEGER,
                observed_at TEXT,
                address TEXT,
                city TEXT,
                state TEXT,
                zip_code TEXT,
                PRIMARY KEY (source_system, source_record_id, subject_role)
            );

            INSERT INTO projected_entity_observations
            SELECT 'IRS990', f.source_object_id, 'filer', f.ein, f.filer_name,
                   normalize_org_name(f.filer_name), f.filing_id, f.return_timestamp,
                   COALESCE(f.filer_address, m.address),
                   COALESCE(f.filer_city, m.city),
                   COALESCE(f.filer_state, m.state),
                   COALESCE(f.filer_zip_code, m.zip_code)
            FROM irs990_filings AS f
            LEFT JOIN irs_master AS m ON m.ein = f.ein
            WHERE f.filer_name IS NOT NULL;

            INSERT INTO projected_entity_observations
            SELECT 'FEC', committee_id, 'committee', committee_id, name,
                   normalize_org_name(name), NULL, NULL, NULL, NULL,
                   COALESCE(
                       state,
                       json_extract(raw_json, '$.state'),
                       json_extract(raw_json, '$.committee_state')
                   ),
                   NULL
            FROM committees
            WHERE name IS NOT NULL;

            INSERT INTO projected_entity_observations
            SELECT 'LDA', filing_uuid, 'client',
                   COALESCE(
                       client_id,
                       CAST(json_extract(raw_json, '$.client.client_id') AS TEXT),
                       CAST(json_extract(raw_json, '$.client.id') AS TEXT)
                   ),
                   client_name, normalize_org_name(client_name), NULL, NULL,
                   client_address, client_city,
                   COALESCE(client_state, json_extract(raw_json, '$.client.state')),
                   client_zip_code
            FROM lda_filings
            WHERE client_name IS NOT NULL;

            DROP TABLE IF EXISTS temp.changed_entity_observations;
            CREATE TEMP TABLE changed_entity_observations AS
            SELECT existing.observation_id
            FROM entity_observations AS existing
            JOIN projected_entity_observations AS projected
              ON projected.source_system = existing.source_system
             AND projected.source_record_id = existing.source_record_id
             AND projected.subject_role = existing.subject_role
            WHERE existing.observed_name IS NOT projected.observed_name
               OR existing.normalized_name IS NOT projected.normalized_name
               OR existing.native_identifier IS NOT projected.native_identifier
               OR existing.address IS NOT projected.address
               OR existing.city IS NOT projected.city
               OR existing.state IS NOT projected.state
               OR existing.zip_code IS NOT projected.zip_code;

            UPDATE entity_match_candidates
            SET is_current = 0, invalidated_at = datetime('now')
            WHERE is_current = 1
              AND EXISTS (
                  SELECT 1 FROM changed_entity_observations AS changed
                  WHERE changed.observation_id = entity_match_candidates.left_observation_id
                     OR changed.observation_id = entity_match_candidates.right_observation_id
              );

            INSERT INTO entity_match_decisions
                (candidate_id, decision, reviewer, rationale)
            SELECT DISTINCT candidate.candidate_id, 'needs_review', 'system',
                   'Source identity fields changed; previous acceptance invalidated.'
            FROM entity_match_candidates AS candidate
            JOIN changed_entity_observations AS changed
              ON changed.observation_id = candidate.left_observation_id
              OR changed.observation_id = candidate.right_observation_id
            JOIN (
                SELECT candidate_id, MAX(decision_id) AS decision_id
                FROM entity_match_decisions
                GROUP BY candidate_id
            ) AS latest ON latest.candidate_id = candidate.candidate_id
            JOIN entity_match_decisions AS decision
              ON decision.candidate_id = latest.candidate_id
             AND decision.decision_id = latest.decision_id
            WHERE decision.decision = 'accepted';
        """)
        conn.execute("""
            INSERT INTO entity_observations (
                source_system, source_record_id, subject_role, native_identifier,
                observed_name, normalized_name, irs_filing_id, observed_at,
                address, city, state, zip_code
            )
            SELECT source_system, source_record_id, subject_role, native_identifier,
                   observed_name, normalized_name, irs_filing_id, observed_at,
                   address, city, state, zip_code
            FROM projected_entity_observations
            WHERE 1
            ON CONFLICT(source_system, source_record_id, subject_role) DO UPDATE SET
                native_identifier = excluded.native_identifier,
                observed_name = excluded.observed_name,
                normalized_name = excluded.normalized_name,
                irs_filing_id = excluded.irs_filing_id,
                observed_at = excluded.observed_at,
                address = excluded.address,
                city = excluded.city,
                state = excluded.state,
                zip_code = excluded.zip_code
        """)
        return conn.execute(
            "SELECT COUNT(*) FROM projected_entity_observations"
        ).fetchone()[0]


def generate_exact_name_match_candidates(db_path: Any = None) -> int:
    """Store normalized exact-name candidates for human review."""
    from .db import connect, init_db, upsert

    count = 0
    with connect(db_path) as conn:
        rows = conn.execute("""
            SELECT irs.observation_id AS irs_id, external.observation_id AS external_id,
                   irs.observed_name AS irs_name, external.observed_name AS external_name,
                   irs.normalized_name,
                   irs.address AS irs_address, irs.city AS irs_city,
                   irs.state AS irs_state, irs.zip_code AS irs_zip_code,
                   external.address AS external_address, external.city AS external_city,
                   external.state AS external_state, external.zip_code AS external_zip_code
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
            comparison = compare_organization_identities(
                row["irs_name"], row["external_name"],
                left_address=row["irs_address"], left_city=row["irs_city"],
                left_state=row["irs_state"], left_zip_code=row["irs_zip_code"],
                right_address=row["external_address"], right_city=row["external_city"],
                right_state=row["external_state"], right_zip_code=row["external_zip_code"],
            )
            upsert(conn, "entity_match_candidates", {
                "left_observation_id": left_id,
                "right_observation_id": right_id,
                "matcher_name": "normalized_exact_v1",
                "score": comparison.score,
                "is_current": 1,
                "invalidated_at": None,
                "evidence_json": {
                    "irs_name": row["irs_name"],
                    "external_name": row["external_name"],
                    "comparison": comparison.evidence,
                },
            })
            count += 1
    return count


def generate_relationship_name_match_candidates(
    db_path: Any = None,
    minimum_score: float = 0.78,
    minimum_anchor_length: int = 5,
    max_candidates_per_observation: int = 25,
) -> int:
    """Generate review candidates for possible organization relationships."""
    if not 0 < minimum_score <= 1:
        raise ValueError("minimum_score must be in (0, 1]")
    from .db import connect, init_db, upsert

    init_db(db_path)
    with connect(db_path) as conn:
        external_rows = conn.execute("""
            SELECT observation_id, source_system, observed_name, normalized_name,
                   address, city, state, zip_code
            FROM entity_observations
            WHERE source_system IN ('FEC', 'LDA')
              AND normalized_name <> ''
        """).fetchall()
        by_token: dict[str, list[Any]] = {}
        for row in external_rows:
            for token in set(profile_organization_name(row["observed_name"]).anchor_tokens):
                if len(token) >= minimum_anchor_length:
                    by_token.setdefault(token, []).append(row)

        count = 0
        irs_rows = conn.execute("""
            SELECT observation_id, observed_name, normalized_name,
                   address, city, state, zip_code
            FROM entity_observations
            WHERE source_system = 'IRS990' AND normalized_name <> ''
        """)
        for irs in irs_rows:
            profile = profile_organization_name(irs["observed_name"])
            candidates: dict[int, Any] = {}
            for token in set(profile.anchor_tokens):
                if len(token) >= minimum_anchor_length:
                    for external in by_token.get(token, []):
                        candidates[external["observation_id"]] = external
            scored = []
            for external in candidates.values():
                comparison = compare_organization_identities(
                    irs["observed_name"], external["observed_name"],
                    left_address=irs["address"], left_city=irs["city"],
                    left_state=irs["state"], left_zip_code=irs["zip_code"],
                    right_address=external["address"], right_city=external["city"],
                    right_state=external["state"], right_zip_code=external["zip_code"],
                )
                if (
                    external["normalized_name"] != irs["normalized_name"]
                    and comparison.score >= minimum_score
                    and comparison.relationship != "similar_name"
                ):
                    scored.append((comparison.score, external, comparison))
            for score, external, comparison in sorted(
                scored, key=lambda item: item[0], reverse=True
            )[:max_candidates_per_observation]:
                left_id, right_id = sorted((irs["observation_id"], external["observation_id"]))
                upsert(conn, "entity_match_candidates", {
                    "left_observation_id": left_id,
                    "right_observation_id": right_id,
                    "matcher_name": "relationship_similarity_v1",
                    "score": score,
                    "is_current": 1,
                    "invalidated_at": None,
                    "evidence_json": {
                        "irs_name": irs["observed_name"],
                        "external_name": external["observed_name"],
                        "relationship": comparison.relationship,
                        "comparison": comparison.evidence,
                    },
                })
                count += 1
    return count