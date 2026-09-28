from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable
from uuid import uuid4

from ..models import (
    CanonicalPIRecord,
    EmailEvidence,
    PIContactVerdict,
    stable_id,
    utc_now_iso,
)
from ..storage import _identity_index_entries, dedupe_key_for_record
from ..verify.confidence import contact_verdict_for_pi


_CANONICAL_FIELDS = {item.name for item in fields(CanonicalPIRecord)}
_REQUIRED_IDENTITY_FIELDS = {
    "display_name",
    "given_name",
    "family_name",
    "aliases",
    "department",
    "departments",
    "title",
    "profile_url",
    "profile_urls",
    "lab_url",
    "emails",
    "research_areas",
    "external_ids",
    "field_sources",
    "email_association",
}


@dataclass(frozen=True)
class _IdentitySpec:
    person_id: str
    source_urls: tuple[str, ...]
    record: dict[str, Any]


@dataclass(frozen=True)
class _SplitManifest:
    split_id: str
    institution_id: str
    reason: str
    evidence: str
    retained: _IdentitySpec
    separated: _IdentitySpec
    stable_name: str
    stable_profile_url: str
    minimum_moved_observations: int
    minimum_moved_fingerprints: int
    expected_cohort_delta: int


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Reviewed identity split manifest is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Reviewed identity split manifest root must be an object")
    return payload, raw


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _identity_spec(payload: Any, field: str) -> _IdentitySpec:
    if not isinstance(payload, dict):
        raise ValueError(f"{field} must be an object")
    person_id = _required_text(payload.get("person_id"), f"{field}.person_id")
    raw_urls = payload.get("source_urls")
    if not isinstance(raw_urls, list) or not raw_urls:
        raise ValueError(f"{field}.source_urls must be a non-empty array")
    source_urls = tuple(_required_text(value, f"{field}.source_urls") for value in raw_urls)
    if len(source_urls) != len(set(source_urls)):
        raise ValueError(f"{field}.source_urls contains duplicates")
    record = payload.get("record")
    if not isinstance(record, dict):
        raise ValueError(f"{field}.record must be an object")
    missing = sorted(_REQUIRED_IDENTITY_FIELDS - set(record))
    unknown = sorted(set(record) - _CANONICAL_FIELDS)
    if missing:
        raise ValueError(f"{field}.record is missing required fields: {missing}")
    if unknown:
        raise ValueError(f"{field}.record has unknown canonical fields: {unknown}")
    return _IdentitySpec(person_id=person_id, source_urls=source_urls, record=dict(record))


def _load_manifest(path: Path) -> tuple[_SplitManifest, dict[str, Any], str]:
    payload, raw = _read_json(path)
    if payload.get("schema_version") != 1:
        raise ValueError("Reviewed identity split manifest schema_version must be 1")
    if payload.get("operation") != "reviewed_identity_split":
        raise ValueError("Reviewed identity split manifest operation is not reviewed_identity_split")
    retained = _identity_spec(payload.get("retained_identity"), "retained_identity")
    separated = _identity_spec(payload.get("separated_identity"), "separated_identity")
    if retained.person_id == separated.person_id:
        raise ValueError("The retained and separated person IDs must differ")
    if set(retained.source_urls).intersection(separated.source_urls):
        raise ValueError("Retained and separated source URL partitions must be disjoint")
    stable_components = payload.get("stable_person_id")
    if not isinstance(stable_components, dict):
        raise ValueError("stable_person_id must be an object")
    stable_name = _required_text(stable_components.get("normalized_name"), "stable_person_id.normalized_name")
    stable_profile = _required_text(stable_components.get("profile_url"), "stable_person_id.profile_url")
    institution_id = _required_text(payload.get("institution_id"), "institution_id")
    expected_person_id = stable_id("pi", institution_id, stable_name, stable_profile)
    if expected_person_id != separated.person_id:
        raise ValueError(
            "Separated person_id is not the stable canonical ID derived from the reviewed "
            f"institution/name/profile tuple: expected {expected_person_id}"
        )
    if separated.record.get("profile_url") != stable_profile:
        raise ValueError("Separated canonical profile_url differs from stable_person_id.profile_url")
    expected = payload.get("expected") or {}
    if not isinstance(expected, dict):
        raise ValueError("expected must be an object")
    manifest = _SplitManifest(
        split_id=_required_text(payload.get("split_id"), "split_id"),
        institution_id=institution_id,
        reason=_required_text(payload.get("reason"), "reason"),
        evidence=_required_text(payload.get("evidence"), "evidence"),
        retained=retained,
        separated=separated,
        stable_name=stable_name,
        stable_profile_url=stable_profile,
        minimum_moved_observations=int(expected.get("minimum_moved_observations", 1)),
        minimum_moved_fingerprints=int(expected.get("minimum_moved_fingerprints", 1)),
        expected_cohort_delta=int(expected.get("cohort_delta", 0)),
    )
    if manifest.minimum_moved_observations < 1 or manifest.minimum_moved_fingerprints < 0:
        raise ValueError("Expected minimum row counts must be non-negative")
    return manifest, payload, hashlib.sha256(raw).hexdigest()


