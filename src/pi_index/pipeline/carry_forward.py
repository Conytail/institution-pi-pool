from __future__ import annotations

from dataclasses import fields
import json
from pathlib import Path
import sqlite3
from typing import Any, TypeVar

from ..models import (
    CanonicalPIRecord,
    EmailEvidence,
    OfficialPublicationFingerprint,
    PersonEvidence,
    stable_id,
)
from ..storage import PIIndexStorage
from ..verify.confidence import contact_verdict_for_pi


ModelT = TypeVar("ModelT")


def _from_json(model_type: type[ModelT], value: str) -> ModelT:
    payload = json.loads(value)
    allowed = {item.name for item in fields(model_type)}
    return model_type(**{key: item for key, item in payload.items() if key in allowed})


def _readonly_connection(path: str | Path) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _same_file(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return left.samefile(right)
    except OSError:
        return False


def _unique(values: list[str | None]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _merge_external_ids_current_first(
    current: dict[str, Any],
    carried: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in sorted(set(current).union(carried)):
        values: list[Any] = []
        for source in (current, carried):
            value = source.get(key)
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if candidate not in (None, "") and candidate not in values:
                    values.append(candidate)
        if values:
            merged[key] = values[0] if len(values) == 1 else values
    return merged


def _merge_carried_into_current(
    carried: CanonicalPIRecord,
    current: CanonicalPIRecord,
    *,
    include_carried_evidence_ids: bool,
) -> CanonicalPIRecord:
    """Fill current gaps without letting archived values replace refreshed data."""

    if carried.institution_id != current.institution_id:
        raise ValueError("Carry-forward identity merge requires the same institution")

    carried_display_name = carried.display_name
    current.display_name = current.display_name or carried.display_name
    current.given_name = current.given_name or carried.given_name
    current.family_name = current.family_name or carried.family_name
    current.ror_id = current.ror_id or carried.ror_id
    current.title = current.title or carried.title
    current.department = current.department or carried.department
    current.profile_url = current.profile_url or carried.profile_url
    current.lab_url = current.lab_url or carried.lab_url

    aliases = set(current.aliases + carried.aliases)
    if carried_display_name and carried_display_name != current.display_name:
        aliases.add(carried_display_name)
    aliases.discard(current.display_name)
    current.aliases = sorted(aliases)
    current.departments = _unique(
        [
            current.department,
            *(current.departments or []),
            carried.department,
            *(carried.departments or []),
        ]
    )
    current.profile_urls = _unique(
        [
            current.profile_url,
            *(current.profile_urls or []),
            carried.profile_url,
            *(carried.profile_urls or []),
        ]
    )
    if not current.emails and carried.emails:
        current.emails = sorted(set(carried.emails))
        current.email_association = carried.email_association
    if not current.research_areas:
        current.research_areas = list(carried.research_areas)
    current.external_ids = _merge_external_ids_current_first(
        current.external_ids or {},
        carried.external_ids or {},
    )
    if include_carried_evidence_ids:
        current.source_evidence_ids = sorted(
            set(current.source_evidence_ids + carried.source_evidence_ids)
        )

    current.field_sources = {
        **(carried.field_sources or {}),
        **(current.field_sources or {}),
    }
    current.first_seen_at = min(
        value
        for value in (current.first_seen_at, carried.first_seen_at)
        if value
    ) if current.first_seen_at or carried.first_seen_at else None
    current.last_seen_at = current.last_seen_at or carried.last_seen_at
    current.last_seen_run_id = current.last_seen_run_id or carried.last_seen_run_id
    current.pool_scope = current.pool_scope or carried.pool_scope
    current.schema_version = 2
    return current


def _selected_person_ids(
    connection: sqlite3.Connection,
    source_url_patterns: list[str],
) -> list[str]:
    if not source_url_patterns:
        raise ValueError("At least one source URL LIKE pattern is required")
    where = " OR ".join("o.source_url LIKE ?" for _ in source_url_patterns)
    rows = connection.execute(
        f"""
        SELECT DISTINCT o.person_id
        FROM pi_observations AS o
        JOIN canonical_pi_records AS p ON p.person_id=o.person_id
        WHERE ({where})
          AND COALESCE(p.membership_status, 'active')!='inactive'
        ORDER BY o.person_id
        """,
        source_url_patterns,
    ).fetchall()
    return [str(row["person_id"]) for row in rows]


def _copy_person_evidence(
    source: sqlite3.Connection,
    target: PIIndexStorage,
    record: CanonicalPIRecord,
) -> int:
    evidence_ids = list(dict.fromkeys(record.source_evidence_ids or []))
    if not evidence_ids:
        return 0
    placeholders = ",".join("?" for _ in evidence_ids)
    rows = source.execute(
        f"SELECT record_json FROM person_evidence WHERE evidence_id IN ({placeholders})",
        evidence_ids,
    ).fetchall()
    for row in rows:
        target.insert_person_evidence(_from_json(PersonEvidence, row["record_json"]))
    return len(rows)


def _copy_email_evidence(
    source: sqlite3.Connection,
    target: PIIndexStorage,
    source_person_id: str,
    target_person_id: str,
) -> int:
    rows = source.execute(
        "SELECT record_json FROM email_evidence WHERE person_id=?",
        (source_person_id,),
    ).fetchall()
    copied = 0
    for row in rows:
        evidence = _from_json(EmailEvidence, row["record_json"])
        evidence.person_id = target_person_id
        exists = target.conn.execute(
            """
            SELECT 1 FROM email_evidence
            WHERE email=? AND source_url=? AND person_id=?
            """,
            (evidence.email.casefold(), evidence.source_url, target_person_id),
        ).fetchone()
        if exists:
            continue
        target.insert_email_evidence(evidence)
        copied += 1
    return copied


def _copy_publications(
    source: sqlite3.Connection,
    target: PIIndexStorage,
    source_person_id: str,
    target_person_id: str,
) -> int:
    rows = source.execute(
        "SELECT record_json FROM official_publication_fingerprints WHERE person_id=?",
        (source_person_id,),
    ).fetchall()
    copied = 0
    for row in rows:
        publication = _from_json(OfficialPublicationFingerprint, row["record_json"])
        identity_key = (publication.doi or "").strip().casefold()
        if not identity_key:
            identity_key = (
                f"{' '.join(publication.title.casefold().split())}|"
                f"{publication.publication_year or ''}"
            )
        publication.person_id = target_person_id
        publication.fingerprint_id = stable_id("pubfp", target_person_id, identity_key)
        existing_rows = target.conn.execute(
            """
            SELECT fingerprint_id, title, publication_year, doi
            FROM official_publication_fingerprints
            WHERE person_id=?
            """,
            (target_person_id,),
        ).fetchall()
        collision = False
        for existing in existing_rows:
            if existing["fingerprint_id"] == publication.fingerprint_id:
                collision = True
                break
            existing_doi = (existing["doi"] or "").strip().casefold()
            if identity_key and publication.doi and existing_doi == identity_key:
                collision = True
                break
            if not publication.doi:
                existing_key = (
                    f"{' '.join((existing['title'] or '').casefold().split())}|"
                    f"{existing['publication_year'] or ''}"
                )
                if existing_key == identity_key:
                    collision = True
                    break
        if collision:
            continue
        target.upsert_publication_fingerprint(publication)
        copied += 1
    return copied


def _refresh_contact_verdict(target: PIIndexStorage, person_id: str) -> None:
    record = target.get_pi_record(person_id)
    if record is None:
        return
    record.publications_summary = target.publication_summary(person_id)
    evidence_rows = target.conn.execute(
        "SELECT record_json FROM email_evidence WHERE person_id=?",
        (person_id,),
    ).fetchall()
    email_evidence = [
        _from_json(EmailEvidence, row["record_json"])
        for row in evidence_rows
    ]
    verdict = contact_verdict_for_pi(record, email_evidence)
    verdict.last_live_checked_at = record.last_checked_at
    verdict.run_id = record.last_seen_run_id or "carry-forward"
    record.contact_confidence = verdict.contact_confidence
    record.topic_match_confidence = verdict.topic_match_confidence
    record.current_affiliation_confidence = verdict.current_affiliation_confidence
    target.upsert_pi_record(record)
    target.upsert_contact_verdict(verdict)


def carry_forward_records(
    source_db: str | Path,
    target: PIIndexStorage,
    source_url_patterns: list[str],
    *,
    reason: str,
    report_path: str | Path | None = None,
    reactivate_existing: bool = False,
) -> dict[str, Any]:
    """Carry selected official records forward when a source cannot be refreshed.

    Selection is evidence-backed: a person must have an observation whose source
    URL matches one of the supplied SQL LIKE patterns.  The source database is
    opened read-only.  Current target records win field-quality conflicts, while
    strong identifiers, aliases, research evidence and person-specific URLs are
    consolidated through the normal identity rules.
    """

    source_path = Path(source_db).resolve()
    target_path = target.db_path.resolve()
    if _same_file(source_path, target_path):
        raise ValueError("Carry-forward source and target databases must be different files")
    output = Path(report_path).resolve() if report_path is not None else None
    if output is not None and (
        _same_file(output, source_path) or _same_file(output, target_path)
    ):
        raise ValueError("Carry-forward report must not overwrite the source or target database")

    inserted = 0
    merged = 0
    person_evidence_count = 0
    email_evidence_count = 0
    publication_count = 0
    mappings: list[dict[str, str]] = []
    source = _readonly_connection(source_path)
    try:
        selected_ids = _selected_person_ids(source, source_url_patterns)
        for source_person_id in selected_ids:
            row = source.execute(
                "SELECT record_json FROM canonical_pi_records WHERE person_id=?",
                (source_person_id,),
            ).fetchone()
            if row is None:
                continue
            carried = _from_json(CanonicalPIRecord, row["record_json"])
            carried.membership_status = "active"
            carried.missing_streak = 0
            carried.schema_version = 2

            target_person_id = carried.person_id
            current = target.get_pi_record(target_person_id)
            duplicate_reason = "same_person_id" if current is not None else None
            if current is None:
                duplicate = target.find_existing_duplicate(carried)
                if duplicate is not None:
                    target_person_id, duplicate_reason = duplicate
                    current = target.get_pi_record(target_person_id)

            if current is None:
                target.upsert_pi_record(carried)
                inserted += 1
            else:
                merged_record = _merge_carried_into_current(
                    carried,
                    current,
                    include_carried_evidence_ids=(target_person_id == source_person_id),
                )
                if reactivate_existing:
                    merged_record.membership_status = "active"
                    merged_record.missing_streak = 0
                merged_record.person_id = current.person_id
                target.upsert_pi_record(merged_record)
                target_person_id = current.person_id
                merged += 1

            if target_person_id != source_person_id:
                target.upsert_person_id_alias(
                    source_person_id,
                    target_person_id,
                    carried.institution_id,
                    duplicate_reason or "carry_forward_identity_match",
                    carried.last_seen_run_id or "carry-forward",
                )

            if target_person_id == source_person_id:
                person_evidence_count += _copy_person_evidence(source, target, carried)
            email_evidence_count += _copy_email_evidence(
                source,
                target,
                source_person_id,
                target_person_id,
            )
            publication_count += _copy_publications(
                source,
                target,
                source_person_id,
                target_person_id,
            )
            _refresh_contact_verdict(target, target_person_id)
            mappings.append(
                {
                    "source_person_id": source_person_id,
                    "target_person_id": target_person_id,
                    "resolution": duplicate_reason or "inserted",
                }
            )
        target.sync_identity_index(rebuild=True)
    finally:
        source.close()

    report: dict[str, Any] = {
        "source_db": str(source_path),
        "target_db": str(target.db_path.resolve()),
        "source_url_like": source_url_patterns,
        "reason": reason,
        "reactivate_existing": reactivate_existing,
        "selected": len(selected_ids),
        "inserted": inserted,
        "merged_with_current": merged,
        "person_evidence_copied": person_evidence_count,
        "email_evidence_copied": email_evidence_count,
        "publication_fingerprints_copied": publication_count,
        "records": mappings,
    }
    if report_path is not None:
        assert output is not None
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return report
