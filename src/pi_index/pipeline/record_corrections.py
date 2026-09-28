from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

import yaml

from ..models import (
    CanonicalPIRecord,
    EmailEvidence,
    PersonEvidence,
    content_hash,
    stable_id,
)
from ..storage import PIIndexStorage
from ..verify.confidence import contact_verdict_for_pi
from ..verify.email import domain_aligned, verify_email
from ..verify.official_source import is_official_url


CORRECTION_SCHEMA_VERSION = 1
CORRECTION_SOURCE_TYPE = "official_profile_manual_verification"
CORRECTION_EXTRACTION_METHOD = "verified_record_correction_v1"

_REQUIRED_TEXT_KEYS = {
    "correction_id",
    "institution_id",
    "official_evidence_url",
    "reason",
}
_NONEMPTY_TEXT_FIELDS = {"display_name"}
_OPTIONAL_TEXT_FIELDS = {
    "given_name",
    "family_name",
    "title",
    "department",
    "lab_url",
    "pool_scope",
    "profile_url",
}
_LIST_TEXT_FIELDS = {
    "aliases",
    "departments",
    "emails",
    "profile_urls",
    "research_areas",
}
_DICT_FIELDS = {"external_ids"}
_ALLOWED_FIELDS = (
    _NONEMPTY_TEXT_FIELDS
    | _OPTIONAL_TEXT_FIELDS
    | _LIST_TEXT_FIELDS
    | _DICT_FIELDS
    | {"email"}
)
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+$")


