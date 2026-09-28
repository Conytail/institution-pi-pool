from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Protocol
import unicodedata
from uuid import uuid4

from ..sources.openalex_client import (
    OpenAlexConfigurationError,
    OpenAlexHTTPClient,
    OpenAlexHTTPError,
    OpenAlexProtocolError,
    OpenAlexWorksResult,
)
from ..sources.openalex_enrichment import strict_openalex_author_candidates


IDENTITY_RULE = "exact_normalized_name_tokens_and_exact_ror"
WORK_ID_RE = re.compile(r"(?:openalex\.org/)?(W\d+)\b", flags=re.I)
AUTHOR_ID_RE = re.compile(r"(?:openalex\.org/)?(A\d+)\b", flags=re.I)
ROR_RE = re.compile(r"(?:ror\.org/)?(0[a-z0-9]{8})\b", flags=re.I)
ORCID_RE = re.compile(r"(\d{4}-\d{4}-\d{4}-[\dX]{4})\b", flags=re.I)
DEFAULT_MAX_AUTHOR_WORKS = 2_000
REVIEWED_IDENTITY_AUDIT_TYPE = "reviewed_openalex_identity_manifest"
REVIEWED_IDENTITY_MATCH_METHOD = "reviewed_openalex_identity_manifest_v1"
IDENTITY_PROBE_CACHE_VERSION = "official_work_probe_v1"
IDENTITY_PROBE_HIT_TTL = timedelta(days=30)
IDENTITY_PROBE_MISS_TTL = timedelta(days=1)
NAME_CONFUSABLES = str.maketrans(
    {
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "х": "x",
        "у": "y",
        "і": "i",
        "ј": "j",
    }
)


@dataclass(frozen=True)
class _ReviewedOpenAlexIdentity:
    person_id: str
    institution_id: str
    expected_display_name: str
    primary_openalex_author_id: str | None
    confirmed_openalex_author_ids: tuple[str, ...]
    sync_mode: str
    official_works: tuple[dict[str, str | None], ...]
    work_policy: dict[str, Any] | None
    authorship_name_aliases: tuple[str, ...]
    reason: str
    reviewed_at: str
    reviewed_by: str | None
    manifest_sha256: str

    def audit_evidence(self) -> dict[str, Any]:
        return {
            "reviewed": True,
            "audit_type": REVIEWED_IDENTITY_AUDIT_TYPE,
            "schema_version": 1,
            "manifest_sha256": self.manifest_sha256,
            "reason": self.reason,
            "reviewed_at": self.reviewed_at,
            "reviewed_by": self.reviewed_by,
            "expected_display_name": self.expected_display_name,
            "institution_id": self.institution_id,
            "primary_openalex_author_id": self.primary_openalex_author_id,
            "confirmed_openalex_author_ids": list(self.confirmed_openalex_author_ids),
            "sync_mode": self.sync_mode,
            "coverage_limit": (
                "reviewed_official_evidence_only"
                if self.sync_mode == "official_evidence_only"
                else None
            ),
            "official_works": [dict(item) for item in self.official_works],
            "work_policy": dict(self.work_policy) if self.work_policy else None,
            "work_policy_sha256": (
                _work_policy_sha256(self.work_policy) if self.work_policy else None
            ),
            "authorship_name_aliases": list(self.authorship_name_aliases),
        }


class OpenAlexSyncClient(Protocol):
    def get_author_by_orcid(self, orcid: str) -> dict[str, Any] | None: ...

    def get_author(self, openalex_author_id: str) -> dict[str, Any] | None: ...

    def get_work_by_doi(self, doi: str) -> dict[str, Any] | None: ...

    def get_work(self, openalex_work_id: str) -> dict[str, Any] | None: ...

    def search_authors(
        self,
        name: str,
        ror_id: str | None = None,
        limit: int = 10,
        *,
        filter: Any = None,
    ) -> list[dict[str, Any]]: ...

    def search_works(
        self,
        query: str,
        limit: int = 10,
        institution_id: str | None = None,
        *,
        filter: Any = None,
        sort: str | None = None,
        exact: bool = False,
    ) -> list[dict[str, Any]]: ...

    def fetch_works_for_author(
        self,
        openalex_author_id: str,
        *,
        per_page: int = 100,
        since_updated_date: str | datetime | None = None,
        filter: Any = None,
        premium_updated_filter: bool = False,
        sort: str | None = None,
        stop_before_updated_date: str | datetime | None = None,
    ) -> OpenAlexWorksResult: ...