def _same_file(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return left.samefile(right)
    except OSError:
        return False


def _connect(database: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(database, timeout=30.0)
        connection.execute("PRAGMA busy_timeout=30000")
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _placeholders(values: Iterable[Any]) -> str:
    return ",".join("?" for _value in values)


def _canonical_record(connection: sqlite3.Connection, person_id: str) -> CanonicalPIRecord | None:
    row = connection.execute(
        "SELECT record_json FROM canonical_pi_records WHERE person_id=?", (person_id,)
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["record_json"])
    return CanonicalPIRecord(**{key: payload[key] for key in _CANONICAL_FIELDS if key in payload})


def _cohort_size(connection: sqlite3.Connection, institution_id: str) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT c.person_id)
            FROM canonical_pi_records AS c
            JOIN official_publication_fingerprints AS f USING(person_id)
            WHERE c.institution_id=? AND COALESCE(c.membership_status, 'active')='active'
            """,
            (institution_id,),
        ).fetchone()[0]
    )


def _count_by_sources(
    connection: sqlite3.Connection,
    table: str,
    person_id: str,
    source_urls: tuple[str, ...],
) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE person_id=? AND source_url IN ({_placeholders(source_urls)})",
            (person_id, *source_urls),
        ).fetchone()[0]
    )


def _audit_row(connection: sqlite3.Connection, split_id: str) -> sqlite3.Row | None:
    if not _table_exists(connection, "reviewed_identity_splits"):
        return None
    return connection.execute(
        "SELECT * FROM reviewed_identity_splits WHERE split_id=?", (split_id,)
    ).fetchone()


def _assert_record_matches(record: CanonicalPIRecord, spec: _IdentitySpec, label: str) -> None:
    for field in _REQUIRED_IDENTITY_FIELDS:
        if getattr(record, field) != spec.record[field]:
            raise ValueError(f"Already-applied {label} canonical field differs: {field}")


def _validate_already_applied(
    connection: sqlite3.Connection,
    manifest: _SplitManifest,
    manifest_sha256: str,
    audit: sqlite3.Row,
) -> dict[str, Any]:
    if audit["manifest_sha256"] != manifest_sha256:
        raise ValueError(f"split_id {manifest.split_id!r} was already used by a different manifest")
    retained = _canonical_record(connection, manifest.retained.person_id)
    separated = _canonical_record(connection, manifest.separated.person_id)
    if retained is None or separated is None:
        raise ValueError("Split audit exists but one canonical identity is missing")
    _assert_record_matches(retained, manifest.retained, "retained")
    _assert_record_matches(separated, manifest.separated, "separated")
    alias = connection.execute(
        "SELECT 1 FROM pi_identity_aliases WHERE alias_person_id=?",
        (manifest.separated.person_id,),
    ).fetchone()
    if alias is not None:
        raise ValueError("Separated canonical ID still exists as an identity alias")
    moved_observations = _count_by_sources(
        connection,
        "pi_observations",
        manifest.separated.person_id,
        manifest.separated.source_urls,
    )
    old_moved_observations = _count_by_sources(
        connection,
        "pi_observations",
        manifest.retained.person_id,
        manifest.separated.source_urls,
    )
    moved_fingerprints = _count_by_sources(
        connection,
        "official_publication_fingerprints",
        manifest.separated.person_id,
        manifest.separated.source_urls,
    )
    old_moved_fingerprints = _count_by_sources(
        connection,
        "official_publication_fingerprints",
        manifest.retained.person_id,
        manifest.separated.source_urls,
    )
    if (
        old_moved_observations
        or old_moved_fingerprints
        or moved_observations < manifest.minimum_moved_observations
        or moved_fingerprints < manifest.minimum_moved_fingerprints
    ):
        raise ValueError("Split audit exists but the source partition is incomplete")
    return {
        "status": "already_applied",
        "moved_observations": moved_observations,
        "moved_fingerprints": moved_fingerprints,
    }


def _preflight(
    connection: sqlite3.Connection,
    manifest: _SplitManifest,
    manifest_sha256: str,
) -> dict[str, Any]:
    required_tables = {
        "canonical_pi_records",
        "person_evidence",
        "pi_observations",
        "email_evidence",
        "official_publication_fingerprints",
        "pi_identity_aliases",
        "pi_identity_keys",
    }
    missing_tables = sorted(table for table in required_tables if not _table_exists(connection, table))
    if missing_tables:
        raise ValueError(f"Target database is missing required PI Pool tables: {missing_tables}")
    audit = _audit_row(connection, manifest.split_id)
    if audit is not None:
        return _validate_already_applied(connection, manifest, manifest_sha256, audit)

    retained = _canonical_record(connection, manifest.retained.person_id)
    if retained is None:
        raise ValueError(f"Merged canonical person is missing: {manifest.retained.person_id}")
    if retained.institution_id != manifest.institution_id:
        raise ValueError("Merged canonical person belongs to a different institution")
    if _canonical_record(connection, manifest.separated.person_id) is not None:
        raise ValueError("Separated target ID is already a canonical record without this split audit")
    alias = connection.execute(
        "SELECT canonical_person_id FROM pi_identity_aliases WHERE alias_person_id=?",
        (manifest.separated.person_id,),
    ).fetchone()
    if alias is None or alias["canonical_person_id"] != manifest.retained.person_id:
        raise ValueError("Separated stable ID is not the reviewed alias of the merged canonical person")

    all_urls = set(manifest.retained.source_urls).union(manifest.separated.source_urls)
    observed_urls = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT source_url FROM pi_observations WHERE person_id=?",
            (manifest.retained.person_id,),
        )
    }
    unexpected = sorted(observed_urls - all_urls)
    if unexpected:
        raise ValueError(f"Manifest does not partition all merged observations: {unexpected}")
    missing_move_urls = sorted(
        set(manifest.separated.source_urls).difference(observed_urls)
    )
    missing_retain_urls = sorted(set(manifest.retained.source_urls).difference(observed_urls))
    if missing_move_urls or missing_retain_urls:
        raise ValueError(
            f"Manifest source URLs are absent (separated={missing_move_urls}, retained={missing_retain_urls})"
        )

    moved_observations = _count_by_sources(
        connection,
        "pi_observations",
        manifest.retained.person_id,
        manifest.separated.source_urls,
    )
    moved_fingerprints = _count_by_sources(
        connection,
        "official_publication_fingerprints",
        manifest.retained.person_id,
        manifest.separated.source_urls,
    )
    if moved_observations < manifest.minimum_moved_observations:
        raise ValueError("Fewer separated observations were found than the reviewed minimum")
    if moved_fingerprints < manifest.minimum_moved_fingerprints:
        raise ValueError("Fewer separated publication fingerprints were found than the reviewed minimum")

    for label, spec in (("retained", manifest.retained), ("separated", manifest.separated)):
        placeholders = _placeholders(spec.source_urls)
        evidence_rows = connection.execute(
            f"SELECT field_name, field_value FROM person_evidence WHERE source_url IN ({placeholders})",
            spec.source_urls,
        ).fetchall()
        observed = {(str(row["field_name"]), str(row["field_value"])) for row in evidence_rows}
        for field in ("display_name", "department", "profile_url"):
            if (field, str(spec.record[field])) not in observed:
                raise ValueError(f"Reviewed {label} {field} is not backed by selected person evidence")
        if not any(("emails", email) in observed for email in spec.record["emails"]):
            raise ValueError(f"Reviewed {label} email is not backed by selected person evidence")

    retained_fingerprints = _count_by_sources(
        connection,
        "official_publication_fingerprints",
        manifest.retained.person_id,
        manifest.retained.source_urls,
    )
    cohort_before = _cohort_size(connection, manifest.institution_id)
    cohort_delta = int(bool(moved_fingerprints and retained_fingerprints))
    if cohort_delta != manifest.expected_cohort_delta:
        raise ValueError(
            f"Planned fingerprint partition changes cohort by {cohort_delta}, expected {manifest.expected_cohort_delta}"
        )
    claims = (
        _count_by_sources(
            connection,
            "official_publication_source_claims",
            manifest.retained.person_id,
            manifest.separated.source_urls,
        )
        if _table_exists(connection, "official_publication_source_claims")
        else 0
    )
    refresh_states = (
        _count_by_sources(
            connection,
            "official_publication_refresh_state",
            manifest.retained.person_id,
            manifest.separated.source_urls,
        )
        if _table_exists(connection, "official_publication_refresh_state")
        else 0
    )
    evidence_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM person_evidence WHERE source_url IN ({_placeholders(manifest.separated.source_urls)})",
            manifest.separated.source_urls,
        ).fetchone()[0]
    )
    raw_source_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM raw_sources WHERE institution_id=? AND source_url IN ({_placeholders(manifest.separated.source_urls)})",
            (manifest.institution_id, *manifest.separated.source_urls),
        ).fetchone()[0]
    ) if _table_exists(connection, "raw_sources") else 0
    openalex_links = int(
        connection.execute(
            "SELECT COUNT(*) FROM openalex_author_links WHERE person_id=?",
            (manifest.retained.person_id,),
        ).fetchone()[0]
    ) if _table_exists(connection, "openalex_author_links") else 0
    openalex_works = int(
        connection.execute(
            "SELECT COUNT(*) FROM openalex_person_works WHERE person_id=?",
            (manifest.retained.person_id,),
        ).fetchone()[0]
    ) if _table_exists(connection, "openalex_person_works") else 0
    return {
        "status": "planned",
        "moved_observations": moved_observations,
        "moved_fingerprints": moved_fingerprints,
        "moved_claims": claims,
        "moved_refresh_states": refresh_states,
        "separated_person_evidence_links": evidence_count,
        "raw_source_rows_preserved": raw_source_count,
        "openalex_links_retained_on_original": openalex_links,
        "openalex_works_retained_on_original": openalex_works,
        "cohort_before": cohort_before,
        "cohort_after": cohort_before + cohort_delta,
        "cohort_delta": cohort_delta,
        "canonical_delta": 1,
    }


def _replace_payload_identity(payload_text: str, **updates: str) -> str:
    payload = json.loads(payload_text or "{}")
    payload.update(updates)
    return _canonical_json(payload)


def _move_observations(
    connection: sqlite3.Connection,
    old_person_id: str,
    new_person_id: str,
    source_urls: tuple[str, ...],
) -> None:
    rows = connection.execute(
        f"SELECT * FROM pi_observations WHERE person_id=? AND source_url IN ({_placeholders(source_urls)}) ORDER BY observation_id",
        (old_person_id, *source_urls),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["record_json"])
        observation = payload.get("observation") or {}
        new_observation_id = stable_id(
            "obs", new_person_id, row["run_id"], row["source_url"], _canonical_json(observation)
        )
        if (
            new_observation_id != row["observation_id"]
            and connection.execute(
                "SELECT 1 FROM pi_observations WHERE observation_id=?", (new_observation_id,)
            ).fetchone()
        ):
            raise ValueError(f"Separated observation ID already exists: {new_observation_id}")
        payload["observation_id"] = new_observation_id
        payload["person_id"] = new_person_id
        connection.execute("DELETE FROM pi_observations WHERE observation_id=?", (row["observation_id"],))
        connection.execute(
            """
            INSERT INTO pi_observations
            (observation_id, person_id, institution_id, run_id, source_url, observed_at, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_observation_id,
                new_person_id,
                row["institution_id"],
                row["run_id"],
                row["source_url"],
                row["observed_at"],
                _canonical_json(payload),
            ),
        )