def _same_file(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return left.samefile(right)
    except OSError:
        return False


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _dedupe_text_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"Correction field {field_name!r} must be a list of strings")
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def _require_http_url(value: str, field_name: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Correction field {field_name!r} must be an absolute HTTP(S) URL")


def _verified_at_text(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as error:
            raise ValueError("verified_at must be an ISO-8601 timestamp") from error
    else:
        raise ValueError("Each correction must have a non-empty verified_at timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("verified_at must include a timezone offset")
    return parsed.isoformat()


def _normalize_fields(raw_fields: Any) -> dict[str, Any]:
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise ValueError("Each correction must contain a non-empty fields mapping")
    unknown = sorted(set(raw_fields) - _ALLOWED_FIELDS)
    if unknown:
        raise ValueError(f"Unsupported correction fields: {', '.join(unknown)}")
    if "email" in raw_fields and "emails" in raw_fields:
        raise ValueError("Use either correction field 'email' or 'emails', not both")

    normalized: dict[str, Any] = {}
    for field_name, value in raw_fields.items():
        canonical_field = "emails" if field_name == "email" else field_name
        if field_name == "email":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Correction field 'email' must be a non-empty string")
            value = [value]
        if canonical_field in _NONEMPTY_TEXT_FIELDS:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Correction field {canonical_field!r} must be a non-empty string"
                )
            value = value.strip()
        elif canonical_field in _OPTIONAL_TEXT_FIELDS:
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"Correction field {canonical_field!r} must be a string or null"
                )
            value = value.strip() if isinstance(value, str) else None
        elif canonical_field in _LIST_TEXT_FIELDS:
            value = _dedupe_text_list(value, canonical_field)
        elif canonical_field in _DICT_FIELDS:
            if not isinstance(value, dict):
                raise ValueError(f"Correction field {canonical_field!r} must be a mapping")
            value = deepcopy(value)
        normalized[canonical_field] = value

    if "emails" in normalized:
        normalized["emails"] = [email.casefold() for email in normalized["emails"]]
        invalid = [email for email in normalized["emails"] if not _EMAIL_PATTERN.fullmatch(email)]
        if invalid:
            raise ValueError(f"Invalid corrected email address: {invalid[0]}")
    for field_name in ("profile_url", "lab_url"):
        value = normalized.get(field_name)
        if value:
            _require_http_url(value, field_name)
    for field_name in ("profile_urls",):
        for value in normalized.get(field_name, []):
            _require_http_url(value, field_name)
    return normalized


def _load_corrections(path: Path) -> list[dict[str, Any]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("Corrections file root must be a mapping")
    if payload.get("schema_version") != CORRECTION_SCHEMA_VERSION:
        raise ValueError(
            f"Corrections file schema_version must be {CORRECTION_SCHEMA_VERSION}"
        )
    raw_items = payload.get("corrections")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Corrections file must contain a non-empty corrections list")

    corrections: list[dict[str, Any]] = []
    correction_ids: set[str] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError(f"Correction item {index} must be a mapping")
        for key in _REQUIRED_TEXT_KEYS:
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise ValueError(f"Correction item {index} requires non-empty {key!r}")
        locator_keys = [key for key in ("person_id", "profile_url") if raw.get(key)]
        if len(locator_keys) != 1:
            raise ValueError(
                f"Correction {raw['correction_id']!r} must use exactly one locator: "
                "person_id or profile_url"
            )
        locator_key = locator_keys[0]
        locator_value = raw[locator_key]
        if not isinstance(locator_value, str) or not locator_value.strip():
            raise ValueError(
                f"Correction locator {locator_key!r} must be a non-empty string"
            )
        if locator_key == "profile_url":
            _require_http_url(locator_value.strip(), locator_key)

        correction_id = raw["correction_id"].strip()
        if correction_id in correction_ids:
            raise ValueError(f"Duplicate correction_id: {correction_id}")
        correction_ids.add(correction_id)
        official_evidence_url = raw["official_evidence_url"].strip()
        _require_http_url(official_evidence_url, "official_evidence_url")
        corrections.append(
            {
                "correction_id": correction_id,
                "institution_id": raw["institution_id"].strip(),
                "locator": {locator_key: locator_value.strip()},
                "fields": _normalize_fields(raw.get("fields")),
                "official_evidence_url": official_evidence_url,
                "reason": raw["reason"].strip(),
                "verified_at": _verified_at_text(raw.get("verified_at")),
            }
        )
    return corrections


def _official_domains(storage: PIIndexStorage, institution_id: str) -> list[str]:
    row = storage.conn.execute(
        "SELECT official_domains_json FROM institutions WHERE institution_id=?",
        (institution_id,),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"Correction institution_id {institution_id!r} has no institution metadata"
        )
    try:
        values = json.loads(row["official_domains_json"] or "[]")
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Correction institution_id {institution_id!r} has invalid official domains"
        ) from error
    domains = [str(value).strip().casefold().removeprefix("www.") for value in values if value]
    if not domains:
        raise ValueError(
            f"Correction institution_id {institution_id!r} has no official domains"
        )
    return domains


def _resolve_target(
    storage: PIIndexStorage,
    institution_id: str,
    locator: dict[str, str],
) -> CanonicalPIRecord:
    if "person_id" in locator:
        rows = storage.conn.execute(
            """
            SELECT person_id
            FROM canonical_pi_records
            WHERE institution_id=? AND person_id=?
            """,
            (institution_id, locator["person_id"]),
        ).fetchall()
    else:
        profile_url = locator["profile_url"]
        rows = storage.conn.execute(
            """
            SELECT person_id
            FROM canonical_pi_records AS p
            WHERE p.institution_id=?
              AND (
                    p.profile_url=?
                    OR EXISTS (
                        SELECT 1
                        FROM json_each(p.record_json, '$.profile_urls') AS urls
                        WHERE urls.type='text' AND urls.value=?
                    )
              )
            """,
            (institution_id, profile_url, profile_url),
        ).fetchall()
    if len(rows) != 1:
        locator_text = next(iter(locator.items()))
        raise ValueError(
            "Correction locator must resolve to exactly one canonical record; "
            f"institution_id={institution_id!r}, {locator_text[0]}={locator_text[1]!r}, "
            f"matches={len(rows)}"
        )
    record = storage.get_pi_record(str(rows[0]["person_id"]))
    if record is None:
        raise ValueError("Resolved correction target disappeared before preflight completed")
    return record


def _identity_keys(storage: PIIndexStorage, person_id: str) -> list[dict[str, str]]:
    rows = storage.conn.execute(
        """
        SELECT identity_kind, identity_value
        FROM pi_identity_keys
        WHERE person_id=?
        ORDER BY identity_kind, identity_value
        """,
        (person_id,),
    ).fetchall()
    return [
        {"identity_kind": str(row["identity_kind"]), "identity_value": str(row["identity_value"])}
        for row in rows
    ]


def _evidence_value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _change_action(before: Any, after: Any) -> str:
    if before == after:
        return "unchanged"
    if _is_missing(before):
        return "filled_missing_value"
    return "overwrote_non_empty_value"


def _contact_verdict_payload(storage: PIIndexStorage, person_id: str) -> dict[str, Any] | None:
    row = storage.conn.execute(
        "SELECT record_json FROM contact_verdicts WHERE person_id=?", (person_id,)
    ).fetchone()
    return json.loads(row["record_json"]) if row is not None else None


def _email_evidence_records(storage: PIIndexStorage, person_id: str) -> list[EmailEvidence]:
    rows = storage.conn.execute(
        "SELECT record_json FROM email_evidence WHERE person_id=? ORDER BY email, source_url",
        (person_id,),
    ).fetchall()
    return [EmailEvidence(**json.loads(row["record_json"])) for row in rows]


def _apply_one(
    storage: PIIndexStorage,
    correction: dict[str, Any],
    target_record: CanonicalPIRecord,
    official_domains: list[str],
) -> dict[str, Any]:
    record = deepcopy(target_record)
    person_id = record.person_id
    run_id = f"record-correction:{correction['correction_id']}"
    identity_before = _identity_keys(storage, person_id)
    verdict_before = _contact_verdict_payload(storage, person_id)
    field_changes: dict[str, dict[str, Any]] = {}
    evidence_records: list[PersonEvidence] = []

    for field_name, after in correction["fields"].items():
        before = deepcopy(getattr(record, field_name))
        field_changes[field_name] = {
            "before": before,
            "after": deepcopy(after),
            "action": _change_action(before, after),
            "source_before": (record.field_sources or {}).get(field_name),
            "source_after": CORRECTION_SOURCE_TYPE,
        }
        setattr(record, field_name, deepcopy(after))
        value_text = _evidence_value(after)
        evidence_id = stable_id(
            "ev",
            CORRECTION_EXTRACTION_METHOD,
            correction["correction_id"],
            person_id,
            field_name,
            value_text,
            correction["official_evidence_url"],
            correction["verified_at"],
        )
        evidence_records.append(
            PersonEvidence(
                evidence_id=evidence_id,
                person_temp_id=person_id,
                institution_id=record.institution_id,
                field_name=field_name,
                field_value=value_text,
                source_url=correction["official_evidence_url"],
                source_type=CORRECTION_SOURCE_TYPE,
                extraction_method=CORRECTION_EXTRACTION_METHOD,
                extracted_at=correction["verified_at"],
                confidence=1.0,
                evidence_text=(
                    f"Official-field correction {correction['correction_id']}: "
                    f"{field_name}={value_text}. {correction['reason']}"
                ),
                content_hash=content_hash(
                    json.dumps(
                        {
                            "field": field_name,
                            "value": after,
                            "official_evidence_url": correction["official_evidence_url"],
                            "reason": correction["reason"],
                            "verified_at": correction["verified_at"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                ),
                run_id=run_id,
            )
        )

    record.field_sources = dict(record.field_sources or {})
    for field_name in correction["fields"]:
        record.field_sources[field_name] = CORRECTION_SOURCE_TYPE
    record.source_evidence_ids = sorted(
        set(record.source_evidence_ids or []).union(item.evidence_id for item in evidence_records)
    )
    record.last_checked_at = correction["verified_at"]
    if "emails" in correction["fields"]:
        record.email_association = "person_local" if record.emails else "none"
    record.publications_summary = storage.publication_summary(person_id)

    removed_email_evidence = 0
    inserted_email_evidence = 0
    if "emails" in correction["fields"]:
        removed_email_evidence = storage.conn.execute(
            "DELETE FROM email_evidence WHERE person_id=?", (person_id,)
        ).rowcount
        storage.conn.commit()
        for email in record.emails:
            evidence = verify_email(
                email,
                correction["official_evidence_url"],
                CORRECTION_SOURCE_TYPE,
                official_domains,
                official_domains,
                person_id=person_id,
                association="person_local",
            )
            evidence.extracted_at = correction["verified_at"]
            evidence.run_id = run_id
            storage.insert_email_evidence(evidence)
            inserted_email_evidence += 1

    for evidence in evidence_records:
        storage.insert_person_evidence(evidence)

    email_evidence = _email_evidence_records(storage, person_id)
    verdict = contact_verdict_for_pi(record, email_evidence)
    verdict.last_live_checked_at = correction["verified_at"]
    verdict.run_id = run_id
    record.contact_confidence = verdict.contact_confidence
    record.topic_match_confidence = verdict.topic_match_confidence
    record.current_affiliation_confidence = verdict.current_affiliation_confidence
    storage.upsert_pi_record(record)
    storage.upsert_contact_verdict(verdict)

    identity_after = _identity_keys(storage, person_id)
    before_identity_pairs = {
        (item["identity_kind"], item["identity_value"]) for item in identity_before
    }
    after_identity_pairs = {
        (item["identity_kind"], item["identity_value"]) for item in identity_after
    }
    return {
        "correction_id": correction["correction_id"],
        "institution_id": correction["institution_id"],
        "locator": correction["locator"],
        "person_id": person_id,
        "official_evidence_url": correction["official_evidence_url"],
        "reason": correction["reason"],
        "verified_at": correction["verified_at"],
        "field_resolution_policy": (
            "Every explicitly listed field is authoritative official evidence and replaces "
            "the current canonical value, including a non-empty value. Unlisted fields are preserved."
        ),
        "field_changes": field_changes,
        "non_empty_fields_overwritten": sorted(
            field_name
            for field_name, change in field_changes.items()
            if change["action"] == "overwrote_non_empty_value"
        ),
        "person_evidence_ids": [item.evidence_id for item in evidence_records],
        "email_evidence": {
            "removed": removed_email_evidence,
            "inserted": inserted_email_evidence,
        },
        "contact_verdict": {
            "before": verdict_before,
            "after": verdict.to_dict(),
        },
        "identity_keys": {
            "before": identity_before,
            "after": identity_after,
            "added": [
                {"identity_kind": kind, "identity_value": value}
                for kind, value in sorted(after_identity_pairs - before_identity_pairs)
            ],
            "removed": [
                {"identity_kind": kind, "identity_value": value}
                for kind, value in sorted(before_identity_pairs - after_identity_pairs)
            ],
        },
    }


def apply_record_corrections(
    storage: PIIndexStorage,
    corrections_path: str | Path,
    *,
    report_path: str | Path,
) -> dict[str, Any]:
    """Apply exact, official-evidence-backed corrections to an existing database.

    All correction documents, fields, official domains and target cardinalities
    are preflighted before the first write. A locator is deliberately exact and
    must resolve to one canonical record; zero or multiple records abort the
    entire request.
    """

    corrections_file = Path(corrections_path).resolve()
    database_file = storage.db_path.resolve()
    output_file = Path(report_path).resolve()
    if _same_file(corrections_file, database_file):
        raise ValueError("Corrections input must not be the target database")
    if _same_file(output_file, database_file) or _same_file(output_file, corrections_file):
        raise ValueError("Corrections report must not overwrite the database or corrections input")

    corrections = _load_corrections(corrections_file)
    preflight: list[tuple[dict[str, Any], CanonicalPIRecord, list[str]]] = []
    targeted_person_ids: set[str] = set()
    for correction in corrections:
        domains = _official_domains(storage, correction["institution_id"])
        if not is_official_url(correction["official_evidence_url"], domains):
            raise ValueError(
                f"Correction {correction['correction_id']!r} evidence URL is not on an "
                "official institution domain"
            )
        for profile_url in [
            correction["fields"].get("profile_url"),
            *correction["fields"].get("profile_urls", []),
        ]:
            if profile_url and not is_official_url(profile_url, domains):
                raise ValueError(
                    f"Correction {correction['correction_id']!r} profile URL is not on an "
                    "official institution domain: {profile_url}"
                )
        for email in correction["fields"].get("emails", []):
            if not domain_aligned(email, domains):
                raise ValueError(
                    f"Correction {correction['correction_id']!r} email is not aligned to "
                    f"an official institution domain: {email}"
                )
        target = _resolve_target(
            storage,
            correction["institution_id"],
            correction["locator"],
        )
        if target.person_id in targeted_person_ids:
            raise ValueError(
                f"Multiple corrections target the same person_id in one request: {target.person_id}"
            )
        targeted_person_ids.add(target.person_id)
        preflight.append((correction, target, domains))

    records = [
        _apply_one(storage, correction, target, domains)
        for correction, target, domains in preflight
    ]
    report: dict[str, Any] = {
        "schema_version": 1,
        "database": str(database_file),
        "corrections_file": str(corrections_file),
        "applied": len(records),
        "records": records,
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_file.with_name(output_file.name + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(output_file)
    return report