class OpenAlexSyncStorage(Protocol):
    def iter_pi_records(self, include_inactive: bool = False) -> Iterable[Any]: ...

    def start_openalex_sync_run(
        self,
        run_id: str,
        institution_id: str | None = None,
        *,
        sync_mode: str = "delta",
        full_snapshot: bool = False,
        started_at: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def finish_openalex_sync_run(
        self,
        run_id: str,
        status: str,
        metrics: dict[str, Any] | None = None,
        *,
        error_reason: str | None = None,
        finished_at: str | None = None,
    ) -> dict[str, Any]: ...

    def get_openalex_author_link(self, person_id: str) -> dict[str, Any] | None: ...

    def get_openalex_identity_probe_cache(
        self, probe_key: str
    ) -> dict[str, Any] | None: ...

    def upsert_openalex_identity_probe_cache(
        self,
        probe_key: str,
        probe_version: str,
        evidence_kind: str,
        evidence_value: str,
        works: Iterable[Mapping[str, Any]],
        *,
        fetched_at: str,
        expires_at: str,
        run_id: str | None = None,
    ) -> dict[str, Any]: ...

    def upsert_openalex_author_link(
        self,
        person_id: str,
        institution_id: str,
        openalex_author_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def upsert_openalex_work(
        self,
        work: dict[str, Any],
        run_id: str,
        *,
        observed_at: str | None = None,
        dry_run: bool = False,
        enqueue_vectors: bool = True,
    ) -> dict[str, Any]: ...

    def reconcile_openalex_person_works(
        self,
        person_id: str,
        institution_id: str,
        openalex_author_id: str | None,
        run_id: str,
        observed_work_ids: Iterable[str],
        *,
        full_snapshot: bool,
        missing_runs_before_tombstone: int = 2,
        dry_run: bool = False,
        observed_at: str | None = None,
        enqueue_vectors: bool = True,
        relationship_evidence: Mapping[str, Any] | None = None,
        observed_work_author_ids: Mapping[str, str | None] | None = None,
        archive_existing_author_link: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def iter_current_openalex_works(
        self,
        *,
        person_id: str | None = None,
        institution_id: str | None = None,
        openalex_author_id: str | None = None,
    ) -> Iterable[dict[str, Any]]: ...

    def complete_openalex_sync_jobs(
        self,
        person_id: str,
        run_id: str,
        *,
        completed_at: str | None = None,
    ) -> int: ...


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _work_id(work: Mapping[str, Any]) -> str:
    value = str(work.get("id") or work.get("openalex_work_id") or "")
    match = WORK_ID_RE.search(value)
    if not match:
        raise OpenAlexProtocolError(f"OpenAlex work is missing a valid Work ID: {value!r}")
    return match.group(1).upper()


def _department_text(record: Any) -> str:
    values = [str(_value(record, "department") or "")]
    values.extend(str(value) for value in (_value(record, "departments", []) or []))
    return "\n".join(values).casefold()


def _selected_records(
    storage: OpenAlexSyncStorage,
    *,
    person_ids: Sequence[str] | None,
    institution_id: str | None,
    department_patterns: Sequence[str] | None,
    limit: int | None,
) -> list[Any]:
    institution_id = str(institution_id or "").strip()
    if not institution_id:
        raise ValueError("institution_id is required for OpenAlex synchronization")
    requested_ids = (
        list(dict.fromkeys(str(value).strip() for value in person_ids))
        if person_ids is not None
        else []
    )
    if any(not value for value in requested_ids):
        raise ValueError("person_ids cannot contain empty values")
    patterns = [
        str(value).casefold().strip()
        for value in (department_patterns or [])
        if str(value).strip()
    ]
    if not requested_ids and not patterns:
        raise ValueError("department_patterns or non-empty person_ids is required")

    requested_set = set(requested_ids)
    records = [
        record
        for record in storage.iter_pi_records()
        if _value(record, "institution_id") == institution_id
        and (not requested_ids or str(_value(record, "person_id") or "") in requested_set)
        and (not patterns or any(pattern in _department_text(record) for pattern in patterns))
    ]
    records.sort(
        key=lambda record: (
            str(_value(record, "department") or "").casefold(),
            str(_value(record, "person_id") or ""),
        )
    )
    matched_ids = {str(_value(record, "person_id") or "") for record in records}
    missing = [person_id for person_id in requested_ids if person_id not in matched_ids]
    if missing:
        raise ValueError(f"requested person_ids were not all matched: {', '.join(missing)}")
    if limit is not None:
        records = records[: max(0, int(limit))]
        if requested_ids:
            limited_ids = {str(_value(record, "person_id") or "") for record in records}
            truncated = [person_id for person_id in requested_ids if person_id not in limited_ids]
            if truncated:
                raise ValueError(f"limit excluded requested person_ids: {', '.join(truncated)}")
    if not records:
        raise ValueError("OpenAlex selection matched zero PI records")
    return records


def _required_manifest_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"reviewed identity manifest requires non-empty {field}")
    return value.strip()


def _strict_manifest_author_id(value: Any, field: str) -> str:
    text = _required_manifest_text(value, field)
    match = re.fullmatch(
        r"(?:https://openalex\.org/)?(A\d+)/?",
        text,
        flags=re.I,
    )
    if match is None:
        raise ValueError(
            f"reviewed identity manifest {field} is not an exact OpenAlex Author ID"
        )
    return match.group(1).upper()


def _strict_manifest_work_id(value: Any, field: str) -> str:
    text = _required_manifest_text(value, field)
    match = re.fullmatch(
        r"(?:https://openalex\.org/)?(W\d+)/?",
        text,
        flags=re.I,
    )
    if match is None:
        raise ValueError(
            f"reviewed identity manifest {field} is not an exact OpenAlex Work ID"
        )
    return match.group(1).upper()


def _strict_manifest_doi(value: Any, field: str) -> str:
    text = _required_manifest_text(value, field)
    normalized = _normalize_doi(text)
    if re.fullmatch(r"10\.\d{4,9}/\S+", normalized, flags=re.I) is None:
        raise ValueError(f"reviewed identity manifest {field} is not a valid DOI")
    return normalized


def _work_policy_sha256(policy: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(policy),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_reviewed_work_policy(raw: Any, *, index: int) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"reviewed identity manifest links[{index}].work_policy must be an object"
        )
    mode = raw.get("mode")
    if mode == "exact_work_allowlist":
        raw_work_ids = raw.get("work_ids")
        if not isinstance(raw_work_ids, list) or not raw_work_ids:
            raise ValueError(
                f"reviewed identity manifest links[{index}].work_policy.work_ids must be "
                "a non-empty list"
            )
        work_ids = [
            _strict_manifest_work_id(
                value,
                f"links[{index}].work_policy.work_ids[{work_index}]",
            )
            for work_index, value in enumerate(raw_work_ids)
        ]
        if len(set(work_ids)) != len(work_ids):
            raise ValueError(
                f"reviewed identity manifest links[{index}].work_policy contains duplicate "
                "work_ids"
            )
        return {"mode": "exact_work_allowlist", "work_ids": work_ids}
    if mode != "field_allowlist":
        raise ValueError(
            f"reviewed identity manifest links[{index}].work_policy mode must be "
            "'field_allowlist' or 'exact_work_allowlist'"
        )
    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ValueError(
            f"reviewed identity manifest links[{index}].work_policy.fields must be "
            "a non-empty list"
        )
    fields = [
        _required_manifest_text(
            value,
            f"links[{index}].work_policy.fields[{field_index}]",
        )
        for field_index, value in enumerate(raw_fields)
    ]
    if len({value.casefold() for value in fields}) != len(fields):
        raise ValueError(
            f"reviewed identity manifest links[{index}].work_policy.fields contains duplicates"
        )
    raw_always = raw.get("always_include_work_ids", [])
    if not isinstance(raw_always, list):
        raise ValueError(
            f"reviewed identity manifest links[{index}].work_policy.always_include_work_ids "
            "must be a list"
        )
    always = [
        _strict_manifest_work_id(
            value,
            f"links[{index}].work_policy.always_include_work_ids[{work_index}]",
        )
        for work_index, value in enumerate(raw_always)
    ]
    if len(set(always)) != len(always):
        raise ValueError(
            f"reviewed identity manifest links[{index}].work_policy contains duplicate "
            "always_include_work_ids"
        )
    return {
        "mode": "field_allowlist",
        "fields": fields,
        "always_include_work_ids": always,
    }


def _normalize_reviewed_authorship_name_aliases(
    raw: Any,
    *,
    index: int,
    expected_display_name: str,
) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(
            f"reviewed identity manifest links[{index}].authorship_name_aliases must be "
            "a list"
        )
    expected_tokens = _name_tokens(expected_display_name)
    if len(expected_tokens) < 2:
        raise ValueError(
            f"reviewed identity manifest links[{index}].expected_display_name must contain "
            "at least two name tokens before authorship aliases can be reviewed"
        )
    expected_surname = expected_tokens[-1]
    expected_given = {token for token in expected_tokens[:-1] if len(token) > 1}
    aliases: list[str] = []
    seen: set[str] = set()
    for alias_index, value in enumerate(raw):
        field = f"links[{index}].authorship_name_aliases[{alias_index}]"
        alias = " ".join(_required_manifest_text(value, field).split())
        alias_tokens = _name_tokens(alias)
        if len(alias_tokens) < 2:
            raise ValueError(
                f"reviewed identity manifest {field} must contain at least two name tokens"
            )
        surname_indexes = [
            token_index
            for token_index, token in enumerate(alias_tokens)
            if _surname_equivalent(token, expected_surname)
        ]
        if not surname_indexes:
            raise ValueError(
                f"reviewed identity manifest {field} surname must match the last surname "
                "token in expected_display_name"
            )
        alias_given = {
            token
            for token_index, token in enumerate(alias_tokens)
            if token_index not in surname_indexes and len(token) > 1
        }
        if not expected_given.intersection(alias_given):
            raise ValueError(
                f"reviewed identity manifest {field} must share at least one substantive "
                "given-name token with expected_display_name"
            )
        key = _normalize_title(alias)
        if key in seen:
            raise ValueError(
                f"reviewed identity manifest links[{index}].authorship_name_aliases "
                "contains duplicates"
            )
        seen.add(key)
        aliases.append(alias)
    return tuple(aliases)


def _load_reviewed_openalex_identity_manifest(
    path: str | Path,
) -> dict[str, _ReviewedOpenAlexIdentity]:
    manifest_path = Path(path).resolve()
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"Reviewed OpenAlex identity manifest does not exist: {manifest_path}"
        )
    manifest_bytes = manifest_path.read_bytes()
    try:
        payload = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Reviewed OpenAlex identity manifest is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Reviewed OpenAlex identity manifest root must be an object")
    if payload.get("schema_version") != 1:
        raise ValueError("Reviewed OpenAlex identity manifest schema_version must be 1")
    if payload.get("audit_type") != REVIEWED_IDENTITY_AUDIT_TYPE:
        raise ValueError(
            "Reviewed OpenAlex identity manifest audit_type must be "
            f"{REVIEWED_IDENTITY_AUDIT_TYPE!r}"
        )
    reviewed_at = _required_manifest_text(payload.get("reviewed_at"), "reviewed_at")
    if _parse_iso(reviewed_at) is None:
        raise ValueError("reviewed identity manifest reviewed_at must be ISO-8601")
    raw_reviewed_by = payload.get("reviewed_by")
    reviewed_by = None
    if raw_reviewed_by is not None:
        reviewed_by = _required_manifest_text(raw_reviewed_by, "reviewed_by")
    raw_links = payload.get("links")
    if not isinstance(raw_links, list) or not raw_links:
        raise ValueError("reviewed identity manifest links must be a non-empty list")

    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    decisions: dict[str, _ReviewedOpenAlexIdentity] = {}
    claimed_author_ids: dict[str, str] = {}
    for index, raw in enumerate(raw_links):
        if not isinstance(raw, Mapping):
            raise ValueError(f"reviewed identity manifest links[{index}] must be an object")
        person_id = _required_manifest_text(raw.get("person_id"), f"links[{index}].person_id")
        if person_id in decisions:
            raise ValueError(
                f"reviewed identity manifest contains duplicate person_id {person_id}"
            )
        institution_id = _required_manifest_text(
            raw.get("institution_id"), f"links[{index}].institution_id"
        )
        expected_display_name = _required_manifest_text(
            raw.get("expected_display_name"),
            f"links[{index}].expected_display_name",
        )
        reason = _required_manifest_text(raw.get("reason"), f"links[{index}].reason")
        sync_mode = str(raw.get("sync_mode") or "full_profile").strip()
        if sync_mode not in {"full_profile", "official_evidence_only"}:
            raise ValueError(
                f"reviewed identity manifest links[{index}].sync_mode must be "
                "'full_profile' or 'official_evidence_only'"
            )
        raw_primary = raw.get("primary_openalex_author_id")
        raw_ids = raw.get("confirmed_openalex_author_ids")
        if raw_ids is None:
            raw_ids = []
        if not isinstance(raw_ids, list):
            raise ValueError(
                f"reviewed identity manifest links[{index}].confirmed_openalex_author_ids "
                "must be a list"
            )
        author_ids = [
            _strict_manifest_author_id(
                value,
                f"links[{index}].confirmed_openalex_author_ids[{author_index}]",
            )
            for author_index, value in enumerate(raw_ids)
        ]
        if len(set(author_ids)) != len(author_ids):
            raise ValueError(
                f"reviewed identity manifest links[{index}] contains duplicate Author IDs"
            )
        primary = (
            _strict_manifest_author_id(
                raw_primary,
                f"links[{index}].primary_openalex_author_id",
            )
            if raw_primary is not None
            else None
        )
        if sync_mode == "full_profile" and (primary is None or not author_ids):
            raise ValueError(
                f"reviewed identity manifest links[{index}] full_profile requires a "
                "primary and at least one confirmed OpenAlex Author ID"
            )
        if (primary is None) != (not author_ids):
            raise ValueError(
                f"reviewed identity manifest links[{index}] must provide both primary and "
                "confirmed Author IDs, or neither"
            )
        if primary is not None and primary not in author_ids:
            raise ValueError(
                f"reviewed identity manifest links[{index}] primary Author ID must be "
                "included in confirmed_openalex_author_ids"
            )
        ordered_ids = (
            (primary, *(value for value in author_ids if value != primary))
            if primary is not None
            else ()
        )

        raw_official_works = raw.get("official_works", [])
        if not isinstance(raw_official_works, list):
            raise ValueError(
                f"reviewed identity manifest links[{index}].official_works must be a list"
            )
        official_works: list[dict[str, str | None]] = []
        seen_work_references: set[tuple[str, str]] = set()
        for work_index, raw_work in enumerate(raw_official_works):
            if not isinstance(raw_work, Mapping):
                raise ValueError(
                    f"reviewed identity manifest links[{index}].official_works[{work_index}] "
                    "must be an object"
                )
            work_id = (
                _strict_manifest_work_id(
                    raw_work.get("openalex_work_id"),
                    f"links[{index}].official_works[{work_index}].openalex_work_id",
                )
                if raw_work.get("openalex_work_id") is not None
                else None
            )
            doi = (
                _strict_manifest_doi(
                    raw_work.get("doi"),
                    f"links[{index}].official_works[{work_index}].doi",
                )
                if raw_work.get("doi") is not None
                else None
            )
            expected_title = (
                _required_manifest_text(
                    raw_work.get("expected_title"),
                    f"links[{index}].official_works[{work_index}].expected_title",
                )
                if raw_work.get("expected_title") is not None
                else None
            )
            expected_openalex_title = (
                _required_manifest_text(
                    raw_work.get("expected_openalex_title"),
                    f"links[{index}].official_works[{work_index}].expected_openalex_title",
                )
                if raw_work.get("expected_openalex_title") is not None
                else None
            )
            if work_id is None and doi is None:
                raise ValueError(
                    f"reviewed identity manifest links[{index}].official_works[{work_index}] "
                    "requires an OpenAlex Work ID or DOI"
                )
            if doi is None and expected_title is None:
                raise ValueError(
                    f"reviewed identity manifest links[{index}].official_works[{work_index}] "
                    "with only a Work ID also requires expected_title"
                )
            reference_key = (work_id or "", doi or "")
            if reference_key in seen_work_references:
                raise ValueError(
                    f"reviewed identity manifest links[{index}] contains duplicate official "
                    "Work references"
                )
            seen_work_references.add(reference_key)
            official_works.append(
                {
                    "openalex_work_id": work_id,
                    "doi": doi,
                    "expected_title": expected_title,
                    "expected_openalex_title": expected_openalex_title,
                }
            )
        if sync_mode == "official_evidence_only" and not official_works:
            raise ValueError(
                f"reviewed identity manifest links[{index}] official_evidence_only requires "
                "at least one official_works entry"
            )
        if sync_mode == "full_profile" and official_works:
            raise ValueError(
                f"reviewed identity manifest links[{index}] full_profile must not include "
                "official_works"
            )
        work_policy = _normalize_reviewed_work_policy(raw.get("work_policy"), index=index)
        if work_policy is not None and sync_mode != "full_profile":
            raise ValueError(
                f"reviewed identity manifest links[{index}].work_policy is only valid for "
                "full_profile"
            )
        authorship_name_aliases = _normalize_reviewed_authorship_name_aliases(
            raw.get("authorship_name_aliases"),
            index=index,
            expected_display_name=expected_display_name,
        )
        if "authorship_name_aliases" in raw and sync_mode != "full_profile":
            raise ValueError(
                f"reviewed identity manifest links[{index}].authorship_name_aliases is only "
                "valid for full_profile"
            )
        for author_id in ordered_ids:
            previous_person = claimed_author_ids.get(author_id)
            if previous_person is not None and previous_person != person_id:
                raise ValueError(
                    f"reviewed identity manifest assigns {author_id} to both "
                    f"{previous_person} and {person_id}"
                )
            claimed_author_ids[author_id] = person_id
        decisions[person_id] = _ReviewedOpenAlexIdentity(
            person_id=person_id,
            institution_id=institution_id,
            expected_display_name=expected_display_name,
            primary_openalex_author_id=primary,
            confirmed_openalex_author_ids=ordered_ids,
            sync_mode=sync_mode,
            official_works=tuple(official_works),
            work_policy=work_policy,
            authorship_name_aliases=authorship_name_aliases,
            reason=reason,
            reviewed_at=reviewed_at,
            reviewed_by=reviewed_by,
            manifest_sha256=manifest_sha256,
        )
    return decisions


def _preflight_reviewed_openalex_identities(
    storage: OpenAlexSyncStorage,
    records: Sequence[Any],
    institution_id: str,
    decisions: Mapping[str, _ReviewedOpenAlexIdentity],
) -> None:
    if not decisions:
        return
    selected = {
        str(_value(record, "person_id") or ""): record
        for record in records
    }
    for person_id, decision in decisions.items():
        record = selected.get(person_id)
        if record is None:
            raise ValueError(
                f"reviewed identity manifest person_id is outside selected scope: {person_id}"
            )
        actual_institution = str(_value(record, "institution_id") or "")
        if decision.institution_id != institution_id or actual_institution != institution_id:
            raise ValueError(
                f"reviewed identity manifest institution mismatch for {person_id}"
            )
        actual_display_name = " ".join(
            str(_value(record, "display_name") or "").split()
        )
        expected_display_name = " ".join(decision.expected_display_name.split())
        if actual_display_name != expected_display_name:
            raise ValueError(
                f"reviewed identity manifest display name mismatch for {person_id}: "
                f"expected {expected_display_name!r}, found {actual_display_name!r}"
            )

        if decision.sync_mode == "official_evidence_only":
            official_fingerprints = _official_identity_evidence(storage, person_id)
            for reference in decision.official_works:
                expected_doi = _normalize_doi(reference.get("doi"))
                expected_title = _normalize_title(reference.get("expected_title"))
                matched = any(
                    (not expected_doi or _normalize_doi(item.get("doi")) == expected_doi)
                    and (
                        not expected_title
                        or _normalize_title(item.get("title")) == expected_title
                    )
                    for item in official_fingerprints
                )
                if not matched:
                    label = reference.get("doi") or reference.get("openalex_work_id")
                    raise ValueError(
                        f"reviewed identity manifest official Work {label} is not backed by "
                        f"this PI's official publication fingerprints: {person_id}"
                    )

        existing = storage.get_openalex_author_link(person_id)
        if existing:
            status = str(_value(existing, "link_status") or "").casefold()
            if status != "confirmed":
                raise ValueError(
                    f"reviewed identity manifest conflicts with existing {status or 'unknown'} "
                    f"OpenAlex link for {person_id}"
                )
            if _author_link_has_reviewed_provenance(existing):
                existing_profiles = _persisted_profiles(existing)
                existing_primary = _normalize_author_id(existing.get("openalex_author_id"))
                existing_ids = {
                    _normalize_author_id(profile.get("openalex_author_id"))
                    for profile in existing_profiles
                }
                if (
                    existing_primary != decision.primary_openalex_author_id
                    or existing_ids != set(decision.confirmed_openalex_author_ids)
                ):
                    raise ValueError(
                        f"reviewed identity manifest conflicts with existing reviewed "
                        f"OpenAlex link for {person_id}"
                    )

    # OpenAlex Author profiles represent people, so an explicitly reviewed ID
    # must not already belong to another confirmed PI.  Include secondary split
    # profiles from persisted evidence, not just the primary table column.
    reviewed_owner = {
        author_id: person_id
        for person_id, decision in decisions.items()
        for author_id in decision.confirmed_openalex_author_ids
    }
    for record in storage.iter_pi_records(include_inactive=True):
        other_person_id = str(_value(record, "person_id") or "")
        existing = storage.get_openalex_author_link(other_person_id)
        if not existing or str(_value(existing, "link_status") or "").casefold() != "confirmed":
            continue
        for profile in _persisted_profiles(existing):
            author_id = _normalize_author_id(profile.get("openalex_author_id"))
            owner = reviewed_owner.get(author_id)
            if owner is not None and owner != other_person_id:
                raise ValueError(
                    f"reviewed identity manifest Author ID {author_id} is already confirmed "
                    f"for another person_id: {other_person_id}"
                )


def _author_link_has_reviewed_provenance(link: Mapping[str, Any]) -> bool:
    """Fail closed when a persisted Author link carries human-review provenance."""

    evidence = _value(link, "evidence", {})
    if isinstance(evidence, Mapping):
        if evidence.get("reviewed") is True or evidence.get("manually_reviewed") is True:
            return True
        # The presence of a structured reviewed-identity payload is itself
        # review provenance.  Older rows did not always persist the nested
        # ``reviewed`` boolean, so requiring it would make those rows unsafe to
        # supersede automatically.
        if isinstance(evidence.get("reviewed_identity"), Mapping):
            return True
    method = str(_value(link, "match_method") or "").casefold()
    return any(marker in method for marker in ("reviewed", "manual", "audited"))


def _normalize_orcid(value: Any) -> str | None:
    match = ORCID_RE.search(str(value or ""))
    return match.group(1).upper() if match else None


def _canonical_orcids(record: Any) -> set[str]:
    values: set[str] = set()

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                visit(child, key)
        elif "orcid" in key.casefold() and (normalized := _normalize_orcid(value)):
            values.add(normalized)

    visit(_value(record, "external_ids", {}) or {})
    return values


def _canonical_names(record: Any) -> list[str]:
    values = [str(_value(record, "display_name") or "")]
    values.extend(str(value) for value in (_value(record, "aliases", []) or []))
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(value.split())
        key = _normalize_title(text)
        if text and key and key not in seen:
            seen.add(key)
            output.append(text)
    return output


def _author_id(author: Mapping[str, Any]) -> str:
    return _normalize_author_id(author.get("id"))


def _normalize_author_id(value: Any) -> str:
    match = AUTHOR_ID_RE.search(str(value or ""))
    return match.group(1).upper() if match else ""


def _author_works_count(author: Mapping[str, Any]) -> int | None:
    value = author.get("works_count")
    try:
        count = int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return max(0, count) if count is not None else None


def _ror_key(value: Any) -> str:
    match = ROR_RE.search(str(value or ""))
    return match.group(1).casefold() if match else ""


def _author_rors(author: Mapping[str, Any]) -> set[str]:
    values: list[Any] = []
    for institution in author.get("last_known_institutions") or []:
        if isinstance(institution, Mapping):
            values.append(institution.get("ror"))
    for affiliation in author.get("affiliations") or []:
        if not isinstance(affiliation, Mapping):
            continue
        institution = affiliation.get("institution") or {}
        if isinstance(institution, Mapping):
            values.append(institution.get("ror"))
    return {key for value in values if (key := _ror_key(value))}


def _merge_author_candidates(groups: Iterable[Iterable[dict[str, Any]]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for author in group:
            author_id = _author_id(author)
            if not author_id or author_id in seen:
                continue
            seen.add(author_id)
            output.append(author)
    return output


def _candidate_orcid(author: Mapping[str, Any]) -> str | None:
    return _normalize_orcid(
        author.get("orcid") or (author.get("ids") or {}).get("orcid")
    )


def _normalize_doi(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", text)


def _normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(character for character in text if not unicodedata.combining(character))
    return " ".join("".join(character if character.isalnum() else " " for character in text).split())


def _name_tokens(value: Any) -> tuple[str, ...]:
    text = re.sub(r"\([^)]*\)", " ", str(value or ""))
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = text.translate(NAME_CONFUSABLES)
    return tuple(re.findall(r"[^\W\d_]+", text, flags=re.UNICODE))


def _surname_variants(value: str) -> set[str]:
    value = value.casefold()
    collapsed = value.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    return {value, collapsed}


def _surname_equivalent(left: str, right: str) -> bool:
    return bool(_surname_variants(left) & _surname_variants(right))


def _given_token_compatible(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    return (len(left) == 1 or len(right) == 1) and left[0] == right[0]


def _canonical_name_parts(record: Any) -> list[tuple[str, tuple[str, ...], str]]:
    family_tokens = _name_tokens(_value(record, "family_name"))
    explicit_family = family_tokens[-1] if family_tokens else ""
    parts: list[tuple[str, tuple[str, ...], str]] = []
    for name in _canonical_names(record):
        tokens = _name_tokens(name)
        if len(tokens) < 2:
            continue
        family = explicit_family or tokens[-1]
        family_index = next(
            (
                index
                for index in range(len(tokens) - 1, -1, -1)
                if _surname_equivalent(tokens[index], family)
            ),
            len(tokens) - 1,
        )
        given = tuple(token for index, token in enumerate(tokens) if index != family_index)
        if given:
            parts.append((family, given, name))
    return parts


def _reviewed_authorship_alias_parts(
    record: Any,
    aliases: Sequence[str],
) -> list[tuple[str, tuple[str, ...], str]]:
    display_tokens = _name_tokens(_value(record, "display_name"))
    if len(display_tokens) < 2:
        return []
    family = display_tokens[-1]
    parts: list[tuple[str, tuple[str, ...], str]] = []
    for alias in aliases:
        tokens = _name_tokens(alias)
        family_index = next(
            (
                index
                for index in range(len(tokens) - 1, -1, -1)
                if _surname_equivalent(tokens[index], family)
            ),
            -1,
        )
        if family_index < 0:
            continue
        given = tuple(token for index, token in enumerate(tokens) if index != family_index)
        if given:
            parts.append((family, given, alias))
    return parts


def _compatible_authorship_name(
    record: Any,
    authorship_names: Sequence[str],
    *,
    reviewed_authorship_name_aliases: Sequence[str] = (),
) -> dict[str, Any] | None:
    canonical_parts = [
        (*part, "canonical_name") for part in _canonical_name_parts(record)
    ]
    canonical_parts.extend(
        (*part, "reviewed_authorship_name_alias")
        for part in _reviewed_authorship_alias_parts(
            record,
            reviewed_authorship_name_aliases,
        )
    )
    for authorship_name in authorship_names:
        tokens = _name_tokens(authorship_name)
        if len(tokens) < 2:
            continue
        for family, canonical_given, canonical_name, name_source in canonical_parts:
            family_indexes = [
                index
                for index, token in enumerate(tokens)
                if _surname_equivalent(token, family)
            ]
            for family_index in family_indexes:
                candidate_given = tuple(
                    token for index, token in enumerate(tokens) if index != family_index
                )
                substantive_given = tuple(
                    token for token in canonical_given if len(token) > 1
                ) or canonical_given

                matched_pairs: list[tuple[str, str]] = []

                def match_all(offset: int, used: set[int]) -> bool:
                    if offset >= len(substantive_given):
                        return True
                    canonical_token = substantive_given[offset]
                    for candidate_index, candidate_token in enumerate(candidate_given):
                        if candidate_index in used or not _given_token_compatible(
                            canonical_token, candidate_token
                        ):
                            continue
                        used.add(candidate_index)
                        matched_pairs.append((canonical_token, candidate_token))
                        if match_all(offset + 1, used):
                            return True
                        matched_pairs.pop()
                        used.remove(candidate_index)
                    return False

                if match_all(0, set()):
                    return {
                        "canonical_name": canonical_name,
                        "canonical_name_source": name_source,
                        "authorship_name": authorship_name,
                        "family_name": family,
                        "canonical_given_tokens": list(substantive_given),
                        "authorship_given_tokens": list(candidate_given),
                        "matched_given_tokens": [list(pair) for pair in matched_pairs],
                    }
    return None


def _official_evidence_key(item: Mapping[str, Any]) -> str:
    doi = _normalize_doi(item.get("doi"))
    if doi:
        return f"doi:{doi}"
    title = _normalize_title(item.get("title"))
    return f"title:{title}" if title else ""


def _work_matches_official(item: Mapping[str, Any], work: Mapping[str, Any]) -> dict[str, str] | None:
    doi = _normalize_doi(item.get("doi"))
    work_doi = _normalize_doi(work.get("doi"))
    if doi and work_doi == doi:
        return {"kind": "doi", "value": doi, "openalex_work_id": _work_id(work)}
    title = _normalize_title(item.get("title"))
    work_title = _normalize_title(work.get("title") or work.get("display_name"))
    if title and work_title == title:
        return {
            "kind": "normalized_title",
            "value": title,
            "openalex_work_id": _work_id(work),
        }
    return None


def _quoted_title_query(value: Any) -> str:
    title = " ".join(str(value or "").split())
    escaped = title.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"' if escaped else ""


def _probe_official_work(
    client: OpenAlexSyncClient,
    item: Mapping[str, Any],
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    doi = _normalize_doi(item.get("doi"))
    get_by_doi = getattr(client, "get_work_by_doi", None)
    if doi and callable(get_by_doi):
        work = get_by_doi(doi)
        if work is not None and _work_matches_official(item, work):
            matches.append(dict(work))
    if not matches:
        query = _quoted_title_query(item.get("title"))
        search_works = getattr(client, "search_works", None)
        if query and callable(search_works):
            for work in search_works(query, limit=10, exact=True):
                if _work_matches_official(item, work):
                    matches.append(dict(work))
    deduped: dict[str, dict[str, Any]] = {}
    for work in matches:
        deduped.setdefault(_work_id(work), work)
    return [deduped[key] for key in sorted(deduped)]


def _fetch_reviewed_official_works(
    client: OpenAlexSyncClient,
    references: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    works: dict[str, dict[str, Any]] = {}
    for reference in references:
        expected_id = str(reference.get("openalex_work_id") or "")
        expected_doi = _normalize_doi(reference.get("doi"))
        expected_title = _normalize_title(
            reference.get("expected_openalex_title")
            or reference.get("expected_title")
        )
        work: dict[str, Any] | None = None
        get_work = getattr(client, "get_work", None)
        if expected_id and callable(get_work):
            work = get_work(expected_id)
        elif expected_doi:
            work = client.get_work_by_doi(expected_doi)
        else:
            raise OpenAlexConfigurationError(
                "OpenAlex client cannot fetch a reviewed Work ID singleton"
            )
        if work is None:
            raise OpenAlexProtocolError(
                f"Reviewed official Work was not found in OpenAlex: "
                f"{expected_id or expected_doi}"
            )
        actual_id = _work_id(work)
        actual_doi = _normalize_doi(work.get("doi"))
        actual_title = _normalize_title(work.get("title") or work.get("display_name"))
        if expected_id and actual_id != expected_id:
            raise OpenAlexProtocolError(
                f"Reviewed official Work ID mismatch: expected {expected_id}, found {actual_id}"
            )
        if expected_doi and actual_doi != expected_doi:
            raise OpenAlexProtocolError(
                f"Reviewed official Work DOI mismatch for {actual_id}: expected "
                f"{expected_doi}, found {actual_doi or 'missing'}"
            )
        if expected_title and actual_title != expected_title:
            raise OpenAlexProtocolError(
                f"Reviewed official Work title mismatch for {actual_id}"
            )
        works.setdefault(actual_id, dict(work))
    return [works[key] for key in sorted(works)]


def _primary_topic_field(work: Mapping[str, Any]) -> str:
    primary_topic = work.get("primary_topic") or {}
    if not isinstance(primary_topic, Mapping):
        return ""
    field = primary_topic.get("field") or {}
    if not isinstance(field, Mapping):
        return ""
    return " ".join(str(field.get("display_name") or "").split())


def _apply_reviewed_work_policy(
    works: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any] | None,
    *,
    full_snapshot: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    copied = [dict(work) for work in works]
    if policy is None:
        return copied, None
    mode = policy.get("mode")
    raw_ids = {_work_id(work) for work in copied}
    if mode == "exact_work_allowlist":
        allowed = {str(value) for value in policy.get("work_ids") or []}
        missing = sorted(allowed - raw_ids)
        if full_snapshot and missing:
            raise OpenAlexProtocolError(
                "Reviewed exact work allowlist missing from complete profile: "
                + ", ".join(missing)
            )
        selected = [work for work in copied if _work_id(work) in allowed]
        return selected, {
            "mode": "exact_work_allowlist",
            "policy_sha256": _work_policy_sha256(policy),
            "work_ids": sorted(allowed),
            "missing_work_ids": missing,
            "raw_work_count": len(copied),
            "selected_work_count": len(selected),
            "excluded_work_count": len(copied) - len(selected),
        }
    if mode != "field_allowlist":
        raise ValueError("Unsupported reviewed OpenAlex work policy")
    fields = {str(value).casefold() for value in policy.get("fields") or []}
    always = {str(value) for value in policy.get("always_include_work_ids") or []}
    missing_always = sorted(always - raw_ids)
    if full_snapshot and missing_always:
        raise OpenAlexProtocolError(
            "Reviewed work policy always_include_work_ids missing from complete profile: "
            + ", ".join(missing_always)
        )
    selected = [
        work
        for work in copied
        if _work_id(work) in always or _primary_topic_field(work).casefold() in fields
    ]
    audit = {
        "mode": "field_allowlist",
        "policy_sha256": _work_policy_sha256(policy),
        "fields": list(policy.get("fields") or []),
        "always_include_work_ids": sorted(always),
        "raw_work_count": len(copied),
        "selected_work_count": len(selected),
        "excluded_work_count": len(copied) - len(selected),
    }
    return selected, audit


def _reviewed_policy_always_work_ids(policy: Mapping[str, Any] | None) -> set[str]:
    if not policy:
        return set()
    if policy.get("mode") == "exact_work_allowlist":
        return {str(value) for value in policy.get("work_ids") or []}
    return {str(value) for value in policy.get("always_include_work_ids") or []}


def _work_authorships(work: Mapping[str, Any]) -> list[tuple[str, list[str]]]:
    output: list[tuple[str, list[str]]] = []
    for authorship in work.get("authorships") or []:
        if not isinstance(authorship, Mapping):
            continue
        author = authorship.get("author") or {}
        if not isinstance(author, Mapping):
            continue
        author_id = _normalize_author_id(author.get("id"))
        if not author_id:
            continue
        names = [
            str(value)
            for value in (author.get("display_name"), authorship.get("raw_author_name"))
            if str(value or "").strip()
        ]
        output.append((author_id, list(dict.fromkeys(names))))
    return output


def _augment_with_reviewed_official_authorship_overrides(
    storage: OpenAlexSyncStorage,
    record: Any,
    person_id: str,
    raw_works: Sequence[Mapping[str, Any]],
    compatible_works: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any] | None,
    *,
    reviewed_authorship_name_aliases: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Recover reviewed official Works whose OpenAlex author slots are shifted.

    OpenAlex occasionally attaches the confirmed Author ID to a coauthor's raw
    name while another authorship on the same Work contains the canonical PI
    name.  A manifest ``always_include_work_ids`` entry, or an exact reviewed
    Work allowlist entry, may override only that slot-level defect, and only
    when the Work is independently backed by the PI's active official
    title/DOI evidence.
    """

    copied = [dict(work) for work in compatible_works]
    if not policy:
        return copied, None
    always = _reviewed_policy_always_work_ids(policy)
    present = {_work_id(work) for work in copied}
    requested = sorted(always - present)
    if not requested:
        return copied, None

    raw_by_id = {_work_id(work): dict(work) for work in raw_works}
    official = _official_identity_evidence(storage, person_id)
    official_titles = {
        _normalize_title(item.get("title"))
        for item in official
        if _normalize_title(item.get("title"))
    }
    official_dois = {
        _normalize_doi(item.get("doi"))
        for item in official
        if _normalize_doi(item.get("doi"))
    }
    accepted: list[str] = []
    rejected: dict[str, str] = {}
    for work_id in requested:
        work = raw_by_id.get(work_id)
        if work is None:
            rejected[work_id] = "missing_from_complete_profile"
            continue
        title_match = _normalize_title(
            work.get("title") or work.get("display_name")
        ) in official_titles
        doi_match = bool(
            _normalize_doi(work.get("doi"))
            and _normalize_doi(work.get("doi")) in official_dois
        )
        if not (title_match or doi_match):
            rejected[work_id] = "not_backed_by_official_publication_evidence"
            continue
        names = [
            name
            for _author_id, authorship_names in _work_authorships(work)
            for name in authorship_names
        ]
        if _compatible_authorship_name(
            record,
            list(dict.fromkeys(names)),
            reviewed_authorship_name_aliases=reviewed_authorship_name_aliases,
        ) is None:
            rejected[work_id] = "no_canonical_compatible_authorship_on_work"
            continue
        copied.append(work)
        accepted.append(work_id)
    audit = {
        "requested_work_ids": requested,
        "accepted_work_ids": accepted,
        "rejected_work_ids": rejected,
        "rule": "exact_official_work_and_any_canonical_compatible_authorship",
    }
    if rejected:
        raise OpenAlexProtocolError(
            "Reviewed official authorship overrides failed: "
            + ", ".join(f"{key}={value}" for key, value in sorted(rejected.items()))
        )
    return copied, audit


def _profile_work_authorship_evidence(
    record: Any,
    openalex_author_id: str,
    work: Mapping[str, Any],
    *,
    reviewed_authorship_name_aliases: Sequence[str] = (),
) -> tuple[dict[str, Any] | None, str | None]:
    matched_author_id = False
    names: list[str] = []
    for authorship_id, authorship_names in _work_authorships(work):
        if authorship_id != openalex_author_id:
            continue
        matched_author_id = True
        names.extend(authorship_names)
    if not matched_author_id:
        return None, "missing_profile_authorship"
    evidence = _compatible_authorship_name(
        record,
        list(dict.fromkeys(names)),
        reviewed_authorship_name_aliases=reviewed_authorship_name_aliases,
    )
    if evidence is None:
        return None, "incompatible_profile_authorship_name"
    return evidence, None


def _filter_profile_results_by_authorship(
    record: Any,
    profile_results: Mapping[str, OpenAlexWorksResult],
    *,
    reviewed_authorship_name_aliases: Sequence[str] = (),
) -> tuple[dict[str, OpenAlexWorksResult], dict[str, Any], list[str]]:
    filtered: dict[str, OpenAlexWorksResult] = {}
    audits: dict[str, Any] = {}
    zero_compatible_profiles: list[str] = []
    for author_id, result in profile_results.items():
        accepted: list[dict[str, Any]] = []
        rejected_reasons: dict[str, int] = {}
        for work in result.works:
            _evidence, reason = _profile_work_authorship_evidence(
                record,
                author_id,
                work,
                reviewed_authorship_name_aliases=reviewed_authorship_name_aliases,
            )
            if reason is None:
                accepted.append(dict(work))
            else:
                rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
        if result.works and not accepted:
            zero_compatible_profiles.append(author_id)
        filtered[author_id] = OpenAlexWorksResult(
            works=accepted,
            meta_count=result.meta_count,
            pages_fetched=result.pages_fetched,
            raw_results_count=result.raw_results_count,
            terminal_cursor=result.terminal_cursor,
            stopped_at_cutoff=result.stopped_at_cutoff,
            cursors=result.cursors,
        )
        raw_count = len(result.works)
        audits[author_id] = {
            "raw_work_count": raw_count,
            "compatible_work_count": len(accepted),
            "rejected_work_count": raw_count - len(accepted),
            "compatible_fraction": (
                round(len(accepted) / raw_count, 6) if raw_count else None
            ),
            "rejected_reasons": rejected_reasons,
        }
    return filtered, audits, zero_compatible_profiles


def _perfect_distinct_evidence_assignment(
    supports: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, str] | None:
    candidates = sorted(supports, key=lambda author_id: (len(supports[author_id]), author_id))
    evidence_owner: dict[str, str] = {}

    def assign(author_id: str, seen: set[str]) -> bool:
        for evidence_key in sorted(supports[author_id]):
            if evidence_key in seen:
                continue
            seen.add(evidence_key)
            owner = evidence_owner.get(evidence_key)
            if owner is None or assign(owner, seen):
                evidence_owner[evidence_key] = author_id
                return True
        return False

    for author_id in candidates:
        if not assign(author_id, set()):
            return None
    return {author_id: evidence for evidence, author_id in evidence_owner.items()}


def _official_identity_evidence(storage: OpenAlexSyncStorage, person_id: str) -> list[dict[str, Any]]:
    loader = getattr(storage, "get_official_publication_identity_evidence", None)
    if callable(loader):
        return [dict(item) for item in loader(person_id)]
    conn = getattr(storage, "conn", None)
    if conn is None:
        return []
    rows = conn.execute(
        """
        SELECT f.title, f.doi
        FROM official_publication_fingerprints f
        WHERE f.person_id=?
          AND (
                NOT EXISTS (
                    SELECT 1 FROM official_publication_source_claims c
                    WHERE c.fingerprint_id=f.fingerprint_id
                )
                OR EXISTS (
                    SELECT 1 FROM official_publication_source_claims c
                    WHERE c.fingerprint_id=f.fingerprint_id
                      AND c.claim_status IN ('active', 'no_longer_observed')
                )
          )
        """,
        (person_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _publication_overlap(
    official: Sequence[Mapping[str, Any]],
    works: Sequence[Mapping[str, Any]],
) -> dict[str, str] | None:
    official_dois = {_normalize_doi(item.get("doi")) for item in official} - {""}
    official_titles = {_normalize_title(item.get("title")) for item in official} - {""}
    for work in works:
        doi = _normalize_doi(work.get("doi"))
        if doi and doi in official_dois:
            return {"kind": "doi", "value": doi, "openalex_work_id": _work_id(work)}
        title = _normalize_title(work.get("title") or work.get("display_name"))
        if title and title in official_titles:
            return {"kind": "normalized_title", "value": title, "openalex_work_id": _work_id(work)}
    return None


def _bounded_identity_supports(
    client: OpenAlexSyncClient,
    storage: OpenAlexSyncStorage,
    record: Any,
    official: Sequence[Mapping[str, Any]],
    strict_candidates: Sequence[Mapping[str, Any]],
    ror_id: str,
    *,
    work_cache: dict[str, list[dict[str, Any]]],
    author_cache: dict[str, dict[str, Any] | None],
    metrics: dict[str, Any],
    dry_run: bool,
    run_id: str,
    probe_time: datetime,
) -> tuple[
    dict[str, dict[str, dict[str, Any]]],
    dict[str, dict[str, Any]],
]:
    candidate_by_id = {
        author_id: dict(candidate)
        for candidate in strict_candidates
        if (author_id := _author_id(candidate))
    }
    strict_ids = set(candidate_by_id)
    supports: dict[str, dict[str, dict[str, Any]]] = {}
    seen_official: set[str] = set()
    target_ror = _ror_key(ror_id)
    get_author = getattr(client, "get_author", None)

    for item in official:
        evidence_key = _official_evidence_key(item)
        if not evidence_key or evidence_key in seen_official:
            continue
        seen_official.add(evidence_key)
        if evidence_key in work_cache:
            metrics["identity_work_cache_hits"] += 1
            works = work_cache[evidence_key]
        else:
            works: list[dict[str, Any]] | None = None
            probe_key = f"{IDENTITY_PROBE_CACHE_VERSION}|{evidence_key}"
            cache_loader = getattr(storage, "get_openalex_identity_probe_cache", None)
            if callable(cache_loader):
                cached = cache_loader(probe_key)
                if cached:
                    expires_at = _parse_iso(cached.get("expires_at"))
                    current_probe_time = probe_time
                    if current_probe_time.tzinfo is None:
                        current_probe_time = current_probe_time.replace(tzinfo=timezone.utc)
                    current_probe_time = current_probe_time.astimezone(timezone.utc)
                    if expires_at is not None and expires_at > current_probe_time:
                        raw_cached_works = cached.get("works")
                        cache_status = str(cached.get("result_status") or "")
                        cache_hash = str(cached.get("works_sha256") or "")
                        if isinstance(raw_cached_works, list):
                            cache_json = json.dumps(
                                raw_cached_works,
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                            valid_hash = (
                                hashlib.sha256(cache_json.encode("utf-8")).hexdigest()
                                == cache_hash
                            )
                            cached_works = [
                                dict(work)
                                for work in raw_cached_works
                                if isinstance(work, Mapping)
                                and _work_matches_official(item, work)
                            ]
                            valid_status = (
                                (cache_status == "hit" and cached_works)
                                or (cache_status == "miss" and not raw_cached_works)
                            )
                            if valid_hash and valid_status:
                                works = cached_works
                                metrics["identity_work_persistent_cache_hits"] += 1
                    else:
                        metrics["identity_work_persistent_cache_expired"] += 1
                if works is None:
                    metrics["identity_work_persistent_cache_misses"] += 1
            if works is None:
                metrics["identity_work_probes"] += 1
                works = _probe_official_work(client, item)
                cache_writer = getattr(
                    storage,
                    "upsert_openalex_identity_probe_cache",
                    None,
                )
                if not dry_run and callable(cache_writer):
                    ttl = IDENTITY_PROBE_HIT_TTL if works else IDENTITY_PROBE_MISS_TTL
                    evidence_kind, _, evidence_value = evidence_key.partition(":")
                    cache_writer(
                        probe_key,
                        IDENTITY_PROBE_CACHE_VERSION,
                        evidence_kind,
                        evidence_value,
                        works,
                        fetched_at=_iso(probe_time),
                        expires_at=_iso(probe_time + ttl),
                        run_id=run_id,
                    )
                    metrics["identity_work_persistent_cache_writes"] += 1
            work_cache[evidence_key] = works

        fallback_for_evidence: dict[
            str,
            tuple[dict[str, Any], dict[str, Any], dict[str, str], dict[str, str]],
        ] = {}
        for work in works:
            overlap = _work_matches_official(item, work)
            if overlap is None:
                continue
            overlap = {**overlap, "official_evidence_key": evidence_key}
            metrics["identity_works_matched"] += 1
            for authorship_id, authorship_names in _work_authorships(work):
                name_evidence = _compatible_authorship_name(record, authorship_names)
                if authorship_id in strict_ids:
                    if name_evidence is None:
                        continue
                    supports.setdefault(authorship_id, {}).setdefault(
                        evidence_key,
                        {
                            **overlap,
                            "identity_path": "exact_name_and_ror",
                            "name_evidence": name_evidence,
                        },
                    )
                    continue

                if name_evidence is None or not callable(get_author):
                    continue
                if authorship_id not in author_cache:
                    metrics["identity_author_probes"] += 1
                    author_cache[authorship_id] = get_author(authorship_id)
                author = author_cache[authorship_id]
                if not author or _author_id(author) != authorship_id:
                    continue
                author_rors = _author_rors(author)
                if author_rors and target_ror not in author_rors:
                    continue
                ror_evidence = {
                    "ror_validation": (
                        "exact_ror" if target_ror in author_rors else "missing_ror_exception"
                    ),
                    "official_institution_ror": ror_id,
                    "candidate_rors": sorted(author_rors),
                }
                fallback_for_evidence.setdefault(
                    authorship_id,
                    (dict(author), name_evidence, ror_evidence, overlap),
                )

        # A relaxed authorship-name path is accepted only when the exact official
        # Work identifies one compatible author.  Exact-name+ROR candidates above
        # do not share this restriction.
        if len(fallback_for_evidence) == 1:
            fallback_id, (author, name_evidence, ror_evidence, overlap) = next(
                iter(fallback_for_evidence.items())
            )
            candidate_by_id.setdefault(fallback_id, author)
            supports.setdefault(fallback_id, {}).setdefault(
                evidence_key,
                {
                    **overlap,
                    "official_evidence_key": evidence_key,
                    "identity_path": "compatible_authorship_name_and_exact_official_work",
                    "name_evidence": name_evidence,
                    **ror_evidence,
                },
            )

    return supports, candidate_by_id


def _primary_author_id(
    author_ids: Sequence[str],
    supports: Mapping[str, Mapping[str, Mapping[str, Any]]],
    candidates: Mapping[str, Mapping[str, Any]],
    canonical_orcids: set[str],
) -> str:
    def key(author_id: str) -> tuple[int, int, int, str]:
        author = candidates[author_id]
        count = _author_works_count(author)
        return (
            -len(supports.get(author_id, {})),
            -int(_candidate_orcid(author) in canonical_orcids),
            -(count if count is not None else -1),
            author_id,
        )

    return min(author_ids, key=key)


def _profile_evidence(
    author_id: str,
    author: Mapping[str, Any],
    supports: Mapping[str, Mapping[str, Mapping[str, Any]]],
    assignment: Mapping[str, str] | None,
) -> dict[str, Any]:
    overlaps = [dict(supports[author_id][key]) for key in sorted(supports.get(author_id, {}))]
    assigned_key = assignment.get(author_id) if assignment else None
    assigned = dict(supports[author_id][assigned_key]) if assigned_key else None
    ror_validation = next(
        (
            str(overlap.get("ror_validation"))
            for overlap in overlaps
            if overlap.get("ror_validation")
        ),
        "exact_ror",
    )
    return {
        "openalex_author_id": author_id,
        "matched_name": author.get("display_name"),
        "orcid": _candidate_orcid(author),
        "works_count": _author_works_count(author),
        "overlap_count": len(overlaps),
        "assigned_identity_evidence": assigned,
        "identity_overlaps": overlaps,
        "ror_validation": ror_validation,
    }


def _persisted_profiles(existing_link: Mapping[str, Any]) -> list[dict[str, Any]]:
    primary = _normalize_author_id(existing_link.get("openalex_author_id"))
    if not primary:
        raise ValueError("confirmed OpenAlex author link has no valid author ID")
    evidence = existing_link.get("evidence") or {}
    if not isinstance(evidence, Mapping):
        evidence = {}
    evidence_primary = evidence.get("primary_openalex_author_id")
    if evidence_primary and _normalize_author_id(evidence_primary) != primary:
        raise ValueError("persisted split-profile primary author conflicts with author link")
    raw_profiles = evidence.get("confirmed_profiles") or []
    profiles: dict[str, dict[str, Any]] = {}
    if isinstance(raw_profiles, list):
        for raw in raw_profiles:
            if not isinstance(raw, Mapping):
                continue
            author_id = _normalize_author_id(raw.get("openalex_author_id"))
            if author_id:
                profiles[author_id] = dict(raw)
    raw_ids = evidence.get("confirmed_openalex_author_ids") or []
    if isinstance(raw_ids, list):
        for raw_id in raw_ids:
            author_id = _normalize_author_id(raw_id)
            if not author_id:
                raise ValueError("persisted confirmed OpenAlex author ID is invalid")
            profiles.setdefault(author_id, {"openalex_author_id": author_id})
    profiles.setdefault(primary, {"openalex_author_id": primary})
    ordered = [profiles.pop(primary)]
    ordered.extend(profiles[key] for key in sorted(profiles))
    return ordered


def _existing_work_ids(storage: OpenAlexSyncStorage, person_id: str) -> set[str]:
    return {
        str(item.get("openalex_work_id") or "")
        for item in storage.iter_current_openalex_works(person_id=person_id)
        if item.get("openalex_work_id")
    }


def _dedupe_works(result: OpenAlexWorksResult) -> tuple[list[dict[str, Any]], set[str]]:
    unique: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for work in result.works:
        work_id = _work_id(work)
        if work_id in identifiers:
            continue
        identifiers.add(work_id)
        unique.append(work)
    return unique, identifiers


def _full_snapshot_errors(
    result: OpenAlexWorksResult,
    unique_count: int,
    existing_count: int,
    minimum_inventory_ratio: float,
) -> list[str]:
    errors: list[str] = []
    if not result.terminal_cursor or result.stopped_at_cutoff:
        errors.append("cursor_did_not_reach_terminal_null")
    if result.meta_count is None:
        errors.append("meta_count_missing")
    elif result.meta_count != unique_count:
        errors.append(f"meta_count_{result.meta_count}_does_not_equal_unique_{unique_count}")
    if existing_count and unique_count == 0:
        errors.append("catastrophic_empty_existing_inventory")
    elif (
        existing_count >= 5
        and unique_count / existing_count < minimum_inventory_ratio
    ):
        errors.append(
            f"suspicious_inventory_drop_{existing_count}_to_{unique_count}"
        )
    return errors


def _new_vector_job_count(result: Mapping[str, Any]) -> int:
    jobs = result.get("vector_jobs")
    if isinstance(jobs, list):
        return sum(
            1
            for job in jobs
            if not isinstance(job, Mapping) or bool(job.get("created", True))
        )
    if isinstance(jobs, (int, float)) and not isinstance(jobs, bool):
        return int(jobs)
    counts = result.get("counts")
    if isinstance(counts, Mapping) and isinstance(counts.get("vector_jobs"), (int, float)):
        return int(counts["vector_jobs"])
    return 0


def _count(result: Mapping[str, Any], key: str) -> int:
    counts = result.get("counts")
    value = counts.get(key, 0) if isinstance(counts, Mapping) else result.get(key, 0)
    return int(value or 0) if not isinstance(value, (list, tuple, set, dict)) else len(value)


def _author_evidence(
    record: Any,
    author: Mapping[str, Any],
    identity_evidence: Mapping[str, Any],
    confirmed_profiles: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    primary_id = _author_id(author)
    profiles = [dict(profile) for profile in (confirmed_profiles or [])]
    if not profiles and primary_id:
        profiles = [
            {
                "openalex_author_id": primary_id,
                "matched_name": author.get("display_name"),
                "orcid": _candidate_orcid(author),
                "works_count": _author_works_count(author),
                "overlap_count": int(bool(identity_evidence)),
                "assigned_identity_evidence": dict(identity_evidence),
                "identity_overlaps": [dict(identity_evidence)] if identity_evidence else [],
                "ror_validation": "exact_ror",
            }
        ]
    profile_ids = [
        author_id
        for profile in profiles
        if (author_id := _normalize_author_id(profile.get("openalex_author_id")))
    ]
    return {
        "source": "openalex",
        "identity_rule": IDENTITY_RULE,
        "official_person_name": _value(record, "display_name"),
        "official_institution_ror": _value(record, "ror_id"),
        "matched_name": author.get("display_name"),
        "identity_evidence": dict(identity_evidence),
        "candidate": dict(author),
        "primary_openalex_author_id": primary_id,
        "confirmed_openalex_author_ids": profile_ids,
        "confirmed_profiles": profiles,
        "split_profile": len(profile_ids) > 1,
    }


def _snapshot_audit(result: OpenAlexWorksResult, unique_count: int) -> dict[str, Any]:
    return {
        "meta_count": result.meta_count,
        "pages_fetched": result.pages_fetched,
        "raw_results_count": result.raw_results_count,
        "unique_work_count": unique_count,
        "terminal_cursor": result.terminal_cursor,
        "stopped_at_cutoff": result.stopped_at_cutoff,
    }


def _union_profile_works(
    results: Mapping[str, OpenAlexWorksResult],
) -> tuple[list[dict[str, Any]], set[str]]:
    works: list[dict[str, Any]] = []
    seen: set[str] = set()
    for author_id in results:
        for work in results[author_id].works:
            work_id = _work_id(work)
            if work_id in seen:
                continue
            seen.add(work_id)
            works.append(work)
    return works, seen


def _selected_work_profile_provenance(
    selected_works: Sequence[Mapping[str, Any]],
    profile_results: Mapping[str, OpenAlexWorksResult],
) -> dict[str, str]:
    """Assign each selected Work to a confirmed profile that supplied it.

    Mapping insertion order is significant: callers pass the primary Author
    first, so duplicate Works shared by split profiles retain deterministic
    primary-profile provenance.
    """

    selected_ids = {_work_id(work) for work in selected_works}
    provenance: dict[str, str] = {}
    for author_id, result in profile_results.items():
        normalized_author_id = _normalize_author_id(author_id)
        if not normalized_author_id:
            raise ValueError(f"Invalid confirmed profile ID: {author_id}")
        for work in result.works:
            work_id = _work_id(work)
            if work_id in selected_ids:
                provenance.setdefault(work_id, normalized_author_id)
    missing = sorted(selected_ids - set(provenance))
    if missing:
        raise RuntimeError(
            "Selected OpenAlex Works lost profile provenance: "
            + ", ".join(missing[:5])
        )
    return provenance


def _inventory_errors(
    unique_count: int,
    existing_count: int,
    minimum_inventory_ratio: float,
) -> list[str]:
    if existing_count and unique_count == 0:
        return ["catastrophic_empty_existing_inventory"]
    if existing_count >= 5 and unique_count / existing_count < minimum_inventory_ratio:
        return [f"suspicious_inventory_drop_{existing_count}_to_{unique_count}"]
    return []


def _profile_snapshot_audit(
    results: Mapping[str, OpenAlexWorksResult],
    unique_counts: Mapping[str, int],
    union_count: int,
) -> dict[str, Any]:
    if len(results) == 1:
        author_id = next(iter(results))
        audit = _snapshot_audit(results[author_id], unique_counts[author_id])
        audit["openalex_author_id"] = author_id
        return audit
    return {
        "profile_count": len(results),
        "union_unique_work_count": union_count,
        "profiles": {
            author_id: _snapshot_audit(result, unique_counts[author_id])
            for author_id, result in results.items()
        },
    }


def _initial_metrics(
    run_id: str,
    institution_id: str,
    *,
    dry_run: bool,
    force_full: bool,
    premium_updated_filter: bool,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "institution_id": institution_id,
        "dry_run": dry_run,
        "force_full": force_full,
        "premium_updated_filter": premium_updated_filter,
        "people_considered": 0,
        "people_resolved": 0,
        "people_unresolved": 0,
        "people_failed": 0,
        "people_skipped_auth_failure": 0,
        "full_snapshots": 0,
        "full_snapshots_rejected": 0,
        "delta_snapshots": 0,
        "works_fetched": 0,
        "works_new": 0,
        "works_changed": 0,
        "works_vector_text_changed": 0,
        "links_added": 0,
        "links_recovered": 0,
        "links_reactivated": 0,
        "links_missing": 0,
        "links_tombstoned": 0,
        "vector_jobs": 0,
        "source_sync_jobs_completed": 0,
        "identity_work_probes": 0,
        "identity_work_cache_hits": 0,
        "identity_work_persistent_cache_hits": 0,
        "identity_work_persistent_cache_misses": 0,
        "identity_work_persistent_cache_expired": 0,
        "identity_work_persistent_cache_writes": 0,
        "identity_works_matched": 0,
        "identity_author_probes": 0,
        "split_profiles_confirmed": 0,
        "identities_revalidation_requested": 0,
        "identities_marked_stale_for_revalidation": 0,
        "people_official_evidence_only": 0,
        "people_identity_pending": 0,
        "automatic_author_links_archived_for_identity_pending": 0,
        "automatic_author_links_archive_planned_for_identity_pending": 0,
        "works_authorship_rejected": 0,
        "works_authorship_rejected_reasons": {},
        "works_reviewed_authorship_overrides": 0,
        "works_policy_excluded": 0,
        "unresolved_reasons": {},
        "people": [],
        "errors": [],
    }


def _mark_unresolved(
    metrics: dict[str, Any],
    person_result: dict[str, Any],
    reason: str,
    *,
    status: str = "unresolved",
) -> None:
    metrics["people_unresolved"] += 1
    metrics["unresolved_reasons"][reason] = int(
        metrics["unresolved_reasons"].get(reason, 0)
    ) + 1
    person_result.update({"status": status, "reason": reason})
    metrics["people"].append(person_result)


def sync_openalex_publications(
    storage: OpenAlexSyncStorage,
    *,
    client: OpenAlexSyncClient | None = None,
    person_ids: Sequence[str] | None = None,
    institution_id: str | None = None,
    department_patterns: Sequence[str] | None = None,
    limit: int | None = None,
    full: bool = False,
    dry_run: bool = False,
    premium_updated_filter: bool = False,
    updated_date_overlap: timedelta = timedelta(days=2),
    full_refresh_interval: timedelta = timedelta(days=30),
    minimum_full_inventory_ratio: float = 0.2,
    missing_runs_before_tombstone: int = 2,
    max_author_works: int = DEFAULT_MAX_AUTHOR_WORKS,
    reviewed_identity_manifest: str | Path | None = None,
    revalidate_identities: bool = False,
    run_id: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Safely synchronize selected, identity-confirmed OpenAlex publications."""
    if updated_date_overlap < timedelta(0):
        raise ValueError("updated_date_overlap cannot be negative")
    if full_refresh_interval <= timedelta(0):
        raise ValueError("full_refresh_interval must be positive")
    if not 0 < minimum_full_inventory_ratio <= 1:
        raise ValueError("minimum_full_inventory_ratio must be in (0, 1]")
    if missing_runs_before_tombstone < 1:
        raise ValueError("missing_runs_before_tombstone must be at least 1")
    if isinstance(max_author_works, bool) or int(max_author_works) < 1:
        raise ValueError("max_author_works must be at least 1")
    max_author_works = int(max_author_works)

    records = _selected_records(
        storage,
        person_ids=person_ids,
        institution_id=institution_id,
        department_patterns=department_patterns,
        limit=limit,
    )
    reviewed_identities = (
        _load_reviewed_openalex_identity_manifest(reviewed_identity_manifest)
        if reviewed_identity_manifest is not None
        else {}
    )
    _preflight_reviewed_openalex_identities(
        storage,
        records,
        str(institution_id),
        reviewed_identities,
    )
    client = client or OpenAlexHTTPClient()
    clock = now or (lambda: datetime.now(timezone.utc))
    started_at = _iso(clock())
    run_id = run_id or f"openalex_sync_{uuid4().hex[:16]}"
    institution_id = str(institution_id)
    metrics = _initial_metrics(
        run_id,
        institution_id,
        dry_run=dry_run,
        force_full=full,
        premium_updated_filter=premium_updated_filter,
    )
    metrics["people_considered"] = len(records)
    metrics["selection"] = {
        "person_ids": list(person_ids) if person_ids is not None else None,
        "institution_id": institution_id,
        "department_patterns": list(department_patterns or []),
        "limit": limit,
        "max_author_works": max_author_works,
        "revalidate_identities": bool(revalidate_identities),
    }
    if reviewed_identities:
        first_review = next(iter(reviewed_identities.values()))
        metrics["reviewed_identity_manifest"] = {
            "audit_type": REVIEWED_IDENTITY_AUDIT_TYPE,
            "schema_version": 1,
            "manifest_sha256": first_review.manifest_sha256,
            "reviewed_link_count": len(reviewed_identities),
            "person_ids": sorted(reviewed_identities),
        }
    metrics["full_refresh_interval_days"] = full_refresh_interval.total_seconds() / 86400

    run_started = False
    if not dry_run:
        storage.start_openalex_sync_run(
            run_id,
            institution_id,
            sync_mode="selected_full" if full else "delta",
            full_snapshot=full,
            started_at=started_at,
            metrics=metrics,
        )
        run_started = True

    fatal_auth = False
    identity_work_cache: dict[str, list[dict[str, Any]]] = {}
    identity_author_cache: dict[str, dict[str, Any] | None] = {}
    for record_index, record in enumerate(records):
        person_id = str(_value(record, "person_id") or "")
        current_institution_id = str(_value(record, "institution_id") or "")
        display_name = str(_value(record, "display_name") or "")
        ror_id = str(_value(record, "ror_id") or "")
        person_result: dict[str, Any] = {
            "person_id": person_id,
            "institution_id": current_institution_id,
            "display_name": display_name,
        }
        try:
            reviewed_identity = reviewed_identities.get(person_id)
            existing_link = storage.get_openalex_author_link(person_id)
            if existing_link and existing_link.get("institution_id") != current_institution_id:
                raise ValueError("existing OpenAlex author link belongs to another institution")
            link_status = str(_value(existing_link, "link_status") or "").casefold()
            if link_status == "rejected":
                _mark_unresolved(metrics, person_result, "existing_link_rejected")
                continue
            existing_evidence = _value(existing_link, "evidence", {})
            existing_is_reviewed = bool(
                existing_link and _author_link_has_reviewed_provenance(existing_link)
            )
            revalidating_auto_identity = bool(
                revalidate_identities
                and reviewed_identity is None
                and existing_link
                and link_status == "confirmed"
                and not existing_is_reviewed
            )
            if revalidating_auto_identity:
                metrics["identities_revalidation_requested"] += 1
                if not dry_run:
                    existing_link = storage.upsert_openalex_author_link(
                        person_id,
                        current_institution_id,
                        str(existing_link.get("openalex_author_id") or ""),
                        link_status="stale",
                        confidence=_value(existing_link, "confidence"),
                        match_method=_value(existing_link, "match_method"),
                        evidence={
                            "identity_revalidation": {
                                "requested": True,
                                "run_id": run_id,
                                "requested_at": _iso(clock()),
                                "previous_link_status": "confirmed",
                            }
                        },
                        run_id=run_id,
                        verified_at=_iso(clock()),
                    )
                    metrics["identities_marked_stale_for_revalidation"] += 1
                link_status = "stale"
            if reviewed_identity is None and link_status == "confirmed":
                reviewed_audit = (
                    existing_evidence.get("reviewed_identity")
                    if isinstance(existing_evidence, Mapping)
                    else None
                )
                if isinstance(reviewed_audit, Mapping) and (
                    reviewed_audit.get("sync_mode") == "official_evidence_only"
                    or reviewed_audit.get("work_policy")
                    or reviewed_audit.get("authorship_name_aliases")
                ):
                    _mark_unresolved(
                        metrics,
                        person_result,
                        "reviewed_identity_manifest_required_for_limited_coverage",
                        status="manual_review",
                    )
                    continue

            author_id = ""
            author_ids: list[str] = []
            resolution = ""
            identity_evidence: dict[str, Any] = {}
            confirmed_profiles: list[dict[str, Any]] = []
            candidate_by_id: dict[str, dict[str, Any]] = {}
            reviewed_author: dict[str, Any] | None = None
            if reviewed_identity is not None:
                author_id = reviewed_identity.primary_openalex_author_id or ""
                author_ids = list(reviewed_identity.confirmed_openalex_author_ids)
                resolution = REVIEWED_IDENTITY_MATCH_METHOD
                identity_evidence = {
                    "kind": "reviewed_openalex_identity",
                    **reviewed_identity.audit_evidence(),
                }
                if author_id:
                    reviewed_author = {
                        "id": f"https://openalex.org/{author_id}",
                        "display_name": display_name,
                    }
                confirmed_profiles = [
                    {
                        "openalex_author_id": current_author_id,
                        "matched_name": None,
                        "orcid": None,
                        "works_count": None,
                        "overlap_count": 0,
                        "assigned_identity_evidence": None,
                        "identity_overlaps": [],
                        "ror_validation": "reviewed_identity",
                    }
                    for current_author_id in author_ids
                ]
            elif (
                existing_link
                and link_status == "confirmed"
                and not revalidating_auto_identity
            ):
                confirmed_profiles = _persisted_profiles(existing_link)
                author_ids = [
                    _normalize_author_id(profile.get("openalex_author_id"))
                    for profile in confirmed_profiles
                ]
                author_id = author_ids[0]
                resolution = "existing_confirmed_link"
            else:
                if not ror_id:
                    _mark_unresolved(metrics, person_result, "missing_official_ror")
                    continue
                canonical_names = _canonical_names(record)
                canonical_orcids = _canonical_orcids(record)
                direct_orcid_candidates: list[dict[str, Any]] = []
                get_by_orcid = getattr(client, "get_author_by_orcid", None)
                if canonical_orcids and callable(get_by_orcid):
                    direct_orcid_candidates = _merge_author_candidates(
                        [candidate]
                        for orcid in sorted(canonical_orcids)
                        if (candidate := get_by_orcid(orcid)) is not None
                    )
                    direct_orcid_candidates = strict_openalex_author_candidates(
                        canonical_names,
                        ror_id,
                        direct_orcid_candidates,
                    )
                if len(direct_orcid_candidates) > 1:
                    _mark_unresolved(metrics, person_result, "orcid_match_not_unique")
                    continue
                candidates = direct_orcid_candidates or _merge_author_candidates(
                    client.search_authors(name, ror_id=ror_id, limit=10)
                    for name in canonical_names
                )
                orcid_matches = [
                    candidate
                    for candidate in candidates
                    if _candidate_orcid(candidate) in canonical_orcids
                ]
                author: dict[str, Any] | None = None
                if canonical_orcids and len(orcid_matches) == 1 and (
                    bool(direct_orcid_candidates) or len(candidates) == 1
                ):
                    author = orcid_matches[0]
                    resolution = "canonical_orcid_exact"
                    identity_evidence = {
                        "kind": "orcid",
                        "value": _candidate_orcid(author),
                    }
                    author_id = _author_id(author)
                    author_ids = [author_id]
                    candidate_by_id = {author_id: dict(author)}
                    confirmed_profiles = [
                        _profile_evidence(author_id, author, {}, None)
                    ]
                elif len(orcid_matches) > 1:
                    _mark_unresolved(metrics, person_result, "orcid_match_not_unique")
                    continue
                else:
                    strict_candidates = strict_openalex_author_candidates(
                        canonical_names,
                        ror_id,
                        candidates,
                    )
                    official = _official_identity_evidence(storage, person_id)
                    if not official:
                        _mark_unresolved(
                            metrics,
                            person_result,
                            "no_orcid_or_official_publication_identity_evidence",
                            status="manual_review",
                        )
                        continue
                    supports, candidate_by_id = _bounded_identity_supports(
                        client,
                        storage,
                        record,
                        official,
                        strict_candidates,
                        ror_id,
                        work_cache=identity_work_cache,
                        author_cache=identity_author_cache,
                        metrics=metrics,
                        dry_run=dry_run,
                        run_id=run_id,
                        probe_time=clock(),
                    )
                    author_ids = sorted(supports)
                    if not author_ids:
                        _mark_unresolved(
                            metrics,
                            person_result,
                            "official_publication_did_not_match_candidate_works",
                            status="manual_review",
                        )
                        continue
                    assignment = _perfect_distinct_evidence_assignment(supports)
                    if assignment is None:
                        person_result["supported_openalex_author_ids"] = author_ids
                        _mark_unresolved(
                            metrics,
                            person_result,
                            "official_publication_evidence_not_distinct",
                            status="manual_review",
                        )
                        continue
                    author_id = _primary_author_id(
                        author_ids,
                        supports,
                        candidate_by_id,
                        canonical_orcids,
                    )
                    author = candidate_by_id[author_id]
                    ordered_ids = [author_id, *(item for item in author_ids if item != author_id)]
                    confirmed_profiles = [
                        _profile_evidence(
                            item,
                            candidate_by_id[item],
                            supports,
                            assignment,
                        )
                        for item in ordered_ids
                    ]
                    identity_evidence = dict(
                        supports[author_id][assignment[author_id]]
                    )
                    uses_fallback = any(
                        any(
                            overlap.get("identity_path")
                            == "compatible_authorship_name_and_exact_official_work"
                            for overlap in supports[item].values()
                        )
                        for item in author_ids
                    )
                    if len(author_ids) > 1:
                        resolution = (
                            "split_openalex_profiles_with_distinct_official_publication_overlaps"
                        )
                        metrics["split_profiles_confirmed"] += 1
                    elif uses_fallback:
                        resolution = (
                            "compatible_authorship_name_and_official_publication_overlap"
                        )
                    else:
                        resolution = "exact_name_ror_and_official_publication_overlap"
                if author is None or not author.get("id"):
                    _mark_unresolved(metrics, person_result, "identity_not_confirmed")
                    continue
                if not author_id:
                    author_id = _author_id(author)
                if not author_ids:
                    author_ids = [author_id]

                get_author = getattr(client, "get_author", None)
                for profile in confirmed_profiles:
                    if isinstance(profile.get("works_count"), int) or not callable(get_author):
                        continue
                    current_author_id = _normalize_author_id(
                        profile.get("openalex_author_id")
                    )
                    if current_author_id not in identity_author_cache:
                        metrics["identity_author_probes"] += 1
                        identity_author_cache[current_author_id] = get_author(current_author_id)
                    current_author = identity_author_cache[current_author_id]
                    if current_author:
                        profile["works_count"] = _author_works_count(current_author)
                over_limit = [
                    {
                        "openalex_author_id": profile.get("openalex_author_id"),
                        "works_count": profile.get("works_count"),
                    }
                    for profile in confirmed_profiles
                    if isinstance(profile.get("works_count"), int)
                    and int(profile["works_count"]) > max_author_works
                ]
                if over_limit:
                    person_result["author_work_count_guard"] = {
                        "max_author_works": max_author_works,
                        "profiles": over_limit,
                    }
                    _mark_unresolved(
                        metrics,
                        person_result,
                        "candidate_work_count_exceeds_limit",
                        status="manual_review",
                    )
                    continue
                if not dry_run:
                    existing_link = storage.upsert_openalex_author_link(
                        person_id,
                        current_institution_id,
                        str(author.get("id") or author_id),
                        link_status="confirmed",
                        confidence=1.0 if resolution == "canonical_orcid_exact" else 0.98,
                        match_method=resolution,
                        evidence=_author_evidence(
                            record,
                            author,
                            identity_evidence,
                            confirmed_profiles,
                        ),
                        run_id=run_id,
                        verified_at=_iso(clock()),
                    )

            get_author = getattr(client, "get_author", None)
            for profile in confirmed_profiles:
                if isinstance(profile.get("works_count"), int) or not callable(get_author):
                    continue
                current_author_id = _normalize_author_id(profile.get("openalex_author_id"))
                if current_author_id not in identity_author_cache:
                    metrics["identity_author_probes"] += 1
                    identity_author_cache[current_author_id] = get_author(current_author_id)
                current_author = identity_author_cache[current_author_id]
                if current_author:
                    profile["works_count"] = _author_works_count(current_author)
                    profile["matched_name"] = (
                        profile.get("matched_name") or current_author.get("display_name")
                    )
            persisted_over_limit = [
                {
                    "openalex_author_id": profile.get("openalex_author_id"),
                    "works_count": profile.get("works_count"),
                }
                for profile in confirmed_profiles
                if isinstance(profile.get("works_count"), int)
                and int(profile["works_count"]) > max_author_works
            ]
            if persisted_over_limit:
                person_result["author_work_count_guard"] = {
                    "max_author_works": max_author_works,
                    "profiles": persisted_over_limit,
                }
                _mark_unresolved(
                    metrics,
                    person_result,
                    "candidate_work_count_exceeds_limit",
                    status="manual_review",
                )
                continue

            if reviewed_identity is not None and author_id and not dry_run:
                assert reviewed_author is not None
                reviewed_evidence = _author_evidence(
                    record,
                    reviewed_author,
                    identity_evidence,
                    confirmed_profiles,
                )
                reviewed_evidence.update(
                    {
                        "identity_rule": REVIEWED_IDENTITY_MATCH_METHOD,
                        "reviewed_identity": reviewed_identity.audit_evidence(),
                    }
                )
                existing_link = storage.upsert_openalex_author_link(
                    person_id,
                    current_institution_id,
                    author_id,
                    link_status="confirmed",
                    confidence=1.0,
                    match_method=REVIEWED_IDENTITY_MATCH_METHOD,
                    evidence=reviewed_evidence,
                    run_id=run_id,
                    verified_at=_iso(clock()),
                )

            if (
                reviewed_identity is not None
                and reviewed_identity.sync_mode == "official_evidence_only"
            ):
                reviewed_works = _fetch_reviewed_official_works(
                    client,
                    reviewed_identity.official_works,
                )
                metrics["people_official_evidence_only"] += 1
                if not author_id:
                    metrics["people_identity_pending"] += 1
                metrics["works_fetched"] += len(reviewed_works)
                observed_at = _iso(clock())
                observed_work_ids: list[str] = []
                local_new = 0
                local_changed = 0
                local_vector_jobs = 0
                for work in reviewed_works:
                    change = storage.upsert_openalex_work(
                        work,
                        run_id,
                        observed_at=observed_at,
                        dry_run=dry_run,
                        enqueue_vectors=True,
                    )
                    observed_work_ids.append(str(change["work_id"]))
                    created = bool(change.get("created"))
                    changed = bool(
                        change.get("vector_text_changed")
                        or change.get("metadata_changed")
                    )
                    metrics["works_new"] += int(created)
                    local_new += int(created)
                    if changed and not created:
                        metrics["works_changed"] += 1
                        local_changed += 1
                    metrics["works_vector_text_changed"] += int(
                        bool(change.get("vector_text_changed"))
                    )
                    jobs = _new_vector_job_count(change)
                    metrics["vector_jobs"] += jobs
                    local_vector_jobs += jobs
                relationship_evidence = {
                    "relationship_method": "reviewed_official_evidence_only",
                    "identity_status": "confirmed" if author_id else "pending",
                    "coverage_limit": "reviewed_official_evidence_only",
                    "reviewed_identity": reviewed_identity.audit_evidence(),
                }
                archive_existing_author_link = None
                if not author_id and existing_link is not None:
                    archive_existing_author_link = {
                        "expected_openalex_author_id": _value(
                            existing_link, "openalex_author_id"
                        ),
                        "reason": (
                            "superseded_by_reviewed_official_evidence_only_"
                            "identity_pending"
                        ),
                    }
                reconciliation = storage.reconcile_openalex_person_works(
                    person_id,
                    current_institution_id,
                    author_id or None,
                    run_id,
                    observed_work_ids,
                    # The reviewed Work list is intentionally coverage-limited,
                    # but it is authoritative for the approved PI-Work
                    # relationship set.  Reconcile it as a full policy
                    # snapshot so legacy auto-profile contamination cannot
                    # remain current beside the reviewed Works.
                    full_snapshot=True,
                    missing_runs_before_tombstone=missing_runs_before_tombstone,
                    dry_run=dry_run,
                    observed_at=observed_at,
                    enqueue_vectors=True,
                    relationship_evidence=relationship_evidence,
                    archive_existing_author_link=archive_existing_author_link,
                )
                for metric_key, result_key in (
                    ("links_added", "added"),
                    ("links_recovered", "recovered"),
                    ("links_reactivated", "reactivated"),
                    ("links_missing", "missing"),
                    ("links_tombstoned", "tombstoned"),
                ):
                    metrics[metric_key] += _count(reconciliation, result_key)
                reconcile_jobs = _new_vector_job_count(reconciliation)
                metrics["vector_jobs"] += reconcile_jobs
                local_vector_jobs += reconcile_jobs
                author_link_archive = reconciliation.get("author_link_archive")
                if author_link_archive:
                    if dry_run:
                        metrics[
                            "automatic_author_links_archive_planned_for_identity_pending"
                        ] += 1
                    else:
                        metrics[
                            "automatic_author_links_archived_for_identity_pending"
                        ] += 1
                person_result.update(
                    {
                        "status": "resolved",
                        "resolution": REVIEWED_IDENTITY_MATCH_METHOD,
                        "openalex_author_id": author_id or None,
                        "openalex_author_ids": author_ids,
                        "identity_status": "confirmed" if author_id else "pending",
                        "sync_mode": "official_evidence_only",
                        "coverage_limit": "reviewed_official_evidence_only",
                        "snapshot_audit": {
                            "reviewed_reference_count": len(
                                reviewed_identity.official_works
                            ),
                            "verified_unique_work_count": len(reviewed_works),
                            "identity_refresh_pending": not bool(author_id),
                            "superseded_automatic_author_link": author_link_archive,
                        },
                        "works_fetched": len(reviewed_works),
                        "works_selected": len(reviewed_works),
                        "works_new": local_new,
                        "works_changed": local_changed,
                        "links_missing": _count(reconciliation, "missing"),
                        "links_tombstoned": _count(reconciliation, "tombstoned"),
                        "vector_jobs": local_vector_jobs,
                        "source_sync_jobs_completed": 0,
                    }
                )
                metrics["people_resolved"] += 1
                metrics["people"].append(person_result)
                continue

            last_success = _parse_iso(_value(existing_link, "last_successful_sync_at"))
            last_full = _parse_iso(_value(existing_link, "last_full_sync_at"))
            current_time = clock()
            if current_time.tzinfo is None:
                current_time = current_time.replace(tzinfo=timezone.utc)
            full_snapshot = bool(
                full
                or revalidating_auto_identity
                or reviewed_identity is not None
                or last_success is None
                or last_full is None
                or current_time.astimezone(timezone.utc) - last_full >= full_refresh_interval
            )
            cutoff: str | None = None
            profile_results: dict[str, OpenAlexWorksResult] = {}
            if full_snapshot:
                sync_mode = "full"
                for current_author_id in author_ids:
                    metrics["full_snapshots"] += 1
                    result = client.fetch_works_for_author(current_author_id, per_page=100)
                    metrics["works_fetched"] += result.raw_results_count
                    profile_results[current_author_id] = result
            else:
                cutoff = _iso(last_success - updated_date_overlap)
                sync_mode = (
                    "premium_delta" if premium_updated_filter else "free_updated_date_delta"
                )
                for current_author_id in author_ids:
                    metrics["delta_snapshots"] += 1
                    if premium_updated_filter:
                        result = client.fetch_works_for_author(
                            current_author_id,
                            per_page=100,
                            since_updated_date=cutoff,
                            premium_updated_filter=True,
                        )
                    else:
                        result = client.fetch_works_for_author(
                            current_author_id,
                            per_page=100,
                            sort="updated_date:desc",
                            stop_before_updated_date=cutoff,
                        )
                    metrics["works_fetched"] += result.raw_results_count
                    profile_results[current_author_id] = result

            profile_unique_counts = {
                current_author_id: len(_dedupe_works(result)[0])
                for current_author_id, result in profile_results.items()
            }
            raw_unique_works, _raw_unique_ids = _union_profile_works(profile_results)
            reviewed_authorship_name_aliases = (
                reviewed_identity.authorship_name_aliases
                if reviewed_identity is not None
                else ()
            )
            compatible_profile_results, authorship_audit, zero_compatible_profiles = (
                _filter_profile_results_by_authorship(
                    record,
                    profile_results,
                    reviewed_authorship_name_aliases=reviewed_authorship_name_aliases,
                )
            )
            for audit in authorship_audit.values():
                rejected_count = int(audit["rejected_work_count"])
                metrics["works_authorship_rejected"] += rejected_count
                for reason, count in audit["rejected_reasons"].items():
                    metrics["works_authorship_rejected_reasons"][reason] = int(
                        metrics["works_authorship_rejected_reasons"].get(reason, 0)
                    ) + int(count)
            compatible_works, _compatible_ids = _union_profile_works(
                compatible_profile_results
            )
            work_policy = reviewed_identity.work_policy if reviewed_identity else None
            compatible_works, official_override_audit = (
                _augment_with_reviewed_official_authorship_overrides(
                    storage,
                    record,
                    person_id,
                    raw_unique_works,
                    compatible_works,
                    work_policy,
                    reviewed_authorship_name_aliases=reviewed_authorship_name_aliases,
                )
            )
            if official_override_audit:
                metrics["works_reviewed_authorship_overrides"] += len(
                    official_override_audit["accepted_work_ids"]
                )
            unique_works, work_policy_audit = _apply_reviewed_work_policy(
                compatible_works,
                work_policy,
                full_snapshot=full_snapshot,
            )
            selected_work_author_ids = _selected_work_profile_provenance(
                unique_works,
                profile_results,
            )
            if work_policy_audit:
                metrics["works_policy_excluded"] += int(
                    work_policy_audit["excluded_work_count"]
                )
            snapshot_audit = _profile_snapshot_audit(
                profile_results,
                profile_unique_counts,
                len(raw_unique_works),
            )
            snapshot_audit["authorship_validation"] = authorship_audit
            if reviewed_authorship_name_aliases:
                snapshot_audit["reviewed_authorship_name_aliases"] = list(
                    reviewed_authorship_name_aliases
                )
            snapshot_audit["compatible_union_work_count"] = len(compatible_works)
            snapshot_audit["selected_union_work_count"] = len(unique_works)
            if work_policy_audit:
                snapshot_audit["reviewed_work_policy"] = work_policy_audit
            if official_override_audit:
                snapshot_audit["reviewed_official_authorship_overrides"] = (
                    official_override_audit
                )
            profile_evidence_by_id = {
                _normalize_author_id(profile.get("openalex_author_id")): profile
                for profile in confirmed_profiles
            }
            for current_author_id, audit in authorship_audit.items():
                profile_evidence_by_id.get(current_author_id, {})[
                    "last_authorship_validation"
                ] = audit
            if full_snapshot:
                profile_by_id = {
                    _normalize_author_id(profile.get("openalex_author_id")): profile
                    for profile in confirmed_profiles
                }
                profile_errors: dict[str, list[str]] = {}
                for current_author_id, result in profile_results.items():
                    previous_count = profile_by_id.get(current_author_id, {}).get(
                        "last_full_work_count"
                    )
                    previous_count = (
                        int(previous_count)
                        if isinstance(previous_count, int) and previous_count >= 0
                        else 0
                    )
                    errors = _full_snapshot_errors(
                        result,
                        profile_unique_counts[current_author_id],
                        previous_count,
                        minimum_full_inventory_ratio,
                    )
                    if result.meta_count is not None and result.meta_count > max_author_works:
                        errors.append(
                            f"meta_count_{result.meta_count}_exceeds_max_author_works_{max_author_works}"
                        )
                    if errors:
                        profile_errors[current_author_id] = errors
                for current_author_id in zero_compatible_profiles:
                    profile_errors.setdefault(current_author_id, []).append(
                        "zero_compatible_authorship_works"
                    )
                union_errors = (
                    []
                    if reviewed_identity is not None or revalidating_auto_identity
                    else _inventory_errors(
                        len(unique_works),
                        len(_existing_work_ids(storage, person_id)),
                        minimum_full_inventory_ratio,
                    )
                )
                proof_errors = [
                    f"{current_author_id}: {'; '.join(errors)}"
                    for current_author_id, errors in profile_errors.items()
                ]
                proof_errors.extend(f"union: {error}" for error in union_errors)
                if proof_errors:
                    metrics["full_snapshots_rejected"] += max(1, len(profile_errors))
                    metrics["people_failed"] += 1
                    error_text = "; ".join(proof_errors)
                    metrics["errors"].append(
                        {"person_id": person_id, "error": f"snapshot_rejected: {error_text}"}
                    )
                    person_result.update(
                        {
                            "status": "snapshot_rejected",
                            "resolution": resolution,
                            "snapshot_audit": snapshot_audit,
                            "error": error_text,
                        }
                    )
                    metrics["people"].append(person_result)
                    continue

            observed_work_ids: list[str] = []
            local_new = 0
            local_changed = 0
            local_vector_jobs = 0
            observed_at = _iso(clock())
            for work in unique_works:
                change = storage.upsert_openalex_work(
                    work,
                    run_id,
                    observed_at=observed_at,
                    dry_run=dry_run,
                    enqueue_vectors=True,
                )
                observed_work_ids.append(str(change["work_id"]))
                created = bool(change.get("created"))
                changed = bool(
                    change.get("vector_text_changed") or change.get("metadata_changed")
                )
                metrics["works_new"] += int(created)
                local_new += int(created)
                if changed and not created:
                    metrics["works_changed"] += 1
                    local_changed += 1
                metrics["works_vector_text_changed"] += int(
                    bool(change.get("vector_text_changed"))
                )
                jobs = _new_vector_job_count(change)
                metrics["vector_jobs"] += jobs
                local_vector_jobs += jobs

            reconciliation = storage.reconcile_openalex_person_works(
                person_id,
                current_institution_id,
                author_id,
                run_id,
                observed_work_ids,
                full_snapshot=full_snapshot,
                missing_runs_before_tombstone=missing_runs_before_tombstone,
                dry_run=dry_run,
                observed_at=observed_at,
                enqueue_vectors=True,
                observed_work_author_ids=selected_work_author_ids,
            )
            for metric_key, result_key in (
                ("links_added", "added"),
                ("links_recovered", "recovered"),
                ("links_reactivated", "reactivated"),
                ("links_missing", "missing"),
                ("links_tombstoned", "tombstoned"),
            ):
                metrics[metric_key] += _count(reconciliation, result_key)
            reconcile_jobs = _new_vector_job_count(reconciliation)
            metrics["vector_jobs"] += reconcile_jobs
            local_vector_jobs += reconcile_jobs
            source_jobs_completed = 0
            if not dry_run:
                if full_snapshot:
                    for profile in confirmed_profiles:
                        current_author_id = _normalize_author_id(
                            profile.get("openalex_author_id")
                        )
                        profile["last_full_work_count"] = profile_unique_counts[
                            current_author_id
                        ]
                        profile["last_full_snapshot_at"] = observed_at
                persisted_evidence = {
                    "primary_openalex_author_id": author_id,
                    "confirmed_openalex_author_ids": author_ids,
                    "confirmed_profiles": confirmed_profiles,
                    "split_profile": len(author_ids) > 1,
                }
                current_link = storage.get_openalex_author_link(person_id) or existing_link or {}
                current_evidence = _value(current_link, "evidence", {})
                if not isinstance(current_evidence, Mapping):
                    current_evidence = {}
                storage.upsert_openalex_author_link(
                    person_id,
                    current_institution_id,
                    _value(current_link, "openalex_author_id") or author_id,
                    link_status="confirmed",
                    confidence=_value(current_link, "confidence"),
                    match_method=_value(current_link, "match_method") or resolution,
                    evidence={**dict(current_evidence), **persisted_evidence},
                    run_id=run_id,
                    verified_at=observed_at,
                )
                complete_source_jobs = getattr(storage, "complete_openalex_sync_jobs", None)
                if callable(complete_source_jobs):
                    source_jobs_completed = int(
                        complete_source_jobs(
                            person_id,
                            run_id,
                            completed_at=observed_at,
                        )
                        or 0
                    )
                    metrics["source_sync_jobs_completed"] += source_jobs_completed
            person_result.update(
                {
                    "status": "resolved",
                    "resolution": resolution,
                    "openalex_author_id": author_id,
                    "openalex_author_ids": author_ids,
                    "sync_mode": sync_mode,
                    "cutoff_updated_date": cutoff,
                    "snapshot_audit": snapshot_audit,
                    "works_fetched": sum(
                        item.raw_results_count for item in profile_results.values()
                    ),
                    "works_selected": len(unique_works),
                    "authorship_validation": authorship_audit,
                    "work_policy": work_policy_audit,
                    "works_new": local_new,
                    "works_changed": local_changed,
                    "links_missing": _count(reconciliation, "missing"),
                    "links_tombstoned": _count(reconciliation, "tombstoned"),
                    "vector_jobs": local_vector_jobs,
                    "source_sync_jobs_completed": source_jobs_completed,
                }
            )
            metrics["people_resolved"] += 1
            metrics["people"].append(person_result)
        except (OpenAlexConfigurationError, OpenAlexHTTPError) as exc:
            metrics["people_failed"] += 1
            error = {"person_id": person_id, "error": f"{type(exc).__name__}: {exc}"}
            metrics["errors"].append(error)
            person_result.update({"status": "failed", **error})
            metrics["people"].append(person_result)
            if isinstance(exc, OpenAlexConfigurationError) or exc.status_code in {401, 403}:
                fatal_auth = True
                metrics["authentication_failure"] = {
                    "status_code": getattr(exc, "status_code", None),
                    "error": str(exc),
                }
                metrics["people_skipped_auth_failure"] = len(records) - record_index - 1
                break
        except Exception as exc:
            metrics["people_failed"] += 1
            error = {"person_id": person_id, "error": f"{type(exc).__name__}: {exc}"}
            metrics["errors"].append(error)
            person_result.update({"status": "failed", **error})
            metrics["people"].append(person_result)

    considered = int(metrics["people_considered"])
    metrics["coverage"] = {
        "considered": considered,
        "resolved": int(metrics["people_resolved"]),
        "unresolved": int(metrics["people_unresolved"]),
        "failed": int(metrics["people_failed"]),
        "skipped_auth_failure": int(metrics["people_skipped_auth_failure"]),
        "resolved_fraction": (
            round(int(metrics["people_resolved"]) / considered, 6) if considered else 0.0
        ),
    }
    status = "failed" if fatal_auth else "success"
    if not fatal_auth and metrics["people_failed"]:
        status = "failed" if metrics["people_failed"] == considered else "partial"
    metrics["status"] = status
    if run_started:
        storage.finish_openalex_sync_run(
            run_id,
            status,
            metrics,
            error_reason="; ".join(item["error"] for item in metrics["errors"]) or None,
            finished_at=_iso(clock()),
        )
    return metrics


__all__ = ["OpenAlexSyncStorage", "sync_openalex_publications"]