def _move_fingerprints(
    connection: sqlite3.Connection,
    old_person_id: str,
    new_person_id: str,
    source_urls: tuple[str, ...],
) -> None:
    rows = connection.execute(
        f"SELECT * FROM official_publication_fingerprints WHERE person_id=? AND source_url IN ({_placeholders(source_urls)}) ORDER BY fingerprint_id",
        (old_person_id, *source_urls),
    ).fetchall()
    for row in rows:
        identity_key = (row["doi"] or "").strip().casefold()
        if not identity_key:
            identity_key = f"{' '.join(row['title'].casefold().split())}|{row['publication_year'] or ''}"
        new_fingerprint_id = stable_id("pubfp", new_person_id, identity_key)
        if connection.execute(
            "SELECT 1 FROM official_publication_fingerprints WHERE fingerprint_id=?",
            (new_fingerprint_id,),
        ).fetchone():
            raise ValueError(f"Separated publication fingerprint already exists: {new_fingerprint_id}")
        payload = json.loads(row["record_json"])
        payload["fingerprint_id"] = new_fingerprint_id
        payload["person_id"] = new_person_id
        connection.execute(
            """
            INSERT INTO official_publication_fingerprints
            (fingerprint_id, person_id, institution_id, title, citation_text,
             publication_year, doi, publication_url, source_url, confidence,
             first_seen_at, last_seen_at, last_seen_run_id, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_fingerprint_id,
                new_person_id,
                row["institution_id"],
                row["title"],
                row["citation_text"],
                row["publication_year"],
                row["doi"],
                row["publication_url"],
                row["source_url"],
                row["confidence"],
                row["first_seen_at"],
                row["last_seen_at"],
                row["last_seen_run_id"],
                _canonical_json(payload),
            ),
        )
        claims = (
            connection.execute(
                "SELECT * FROM official_publication_source_claims WHERE fingerprint_id=?",
                (row["fingerprint_id"],),
            ).fetchall()
            if _table_exists(connection, "official_publication_source_claims")
            else []
        )
        for claim in claims:
            claim_payload = json.loads(claim["record_json"])
            claim_payload["fingerprint_id"] = new_fingerprint_id
            claim_payload["person_id"] = new_person_id
            connection.execute(
                """
                INSERT INTO official_publication_source_claims
                (fingerprint_id, person_id, institution_id, source_url, source_kind,
                 claim_status, missing_streak, first_seen_at, last_seen_at,
                 last_seen_run_id, last_checked_at, tombstoned_at, record_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_fingerprint_id,
                    new_person_id,
                    claim["institution_id"],
                    claim["source_url"],
                    claim["source_kind"],
                    claim["claim_status"],
                    claim["missing_streak"],
                    claim["first_seen_at"],
                    claim["last_seen_at"],
                    claim["last_seen_run_id"],
                    claim["last_checked_at"],
                    claim["tombstoned_at"],
                    _canonical_json(claim_payload),
                ),
            )
        if _table_exists(connection, "official_publication_source_claims"):
            connection.execute(
                "DELETE FROM official_publication_source_claims WHERE fingerprint_id=?",
                (row["fingerprint_id"],),
            )
        connection.execute(
            "DELETE FROM official_publication_fingerprints WHERE fingerprint_id=?",
            (row["fingerprint_id"],),
        )


def _move_refresh_state(
    connection: sqlite3.Connection,
    old_person_id: str,
    new_person_id: str,
    source_urls: tuple[str, ...],
) -> None:
    if not _table_exists(connection, "official_publication_refresh_state"):
        return
    rows = connection.execute(
        f"SELECT * FROM official_publication_refresh_state WHERE person_id=? AND source_url IN ({_placeholders(source_urls)})",
        (old_person_id, *source_urls),
    ).fetchall()
    columns = [str(item[1]) for item in connection.execute("PRAGMA table_info(official_publication_refresh_state)")]
    for row in rows:
        values = dict(row)
        values["person_id"] = new_person_id
        payload = json.loads(values.get("record_json") or "{}")
        payload["person_id"] = new_person_id
        values["record_json"] = _canonical_json(payload)
        connection.execute(
            "DELETE FROM official_publication_refresh_state WHERE person_id=? AND source_kind=? AND source_url=?",
            (old_person_id, row["source_kind"], row["source_url"]),
        )
        connection.execute(
            f"INSERT INTO official_publication_refresh_state ({','.join(columns)}) VALUES ({_placeholders(columns)})",
            tuple(values[column] for column in columns),
        )


def _partition_email_evidence(
    connection: sqlite3.Connection,
    manifest: _SplitManifest,
) -> tuple[int, int]:
    all_urls = (*manifest.retained.source_urls, *manifest.separated.source_urls)
    rows = connection.execute(
        f"SELECT * FROM email_evidence WHERE person_id IN (?, ?) AND source_url IN ({_placeholders(all_urls)})",
        (manifest.retained.person_id, manifest.separated.person_id, *all_urls),
    ).fetchall()
    connection.execute(
        f"DELETE FROM email_evidence WHERE person_id IN (?, ?) AND source_url IN ({_placeholders(all_urls)})",
        (manifest.retained.person_id, manifest.separated.person_id, *all_urls),
    )
    retained_urls = set(manifest.retained.source_urls)
    allowed = {
        manifest.retained.person_id: {value.casefold() for value in manifest.retained.record["emails"]},
        manifest.separated.person_id: {value.casefold() for value in manifest.separated.record["emails"]},
    }
    inserted = 0
    removed = 0
    for row in rows:
        target_id = (
            manifest.retained.person_id
            if row["source_url"] in retained_urls
            else manifest.separated.person_id
        )
        if str(row["email"]).casefold() not in allowed[target_id]:
            removed += 1
            continue
        payload = json.loads(row["record_json"])
        payload["person_id"] = target_id
        connection.execute(
            """
            INSERT INTO email_evidence
            (email, source_url, person_id, source_type, domain_aligned,
             official_source, extracted_at, confidence, verdict, association,
             record_json, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["email"],
                row["source_url"],
                target_id,
                row["source_type"],
                row["domain_aligned"],
                row["official_source"],
                row["extracted_at"],
                row["confidence"],
                row["verdict"],
                row["association"],
                _canonical_json(payload),
                row["run_id"],
            ),
        )
        inserted += 1
    return inserted, removed


def _publication_summary(connection: sqlite3.Connection, person_id: str) -> dict[str, Any]:
    if not _table_exists(connection, "official_publication_source_claims"):
        row = connection.execute(
            """
            SELECT COUNT(*) n, MAX(publication_year) latest_year,
                   SUM(CASE WHEN doi IS NOT NULL AND doi!='' THEN 1 ELSE 0 END) doi_count
            FROM official_publication_fingerprints WHERE person_id=?
            """,
            (person_id,),
        ).fetchone()
        return {
            "official_fingerprint_count": int(row["n"] or 0),
            "official_fingerprint_latest_year": row["latest_year"],
            "official_fingerprint_doi_count": int(row["doi_count"] or 0),
        }
    row = connection.execute(
        """
        SELECT COUNT(*) n, MAX(publication_year) latest_year,
               SUM(CASE WHEN doi IS NOT NULL AND doi!='' THEN 1 ELSE 0 END) doi_count
        FROM official_publication_fingerprints AS f
        WHERE f.person_id=? AND (
            NOT EXISTS (SELECT 1 FROM official_publication_source_claims c WHERE c.fingerprint_id=f.fingerprint_id)
            OR EXISTS (
                SELECT 1 FROM official_publication_source_claims c
                WHERE c.fingerprint_id=f.fingerprint_id
                  AND c.claim_status IN ('active', 'no_longer_observed')
            )
        )
        """,
        (person_id,),
    ).fetchone()
    return {
        "official_fingerprint_count": int(row["n"] or 0),
        "official_fingerprint_latest_year": row["latest_year"],
        "official_fingerprint_doi_count": int(row["doi_count"] or 0),
    }


def _build_record(
    connection: sqlite3.Connection,
    base: CanonicalPIRecord,
    spec: _IdentitySpec,
    *,
    retain_original_first_seen: bool,
) -> CanonicalPIRecord:
    payload = asdict(base)
    payload.update(spec.record)
    payload["person_id"] = spec.person_id
    evidence_rows = connection.execute(
        f"SELECT evidence_id FROM person_evidence WHERE source_url IN ({_placeholders(spec.source_urls)}) ORDER BY evidence_id",
        spec.source_urls,
    ).fetchall()
    payload["source_evidence_ids"] = [str(row[0]) for row in evidence_rows]
    observations = connection.execute(
        f"SELECT observed_at, run_id FROM pi_observations WHERE person_id=? AND source_url IN ({_placeholders(spec.source_urls)}) ORDER BY observed_at",
        (spec.person_id, *spec.source_urls),
    ).fetchall()
    if not observations:
        raise ValueError(f"No canonical observations remain for {spec.person_id}")
    payload["first_seen_at"] = (
        base.first_seen_at if retain_original_first_seen and base.first_seen_at else observations[0]["observed_at"]
    )
    payload["last_checked_at"] = observations[-1]["observed_at"]
    payload["last_seen_at"] = observations[-1]["observed_at"]
    payload["last_seen_run_id"] = observations[-1]["run_id"]
    payload["membership_status"] = "active"
    payload["missing_streak"] = 0
    payload["schema_version"] = 2
    payload["publications_summary"] = _publication_summary(connection, spec.person_id)
    return CanonicalPIRecord(**{key: payload[key] for key in _CANONICAL_FIELDS})


def _email_models(connection: sqlite3.Connection, person_id: str) -> list[EmailEvidence]:
    rows = connection.execute(
        "SELECT * FROM email_evidence WHERE person_id=?", (person_id,)
    ).fetchall()
    return [
        EmailEvidence(
            email=row["email"],
            source_url=row["source_url"],
            person_id=row["person_id"],
            source_type=row["source_type"],
            domain_aligned=bool(row["domain_aligned"]),
            official_source=bool(row["official_source"]),
            extracted_at=row["extracted_at"],
            confidence=float(row["confidence"] or 0),
            verdict=row["verdict"],
            association=row["association"],
            run_id=row["run_id"],
        )
        for row in rows
    ]


def _write_canonical(connection: sqlite3.Connection, record: CanonicalPIRecord, now: str) -> None:
    connection.execute(
        """
        INSERT INTO canonical_pi_records
        (person_id, display_name, institution_id, institution_name, title,
         department, profile_url, emails_json, research_areas_json,
         contact_confidence, topic_match_confidence,
         current_affiliation_confidence, dedupe_key, first_seen_at, last_seen_at,
         last_seen_run_id, membership_status, missing_streak, pool_scope,
         schema_version, record_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET
          display_name=excluded.display_name, institution_id=excluded.institution_id,
          institution_name=excluded.institution_name, title=excluded.title,
          department=excluded.department, profile_url=excluded.profile_url,
          emails_json=excluded.emails_json, research_areas_json=excluded.research_areas_json,
          contact_confidence=excluded.contact_confidence,
          topic_match_confidence=excluded.topic_match_confidence,
          current_affiliation_confidence=excluded.current_affiliation_confidence,
          dedupe_key=excluded.dedupe_key, first_seen_at=excluded.first_seen_at,
          last_seen_at=excluded.last_seen_at, last_seen_run_id=excluded.last_seen_run_id,
          membership_status=excluded.membership_status, missing_streak=excluded.missing_streak,
          pool_scope=excluded.pool_scope, schema_version=excluded.schema_version,
          record_json=excluded.record_json, updated_at=excluded.updated_at
        """,
        (
            record.person_id,
            record.display_name,
            record.institution_id,
            record.institution_name,
            record.title,
            record.department,
            record.profile_url,
            _canonical_json(record.emails),
            _canonical_json(record.research_areas),
            record.contact_confidence,
            record.topic_match_confidence,
            record.current_affiliation_confidence,
            dedupe_key_for_record(record),
            record.first_seen_at,
            record.last_seen_at,
            record.last_seen_run_id,
            record.membership_status,
            record.missing_streak,
            record.pool_scope,
            record.schema_version,
            record.to_json(),
            now,
        ),
    )
    connection.execute("DELETE FROM pi_identity_keys WHERE person_id=?", (record.person_id,))
    connection.executemany(
        "INSERT INTO pi_identity_keys (institution_id, person_id, identity_kind, identity_value) VALUES (?, ?, ?, ?)",
        [
            (record.institution_id, record.person_id, kind, value)
            for kind, value in sorted(_identity_index_entries(record))
        ],
    )


def _write_verdict(connection: sqlite3.Connection, verdict: PIContactVerdict) -> None:
    connection.execute(
        """
        INSERT INTO contact_verdicts
        (person_id, verdict, reasons_json, recommended_action,
         last_live_checked_at, contact_confidence, topic_match_confidence,
         current_affiliation_confidence, run_id, record_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET
          verdict=excluded.verdict, reasons_json=excluded.reasons_json,
          recommended_action=excluded.recommended_action,
          last_live_checked_at=excluded.last_live_checked_at,
          contact_confidence=excluded.contact_confidence,
          topic_match_confidence=excluded.topic_match_confidence,
          current_affiliation_confidence=excluded.current_affiliation_confidence,
          run_id=excluded.run_id, record_json=excluded.record_json
        """,
        (
            verdict.person_id,
            verdict.verdict,
            _canonical_json(verdict.reasons),
            verdict.recommended_action,
            verdict.last_live_checked_at,
            verdict.contact_confidence,
            verdict.topic_match_confidence,
            verdict.current_affiliation_confidence,
            verdict.run_id,
            verdict.to_json(),
        ),
    )


def _enqueue_separated_sync(
    connection: sqlite3.Connection,
    manifest: _SplitManifest,
    run_id: str,
    now: str,
) -> bool:
    if not _table_exists(connection, "vector_dirty_queue"):
        # Older production pools predate the enrichment/vector migration.  The
        # split remains valid there; the later schema upgrade/pilot promotion
        # supplies the OpenAlex state.  Report the deferred queue explicitly.
        return False
    existing = connection.execute(
        "SELECT queue_id FROM vector_dirty_queue WHERE entity_kind='openalex_works_sync' AND entity_id=? AND status='pending'",
        (manifest.separated.person_id,),
    ).fetchone()
    payload = _canonical_json(
        {"identity_split_id": manifest.split_id, "person_id": manifest.separated.person_id}
    )
    if existing:
        connection.execute(
            """
            UPDATE vector_dirty_queue
            SET person_id=?, reason='reviewed_identity_split', run_id=?, updated_at=?, payload_json=?
            WHERE queue_id=?
            """,
            (manifest.separated.person_id, run_id, now, payload, existing["queue_id"]),
        )
    else:
        connection.execute(
            """
            INSERT INTO vector_dirty_queue
            (entity_kind, entity_id, person_id, fingerprint_id, reason, run_id,
             status, attempts, created_at, updated_at, processed_at, last_error,
             payload_json)
            VALUES ('openalex_works_sync', ?, ?, NULL, 'reviewed_identity_split', ?,
                    'pending', 0, ?, ?, NULL, NULL, ?)
            """,
            (manifest.separated.person_id, manifest.separated.person_id, run_id, now, now, payload),
        )
    return True


def _write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_reviewed_identity_split(
    database_path: str | Path,
    manifest_path: str | Path,
    *,
    report_path: str | Path | None = None,
    apply: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Dry-run or transactionally apply one explicitly reviewed canonical split."""

    database = Path(database_path).resolve()
    manifest_file = Path(manifest_path).resolve()
    output = Path(report_path).resolve() if report_path is not None else None
    if not database.is_file():
        raise FileNotFoundError(f"Target PI Pool database does not exist: {database}")
    if not manifest_file.is_file():
        raise FileNotFoundError(f"Reviewed identity split manifest does not exist: {manifest_file}")
    if _same_file(database, manifest_file):
        raise ValueError("Split manifest must not be the target database")
    if output is not None and (_same_file(output, database) or _same_file(output, manifest_file)):
        raise ValueError("Split report must not overwrite the database or manifest")
    manifest, manifest_payload, manifest_sha256 = _load_manifest(manifest_file)
    effective_run_id = run_id or f"reviewed-identity-split:{uuid4().hex}"
    generated_at = utc_now_iso()

    connection = _connect(database, read_only=not apply)
    try:
        plan = _preflight(connection, manifest, manifest_sha256)
        report: dict[str, Any] = {
            "schema_version": 1,
            "operation": "reviewed_identity_split",
            "mode": "apply" if apply else "dry_run",
            "status": plan["status"],
            "generated_at": generated_at,
            "database": str(database),
            "manifest": str(manifest_file),
            "manifest_sha256": manifest_sha256,
            "split_id": manifest.split_id,
            "institution_id": manifest.institution_id,
            "retained_person_id": manifest.retained.person_id,
            "separated_person_id": manifest.separated.person_id,
            "reason": manifest.reason,
            "evidence": manifest.evidence,
            "plan": plan,
        }
        if not apply or plan["status"] == "already_applied":
            _write_report(output, report)
            return report

        connection.execute("BEGIN IMMEDIATE")
        try:
            # Recheck every invariant while holding the write lock.
            plan = _preflight(connection, manifest, manifest_sha256)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reviewed_identity_splits (
                    split_id TEXT PRIMARY KEY,
                    manifest_sha256 TEXT NOT NULL,
                    institution_id TEXT NOT NULL,
                    retained_person_id TEXT NOT NULL,
                    separated_person_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    record_json TEXT NOT NULL
                )
                """
            )
            deleted_alias = connection.execute(
                "DELETE FROM pi_identity_aliases WHERE alias_person_id=? AND canonical_person_id=?",
                (manifest.separated.person_id, manifest.retained.person_id),
            ).rowcount
            if deleted_alias != 1:
                raise ValueError("Reviewed separated alias changed after preflight")

            _move_observations(
                connection,
                manifest.retained.person_id,
                manifest.separated.person_id,
                manifest.separated.source_urls,
            )
            _move_fingerprints(
                connection,
                manifest.retained.person_id,
                manifest.separated.person_id,
                manifest.separated.source_urls,
            )
            _move_refresh_state(
                connection,
                manifest.retained.person_id,
                manifest.separated.person_id,
                manifest.separated.source_urls,
            )
            email_rows_kept, polluted_email_rows_removed = _partition_email_evidence(
                connection, manifest
            )

            base = _canonical_record(connection, manifest.retained.person_id)
            if base is None:
                raise ValueError("Merged canonical person disappeared during split")
            retained = _build_record(
                connection, base, manifest.retained, retain_original_first_seen=True
            )
            separated = _build_record(
                connection, base, manifest.separated, retain_original_first_seen=False
            )
            for record in (retained, separated):
                verdict = contact_verdict_for_pi(record, _email_models(connection, record.person_id))
                verdict.run_id = effective_run_id
                verdict.last_live_checked_at = record.last_checked_at
                record.contact_confidence = verdict.contact_confidence
                record.topic_match_confidence = verdict.topic_match_confidence
                record.current_affiliation_confidence = verdict.current_affiliation_confidence
                _write_canonical(connection, record, generated_at)
                _write_verdict(connection, verdict)

            sync_enqueued = _enqueue_separated_sync(
                connection, manifest, effective_run_id, generated_at
            )
            cohort_after = _cohort_size(connection, manifest.institution_id)
            if cohort_after != plan["cohort_after"]:
                raise RuntimeError(
                    f"Cohort changed unexpectedly during split: {cohort_after} != {plan['cohort_after']}"
                )
            audit_payload = {
                "manifest": manifest_payload,
                "plan": plan,
                "email_rows_kept": email_rows_kept,
                "polluted_email_rows_removed": polluted_email_rows_removed,
            }
            connection.execute(
                """
                INSERT INTO reviewed_identity_splits
                (split_id, manifest_sha256, institution_id, retained_person_id,
                 separated_person_id, reason, run_id, applied_at, record_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.split_id,
                    manifest_sha256,
                    manifest.institution_id,
                    manifest.retained.person_id,
                    manifest.separated.person_id,
                    manifest.reason,
                    effective_run_id,
                    generated_at,
                    _canonical_json(audit_payload),
                ),
            )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != "ok" or foreign_key_errors:
                raise RuntimeError(
                    f"PI Pool integrity failed after split: integrity={integrity}, foreign_keys={len(foreign_key_errors)}"
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        report["status"] = "applied"
        report["plan"] = plan
        report["result"] = {
            "email_rows_kept": email_rows_kept,
            "polluted_email_rows_removed": polluted_email_rows_removed,
            "cohort_after": cohort_after,
            "integrity_check": integrity,
            "foreign_key_errors": len(foreign_key_errors),
            "openalex_sync_enqueued_for": (
                manifest.separated.person_id if sync_enqueued else None
            ),
            "openalex_sync_enqueue_deferred": not sync_enqueued,
        }
        _write_report(output, report)
        return report
    finally:
        connection.close()
