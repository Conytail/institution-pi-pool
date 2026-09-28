from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import yaml

from ..adapters.institution_adapter import ConfigDrivenInstitutionAdapter
from ..config import load_institution_config
from ..crawl.archive import ContentArchive
from ..crawl.fetcher import Fetcher
from ..models import (
    CanonicalPIRecord,
    OfficialPublicationFingerprint,
    PersonEvidence,
    RawSourceRecord,
    content_hash,
    stable_id,
    utc_now_iso,
)
from ..normalize.department import normalize_department
from ..normalize.institution import institution_from_config
from ..normalize.person_name import (
    is_non_person_name as _is_non_person_name,
    is_title_contaminated_name as _is_title_contaminated_name,
    split_name,
)
from ..normalize.title import normalize_title
from ..normalize.topic import normalize_topics
from ..storage import (
    PIIndexStorage,
    is_unusable_profile_url,
    normalize_profile_url,
)
from ..verify.confidence import contact_verdict_for_pi
from ..verify.email import verify_email
from .snapshot import create_institution_snapshot


def load_yaml(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def load_crawl_policy(path: str | Path = "configs/crawl_policy.yaml") -> dict[str, Any]:
    policy_path = Path(path)
    if not policy_path.exists():
        return {}
    return load_yaml(policy_path)


def import_institutions_csv(input_path: str | Path, storage: PIIndexStorage) -> int:
    count = 0
    with Path(input_path).open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            config = {
                "institution": {
                    "name": row.get("name"),
                    "country": row.get("country") or None,
                    "region": row.get("region") or None,
                    "homepage_url": row.get("homepage_url") or None,
                    "ror_id": row.get("ror_id") or None,
                    "official_domains": [],
                    "qs_rank": int(row["qs_rank"]) if row.get("qs_rank") else None,
                    "qs_year": int(row["qs_year"]) if row.get("qs_year") else None,
                    "source": row.get("source") or "csv",
                }
            }
            record = institution_from_config(config, use_ror=False)
            storage.upsert_institution(record)
            count += 1
    return count


def _new_run_id(institution_id: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{institution_id.removeprefix('inst_')[:8]}-{uuid4().hex[:8]}"


def _evidence_for_person(person, institution_id: str, run_id: str) -> list[PersonEvidence]:
    extracted_at = utc_now_iso()
    fields = {
        "display_name": person.name,
        "title": person.title,
        "department": person.department,
        "profile_url": person.profile_url,
        "lab_url": person.lab_url,
        "emails": person.emails,
        "ambiguous_emails": person.ambiguous_emails,
        "research_areas": person.research_areas,
    }
    evidence: list[PersonEvidence] = []
    for field_name, value in fields.items():
        if value in (None, "", [], {}):
            continue
        values = value if isinstance(value, list) else [value]
        for field_value in values:
            evidence.append(
                PersonEvidence(
                    evidence_id=stable_id(
                        "ev",
                        run_id,
                        person.person_temp_id,
                        field_name,
                        field_value,
                        person.source_url,
                    ),
                    person_temp_id=person.person_temp_id,
                    institution_id=institution_id,
                    field_name=field_name,
                    field_value=str(field_value),
                    source_url=person.source_url,
                    source_type=person.source_type,
                    extraction_method=person.extraction_method,
                    extracted_at=extracted_at,
                    confidence=person.confidence,
                    evidence_text=person.evidence_text,
                    content_hash=content_hash(person.evidence_text),
                    run_id=run_id,
                )
            )
    return evidence


def _source_quality(source_type: str | None) -> int:
    source = (source_type or "").casefold()
    if "manual_verification" in source:
        return 100
    if "profile" in source:
        return 50
    if "jsonld" in source:
        return 45
    if "api" in source:
        return 35
    if "directory" in source:
        return 20
    return 0


def _title_quality(value: str | None) -> int:
    if not value:
        return -100
    text = " ".join(value.split())
    lower = text.casefold()
    if lower in {"academic staff", "faculty", "faculty member", "staff"}:
        return -20
    score = 10
    if re.search(r"\b(professor|lecturer|reader|research fellow|principal investigator|dean|director)\b", lower):
        score += 25
    if len(text) > 320 or len(text.split()) > 40:
        score -= 60
    if re.search(r"\b(biography|currently|joined|received|obtained|earned|worked|whose|where he|where she|he is|she is|he was|she was|they were|his |her )\b", lower):
        score -= 50
    if text.count(".") >= 2:
        score -= 30
    return score


def _is_known_content_listing_observation(person: Any) -> bool:
    """Reject observations extracted from an explicitly identified news feed.

    The CityU CALAS WordPress archive was enqueued through stale directory
    links.  Its ``?p=`` articles and ``?paged=`` listings are publication/news
    content, not person profiles; a generic card title such as ``Gavin Li`` is
    therefore not PI identity evidence.  The host/path/query conjunction keeps
    this source-family rule narrow and does not reject an ordinary person with
    the same name elsewhere.
    """

    parsed = urlparse(str(getattr(person, "source_url", "") or ""))
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    path = parsed.path.casefold().rstrip("/")
    query_keys = {key.casefold() for key in parse_qs(parsed.query)}
    return bool(
        (host == "www4.ee.cityu.edu.hk" or host.endswith(".www4.ee.cityu.edu.hk"))
        and path == "/calas"
        and query_keys.intersection({"p", "paged"})
    )


def _sanitize_person_profile_url(person: Any) -> bool:
    """Strip a non-web/error link without dropping the real person row."""

    if not is_unusable_profile_url(getattr(person, "profile_url", None)):
        return False
    person.profile_url = None
    return True


def _is_non_person_observation(person: Any) -> bool:
    """Reject page chrome/error documents before they can become identities.

    Adapter chains intentionally allow a conservative generic profile parser
    after institution-specific parsers.  A redirected HTTP error document, or
    decorative legacy text such as ``Welcome to Jane Doe's HomePage``, is not
    an independent person observation even when its words happen to satisfy a
    generic name heuristic.  Enforce that invariant once at the ingestion
    boundary so no adapter can insert evidence, observations, aliases, or a
    canonical record for such content.
    """

    source_url = str(getattr(person, "source_url", "") or "")
    parsed_source = urlparse(source_url)
    if (
        parsed_source.scheme.casefold() in {"http", "https"}
        and is_unusable_profile_url(source_url)
    ):
        return True
    return _is_known_content_listing_observation(person) or _is_non_person_name(
        str(getattr(person, "name", "") or "")
    )


def _name_quality(value: str | None) -> int:
    if not value:
        return -100
    text = " ".join(value.split())
    if _is_non_person_name(text):
        # Keep section headings below every plausible person name, but above
        # a missing value.  This specifically prevents headings such as
        # ``Current research`` from outscoring a concise two-token name.
        return -90
    tokens = re.findall(r"[^\W\d_]+", text, flags=re.UNICODE)
    score = min(len(text), 80) + min(len(tokens), 6) * 12
    if len(tokens) < 2:
        score -= 80
    return score


def _profile_url_quality(value: str | None) -> int:
    if not value:
        return -100
    normalized = normalize_profile_url(value)
    score = len([part for part in normalized.split("?", 1)[0].split("/") if part])
    if re.search(r"/(?:people|persons|profile|profiles|rp|staff)/[^/]+", normalized, re.I):
        score += 20
    if normalized.rsplit("/", 1)[-1] in {"academic-staff", "directory", "faculty", "people", "staff"}:
        score -= 30
    return score


def _pick_quality_value(
    existing_value: str | None,
    incoming_value: str | None,
    existing_source: str | None,
    incoming_source: str | None,
    quality,
    *,
    prefer_incoming_on_tie: bool = False,
) -> tuple[str | None, str | None]:
    existing_score = (quality(existing_value), _source_quality(existing_source))
    incoming_score = (quality(incoming_value), _source_quality(incoming_source))
    if incoming_score > existing_score or (
        prefer_incoming_on_tie
        and incoming_value is not None
        and incoming_score == existing_score
    ):
        return incoming_value, incoming_source
    return existing_value, existing_source


def _observed_at(record: CanonicalPIRecord) -> datetime | None:
    """Return the best comparable timestamp for a canonical observation."""

    for value in (record.last_seen_at, record.last_checked_at):
        if not value:
            continue
        try:
            observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return observed.astimezone(timezone.utc)
    return None


def _incoming_is_fresher(
    existing: CanonicalPIRecord,
    incoming: CanonicalPIRecord,
) -> bool:
    existing_at = _observed_at(existing)
    incoming_at = _observed_at(incoming)
    return bool(incoming_at and (existing_at is None or incoming_at > existing_at))


def _split_departments(values: list[str | None]) -> list[str]:
    departments: list[str] = []
    for value in values:
        for item in (value or "").split(";"):
            normalized = " ".join(item.split())
            if normalized and normalized.casefold() not in {current.casefold() for current in departments}:
                departments.append(normalized)
    return departments


def _merge_external_ids(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in sorted(set(existing).union(incoming)):
        values: list[Any] = []
        for source in (existing, incoming):
            value = source.get(key)
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if candidate not in (None, "") and candidate not in values:
                    values.append(candidate)
        if values:
            merged[key] = values[0] if len(values) == 1 else values
    return merged


def _merge_records(existing: CanonicalPIRecord, incoming: CanonicalPIRecord) -> CanonicalPIRecord:
    same_run = bool(existing.last_seen_run_id and existing.last_seen_run_id == incoming.last_seen_run_id)
    incoming_is_fresher = _incoming_is_fresher(existing, incoming)
    existing_sources = dict(getattr(existing, "field_sources", None) or {})
    incoming_sources = dict(getattr(incoming, "field_sources", None) or {})
    observed_incoming_name = incoming.display_name
    observed_incoming_profile_url = (
        incoming.profile_url
        if not is_unusable_profile_url(incoming.profile_url)
        else None
    )
    if is_unusable_profile_url(incoming.profile_url):
        incoming.profile_url = None
    incoming.first_seen_at = existing.first_seen_at or incoming.first_seen_at
    existing_profile_identities = {
        normalize_profile_url(value)
        for value in [
            existing.profile_url,
            *(getattr(existing, "profile_urls", None) or []),
        ]
        if value and not is_unusable_profile_url(value)
    }
    incoming_observed_profile_identity = (
        normalize_profile_url(observed_incoming_profile_url)
        if observed_incoming_profile_url
        else None
    )
    incoming_observes_existing_profile = bool(
        incoming_observed_profile_identity
        and incoming_observed_profile_identity in existing_profile_identities
    )
    existing_display_source = existing_sources.get("display_name")
    incoming_display_source = incoming_sources.get("display_name")
    manual_name_is_authoritative = bool(
        existing.display_name
        and "manual_verification" in (existing_display_source or "").casefold()
        and _source_quality(existing_display_source) > _source_quality(incoming_display_source)
    )
    current_same_profile_name = bool(
        (incoming_is_fresher or same_run)
        and incoming_observes_existing_profile
        and incoming.display_name
        and not _is_non_person_name(incoming.display_name)
        and not _is_title_contaminated_name(incoming.display_name)
        and _name_quality(incoming.display_name) >= 0
        and _source_quality(incoming_display_source) >= _source_quality(existing_display_source)
    )
    if manual_name_is_authoritative:
        incoming.display_name = existing.display_name
        display_source = existing_display_source
    elif current_same_profile_name:
        display_source = incoming_display_source
    else:
        incoming.display_name, display_source = _pick_quality_value(
            existing.display_name,
            incoming.display_name,
            existing_display_source,
            incoming_display_source,
            _name_quality,
            prefer_incoming_on_tie=incoming_is_fresher,
        )
    if incoming.display_name == existing.display_name:
        incoming.given_name = existing.given_name or incoming.given_name
        incoming.family_name = existing.family_name or incoming.family_name
    existing_title_source = existing_sources.get("title")
    incoming_title_source = incoming_sources.get("title")
    current_same_profile_title = bool(
        (incoming_is_fresher or same_run)
        and incoming_observes_existing_profile
        and incoming.title
        and _title_quality(incoming.title) >= 0
        and _source_quality(incoming_title_source) >= _source_quality(existing_title_source)
    )
    manual_title_is_authoritative = bool(
        existing.title
        and "manual_verification" in (existing_title_source or "").casefold()
        and _source_quality(existing_title_source) > _source_quality(incoming_title_source)
    )
    current_observation_repairs_navigation_title = bool(
        (incoming_is_fresher or same_run)
        and incoming_observes_existing_profile
        and existing.title
        and existing.title.strip().casefold() in {"dean"}
        and incoming.title
        and incoming.title.strip().casefold() != existing.title.strip().casefold()
        and _title_quality(incoming.title) >= 0
    )
    if manual_title_is_authoritative:
        incoming.title = existing.title
        title_source = existing_title_source
    elif current_observation_repairs_navigation_title:
        # ``Dean`` is the concrete navigation leak observed across HKU's
        # federated templates.  A current person-local directory/profile value
        # repairs that stale singleton even when its source rank is lower than
        # the historical polluted profile claim.
        title_source = incoming_title_source
    elif current_same_profile_title:
        # A current observation of the exact same authoritative person page is
        # the source of truth for a changed or repaired appointment.  Pure
        # quality scoring alone can otherwise preserve a stale navigation word
        # such as ``Dean`` over valid titles like ``Post-Doctoral Fellow`` or
        # ``Principal Professional Practitioner``.  Explicit manual
        # verification remains higher authority and cannot be overwritten.
        title_source = incoming_title_source
    else:
        incoming.title, title_source = _pick_quality_value(
            existing.title,
            incoming.title,
            existing_title_source,
            incoming_title_source,
            _title_quality,
            prefer_incoming_on_tie=incoming_is_fresher,
        )
    incoming.profile_url, profile_source = _pick_quality_value(
        (
            existing.profile_url
            if not is_unusable_profile_url(existing.profile_url)
            else None
        ),
        incoming.profile_url,
        existing_sources.get("profile_url"),
        incoming_sources.get("profile_url"),
        _profile_url_quality,
        prefer_incoming_on_tie=incoming_is_fresher,
    )
    incoming.lab_url = incoming.lab_url or existing.lab_url
    departments = _split_departments(
        [
            *(getattr(existing, "departments", None) or []),
            existing.department,
            *(getattr(incoming, "departments", None) or []),
            incoming.department,
        ]
    )
    incoming.departments = departments
    incoming.department = "; ".join(departments) or None
    profile_urls = [
        *(getattr(existing, "profile_urls", None) or []),
        existing.profile_url,
        *(getattr(incoming, "profile_urls", None) or []),
        observed_incoming_profile_url,
        incoming.profile_url,
    ]
    incoming.profile_urls = list(
        dict.fromkeys(
            value
            for value in profile_urls
            if value and not is_unusable_profile_url(value)
        )
    )
    aliases = {
        normalized
        for alias in [*(existing.aliases or []), *(incoming.aliases or [])]
        if (normalized := " ".join((alias or "").split()))
        and not _is_non_person_name(normalized)
        and not _is_title_contaminated_name(normalized)
    }
    if (
        existing.display_name
        and existing.display_name.casefold() != incoming.display_name.casefold()
        and not _is_non_person_name(existing.display_name)
        and not _is_title_contaminated_name(existing.display_name)
    ):
        aliases.add(existing.display_name)
    if (
        observed_incoming_name
        and observed_incoming_name.casefold() != incoming.display_name.casefold()
        and not _is_non_person_name(observed_incoming_name)
        and not _is_title_contaminated_name(observed_incoming_name)
    ):
        aliases.add(observed_incoming_name)
    aliases.discard(incoming.display_name)
    incoming.aliases = sorted(aliases)
    existing_email_source = existing_sources.get("emails")
    incoming_email_source = incoming_sources.get("emails")
    existing_email_rank = _source_quality(existing_email_source)
    incoming_email_rank = _source_quality(incoming_email_source)
    if incoming_email_rank > existing_email_rank:
        # An observation from a more authoritative source replaces the lower
        # authority email claim even when the profile explicitly reports no
        # person-local address.  In particular, an official profile parsed as
        # ``ambiguous_email`` or ``none`` must be able to clear a directory
        # email that was copied from a neighbouring card or stale listing.
        incoming.emails = sorted(set(incoming.emails))
        email_source = incoming_email_source
    elif existing_email_rank > incoming_email_rank:
        incoming.emails = list(existing.emails)
        incoming.email_association = existing.email_association
        email_source = existing_email_source
    elif (
        same_run
        and existing.email_association == "person_local"
        and incoming.email_association == "person_local"
    ):
        # Equal-authority, person-local observations of the same person in one
        # run are complementary.  Ambiguous/none observations are deliberately
        # excluded: unioning those can preserve a stale email that the current
        # profile observation has just invalidated.
        incoming.emails = sorted(set(existing.emails + incoming.emails))
        email_source = incoming_email_source or existing_email_source
    elif (
        same_run
        and incoming.email_association in {"ambiguous_email", "none"}
        and incoming_observes_existing_profile
    ):
        # A current observation of the exact same official profile supersedes
        # an email previously attributed to that page.  This also covers the
        # three-step case where a lower-authority directory observation first
        # refreshes last_seen_run_id while the old profile remains the email's
        # recorded field source.
        incoming.emails = sorted(set(incoming.emails))
        email_source = incoming_email_source
    elif same_run:
        # A conflicting equal-authority observation from a different page does
        # not erase an existing person-local address without a same-page
        # signal.  No mixed-association pair is unioned.
        if (
            existing.email_association == "person_local"
            and incoming.email_association in {"ambiguous_email", "none"}
        ):
            incoming.emails = list(existing.emails)
            incoming.email_association = existing.email_association
            email_source = existing_email_source
        else:
            incoming.emails = sorted(set(incoming.emails))
            email_source = incoming_email_source
    elif incoming_email_rank == existing_email_rank:
        incoming.emails = sorted(set(incoming.emails))
        email_source = incoming_email_source
    incoming.research_areas = (
        sorted(set(existing.research_areas + incoming.research_areas))
        if same_run
        else (incoming.research_areas or existing.research_areas)
    )
    incoming.source_evidence_ids = (
        sorted(set(existing.source_evidence_ids + incoming.source_evidence_ids))
        if same_run
        else incoming.source_evidence_ids
    )
    incoming.publications_summary = incoming.publications_summary or existing.publications_summary
    incoming.external_ids = _merge_external_ids(existing.external_ids or {}, incoming.external_ids or {})
    incoming.field_sources = {**existing_sources, **incoming_sources}
    if display_source:
        incoming.field_sources["display_name"] = display_source
    if title_source:
        incoming.field_sources["title"] = title_source
    if profile_source:
        incoming.field_sources["profile_url"] = profile_source
    if email_source:
        incoming.field_sources["emails"] = email_source
    incoming.membership_status = "active"
    incoming.missing_streak = 0
    incoming.schema_version = 2
    return incoming


def _canonicalize(
    person,
    institution_record,
    config: dict,
    evidence_ids: list[str],
    run_id: str,
) -> CanonicalPIRecord:
    given, family, normalized_name = split_name(person.name)
    title = normalize_title(person.title)
    department = normalize_department(person.department)
    research_areas = normalize_topics(person.research_areas)
    person_id = stable_id("pi", institution_record.institution_id, normalized_name.lower(), person.profile_url or person.source_url)
    observed_at = utc_now_iso()
    field_sources = {
        "display_name": person.source_type,
        "emails": person.source_type,
    }
    for field_name, value in {
        "title": title,
        "department": department,
        "profile_url": person.profile_url,
        "lab_url": person.lab_url,
        "research_areas": research_areas,
        "external_ids": person.external_ids,
    }.items():
        if value not in (None, "", [], {}):
            field_sources[field_name] = person.source_type
    return CanonicalPIRecord(
        person_id=person_id,
        display_name=normalized_name,
        given_name=given,
        family_name=family,
        aliases=[],
        institution_id=institution_record.institution_id,
        institution_name=institution_record.name,
        ror_id=institution_record.ror_id,
        department=department,
        title=title,
        profile_url=person.profile_url,
        lab_url=person.lab_url,
        emails=sorted(set(email.lower() for email in person.emails)),
        research_areas=research_areas,
        publications_summary={},
        external_ids=person.external_ids,
        source_evidence_ids=evidence_ids,
        last_checked_at=observed_at,
        first_seen_at=observed_at,
        last_seen_at=observed_at,
        last_seen_run_id=run_id,
        membership_status="active",
        missing_streak=0,
        pool_scope=(config.get("pool_scope") or {}).get("name"),
        schema_version=2,
        departments=[department] if department else [],
        profile_urls=[person.profile_url] if person.profile_url else [],
        field_sources=field_sources,
        email_association=person.email_association,
    )


def _store_publication_fingerprints(
    storage: PIIndexStorage,
    record: CanonicalPIRecord,
    person,
    run_id: str,
) -> None:
    observed_at = utc_now_iso()
    observed_fingerprint_ids: list[str] = []
    for fingerprint in person.publication_fingerprints:
        title = str(fingerprint.get("title") or "").strip()
        if not title:
            continue
        doi = str(fingerprint.get("doi") or "").strip().lower() or None
        publication_year = fingerprint.get("publication_year")
        identity_key = doi or f"{' '.join(title.lower().split())}|{publication_year or ''}"
        fingerprint_id = stable_id("pubfp", record.person_id, identity_key)
        storage.upsert_publication_fingerprint(
            OfficialPublicationFingerprint(
                fingerprint_id=fingerprint_id,
                person_id=record.person_id,
                institution_id=record.institution_id,
                title=title,
                citation_text=str(fingerprint.get("citation_text") or title),
                publication_year=int(publication_year) if publication_year else None,
                doi=doi,
                publication_url=fingerprint.get("publication_url"),
                source_url=person.source_url,
                confidence=float(fingerprint.get("confidence") or 0.7),
                run_id=run_id,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                last_seen_run_id=run_id,
            )
        )
        observed_fingerprint_ids.append(fingerprint_id)
    if observed_fingerprint_ids:
        # Full institution ingestion may combine directory cards, profile pages,
        # and selected-publication views.  It can assert what was observed, but
        # it is not a source-local complete snapshot and therefore must never
        # advance absence/tombstone state.
        storage.reconcile_official_publication_claims(
            person_id=record.person_id,
            institution_id=record.institution_id,
            source_url=person.source_url,
            run_id=run_id,
            observed_fingerprint_ids=observed_fingerprint_ids,
            complete=False,
        )


def _ingest_institution_once(
    config_path: str | Path,
    storage: PIIndexStorage,
    crawl_policy_path: str | Path = "configs/crawl_policy.yaml",
    snapshot_root: str | Path | None = None,
    archive_root: str | Path | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    config = load_institution_config(config_path)
    crawl_policy = load_crawl_policy(crawl_policy_path)
    institution_record = institution_from_config(config)
    institution_record.source = f"config:{config_path}"
    storage.upsert_institution(institution_record)

    run_id = _new_run_id(institution_record.institution_id)
    config_sha256 = content_hash(json.dumps(config, ensure_ascii=False, sort_keys=True))
    archive_root = Path(archive_root) if archive_root is not None else storage.db_path.parent / "raw_sources"
    archive = ContentArchive(archive_root)
    pool_scope = str((config.get("pool_scope") or {}).get("name") or "")
    storage.start_ingestion_run(
        run_id,
        institution_record.institution_id,
        institution_record.name,
        str(config_path),
        config_sha256,
        pool_scope,
    )

    def record_fetch(result, source_type: str, crawl_method: str) -> None:
        storage.insert_raw_source(
            RawSourceRecord(
                source_url=result.url,
                source_type=source_type,
                institution_id=institution_record.institution_id,
                fetched_at=result.fetched_at,
                http_status=result.status_code,
                content_hash=result.content_hash,
                parser_used=None,
                crawl_method=crawl_method,
                error_reason=result.error,
                run_id=run_id,
                final_url=result.final_url,
                content_type=result.content_type,
                encoding=result.encoding,
                etag=result.etag,
                last_modified=result.last_modified,
                archive_key=result.archive_key,
                body_sha256=result.content_hash if result.body else None,
                uncompressed_bytes=result.uncompressed_bytes,
                compressed_bytes=result.compressed_bytes,
                network_bytes=result.network_bytes,
                not_modified=result.not_modified,
            )
        )

    fetcher = Fetcher(
        user_agent=crawl_policy.get("user_agent", "pi-index-mvp/0.1"),
        timeout_seconds=int(config.get("crawl", {}).get("timeout_seconds") or crawl_policy.get("timeout_seconds") or 20),
        max_retries=int(config.get("crawl", {}).get("max_retries") or crawl_policy.get("max_retries") or 2),
        backoff_seconds=float(config.get("crawl", {}).get("backoff_seconds") or crawl_policy.get("backoff_seconds") or 1.5),
        default_delay_seconds=float(crawl_policy.get("default_crawl_delay_seconds") or 1.0),
        respect_robots=bool(config.get("crawl", {}).get("respect_robots_txt", crawl_policy.get("respect_robots_txt", True))),
        archive=archive,
        cache_lookup=lambda url: storage.get_latest_raw_source(institution_record.institution_id, url),
        on_result=record_fetch,
        offline=offline,
    )
    adapter = ConfigDrivenInstitutionAdapter(
        config,
        crawl_policy,
        fetcher,
        storage,
        institution_record.institution_id,
        run_id,
    )
    outcome = adapter.crawl_and_parse()
    parsed_results = outcome.parsed_results

    upserted_people: set[str] = set()
    observed_people_with_profile_url: set[str] = set()
    email_count = 0
    non_person_observations_filtered = 0
    unusable_profile_urls_filtered = 0
    for _fetch_result, people in parsed_results:
        for person in people:
            if _sanitize_person_profile_url(person):
                unusable_profile_urls_filtered += 1
            if _is_non_person_observation(person):
                non_person_observations_filtered += 1
                continue
            evidence = _evidence_for_person(person, institution_record.institution_id, run_id)
            evidence_ids = []
            for item in evidence:
                storage.insert_person_evidence(item)
                evidence_ids.append(item.evidence_id)
            record = _canonicalize(person, institution_record, config, evidence_ids, run_id)
            generated_person_id = record.person_id
            record.person_id = storage.resolve_person_id(record.person_id, run_id=run_id)
            existing_same_id = storage.get_pi_record(record.person_id)
            duplicate = storage.find_existing_duplicate(record)
            if existing_same_id and duplicate:
                matched_person_id, reason = duplicate
                kept_person_id = storage.preferred_canonical_person_id(
                    existing_same_id.person_id,
                    matched_person_id,
                )
                duplicate_person_id = (
                    matched_person_id
                    if kept_person_id == existing_same_id.person_id
                    else existing_same_id.person_id
                )
                kept = storage.get_pi_record(kept_person_id)
                duplicate_record = storage.get_pi_record(duplicate_person_id)
                if kept and duplicate_record:
                    duplicate_record.person_id = kept_person_id
                    kept = _merge_records(kept, duplicate_record)
                    kept.person_id = kept_person_id
                    record.person_id = kept_person_id
                    record = _merge_records(kept, record)
                    record.person_id = kept_person_id
                    storage.consolidate_person_ids(
                        duplicate_person_id,
                        kept_person_id,
                        institution_record.institution_id,
                        reason,
                        run_id,
                    )
                    storage.record_duplicate(
                        institution_record.institution_id,
                        f"{institution_record.institution_id}|identity|{kept_person_id}",
                        kept_person_id,
                        duplicate_person_id,
                        reason,
                        run_id,
                    )
                    storage.upsert_person_id_alias(
                        generated_person_id,
                        kept_person_id,
                        institution_record.institution_id,
                        reason,
                        run_id,
                    )
            elif existing_same_id:
                record = _merge_records(existing_same_id, record)
            elif duplicate:
                kept_person_id, reason = duplicate
                existing = storage.get_pi_record(kept_person_id)
                if existing and existing.last_seen_run_id == run_id:
                    storage.record_duplicate(
                        institution_record.institution_id,
                        f"{institution_record.institution_id}|{record.display_name.lower()}",
                        kept_person_id,
                        generated_person_id,
                        reason,
                        run_id,
                    )
                storage.upsert_person_id_alias(
                    generated_person_id,
                    kept_person_id,
                    institution_record.institution_id,
                    reason,
                    run_id,
                )
                record.person_id = kept_person_id
                if existing:
                    record = _merge_records(existing, record)
            storage.insert_pi_observation(record, run_id, person.source_url, person.to_dict())
            _store_publication_fingerprints(storage, record, person, run_id)
            record.publications_summary = storage.publication_summary(record.person_id)
            email_evidence = []
            allowed = config.get("institution", {}).get("allowed_email_domains") or institution_record.official_domains
            for email in record.emails:
                ev = verify_email(
                    email,
                    person.source_url,
                    person.source_type,
                    institution_record.official_domains,
                    allowed,
                    person_id=record.person_id,
                    association=person.email_association,
                )
                ev.run_id = run_id
                storage.insert_email_evidence(ev)
                email_evidence.append(ev)
                email_count += 1
            verdict = contact_verdict_for_pi(record, email_evidence)
            verdict.run_id = run_id
            record.contact_confidence = verdict.contact_confidence
            record.topic_match_confidence = verdict.topic_match_confidence
            record.current_affiliation_confidence = verdict.current_affiliation_confidence
            storage.upsert_pi_record(record)
            storage.upsert_contact_verdict(verdict)
            upserted_people.add(record.person_id)
            if person.profile_url:
                observed_people_with_profile_url.add(record.person_id)

    # A late strong-identity bridge can consolidate two IDs that were both
    # observed earlier in this run.  Normalize the run sets before reporting
    # canonical counts or coverage so removed aliases are not counted as
    # people.
    upserted_people = {
        storage.resolve_person_id(person_id, run_id=run_id)
        for person_id in upserted_people
    }
    observed_people_with_profile_url = {
        storage.resolve_person_id(person_id, run_id=run_id)
        for person_id in observed_people_with_profile_url
    }

    metrics = dict(outcome.metrics)
    metrics["non_person_observations_filtered"] = non_person_observations_filtered
    metrics["unusable_profile_urls_filtered"] = unusable_profile_urls_filtered
    metrics["canonical_pi_records"] = len(upserted_people)
    metrics["emails_extracted"] = email_count
    metrics["directory_to_record_coverage"] = (
        min(1.0, len(upserted_people) / metrics["candidate_blocks"])
        if metrics.get("candidate_blocks")
        else 1.0
    )
    observed_records = [
        record
        for person_id in sorted(upserted_people)
        if (record := storage.get_pi_record(person_id)) is not None
    ]
    observed_profile_url_coverage = (
        len(observed_people_with_profile_url.intersection(upserted_people)) / len(upserted_people)
        if upserted_people
        else 0.0
    )
    run_identity_merge_count = int(
        storage.conn.execute(
            "SELECT COUNT(DISTINCT duplicate_person_id) FROM duplicates WHERE institution_id=? AND run_id=?",
            (institution_record.institution_id, run_id),
        ).fetchone()[0]
        or 0
    )
    unresolved_duplicate_groups = storage.find_unresolved_duplicate_groups(
        institution_record.institution_id,
        upserted_people,
    )
    run_duplicate_count = sum(len(group) - 1 for group in unresolved_duplicate_groups)
    run_duplicate_rate = run_duplicate_count / len(observed_records) if observed_records else 0.0
    identity_merge_population = len(observed_records) + run_identity_merge_count
    run_identity_merge_rate = (
        run_identity_merge_count / identity_merge_population
        if identity_merge_population
        else 0.0
    )
    metrics["observed_profile_url_coverage"] = observed_profile_url_coverage
    metrics["run_identity_merge_count"] = run_identity_merge_count
    metrics["run_identity_merge_rate"] = run_identity_merge_rate
    metrics["run_duplicate_count"] = run_duplicate_count
    metrics["run_duplicate_group_count"] = len(unresolved_duplicate_groups)
    metrics["run_duplicate_rate"] = run_duplicate_rate
    quality_gate = config.get("quality_gate") or {}
    crawl_complete = bool(
        metrics.get("pages_succeeded", 0) > 0
        and metrics.get("pagination_complete")
        and metrics.get("seed_url_coverage", 0) >= float(quality_gate.get("minimum_seed_url_coverage", 1.0))
        and metrics.get("unit_coverage", 0) >= float(quality_gate.get("minimum_unit_coverage", 1.0))
        and metrics.get("profile_fetch_coverage", 0) >= float(quality_gate.get("minimum_profile_fetch_coverage", 0.9))
        and metrics.get("profile_parse_coverage", 0) >= float(quality_gate.get("minimum_profile_parse_coverage", 0.0))
        and observed_profile_url_coverage >= float(quality_gate.get("minimum_profile_url_coverage", 0.0))
        and run_duplicate_rate <= float(quality_gate.get("maximum_duplicate_rate", 0.0))
        and len(upserted_people) >= int(quality_gate.get("minimum_people", 1))
    )
    lifecycle = storage.reconcile_pi_membership(
        institution_record.institution_id,
        run_id,
        upserted_people,
        crawl_complete=crawl_complete,
        missing_runs_before_inactive=int((config.get("capture") or {}).get("missing_runs_before_inactive", 2)),
    )
    metrics["membership_reconciliation"] = lifecycle
    metrics["crawl_complete"] = crawl_complete
    run_status = "success" if crawl_complete else ("partial" if metrics.get("pages_succeeded") else "failed")
    storage.finish_ingestion_run(
        run_id,
        metrics,
        len(upserted_people),
        email_count,
        run_status,
        crawl_complete,
    )
    result: dict[str, Any] = {
        "run_id": run_id,
        "institution_id": institution_record.institution_id,
        "institution_name": institution_record.name,
        "pages_parsed": len(parsed_results),
        "canonical_pi_records": len(upserted_people),
        "emails": email_count,
        "crawl_complete": crawl_complete,
        "crawl_metrics": metrics,
        "archive_root": str(archive.root),
    }
    if snapshot_root is not None:
        snapshot = create_institution_snapshot(
            storage,
            institution_record.institution_id,
            snapshot_root,
            config=config,
            config_path=config_path,
            run_id=run_id,
            archive_root=archive.root,
        )
        result.update(
            {
                "snapshot_dir": str(snapshot.snapshot_dir),
                "snapshot_run_id": snapshot.run_id,
                "snapshot_quality_status": snapshot.manifest["quality_status"],
            }
        )
    return result


def ingest_institution(
    config_path: str | Path,
    storage: PIIndexStorage,
    crawl_policy_path: str | Path = "configs/crawl_policy.yaml",
    snapshot_root: str | Path | None = None,
    archive_root: str | Path | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    runs_before = {
        row["run_id"]
        for row in storage.conn.execute(
            "SELECT run_id FROM ingestion_runs WHERE run_id IS NOT NULL"
        ).fetchall()
    }
    try:
        return _ingest_institution_once(
            config_path,
            storage,
            crawl_policy_path,
            snapshot_root,
            archive_root,
            offline,
        )
    except Exception as exc:
        row = storage.conn.execute(
            """
            SELECT * FROM ingestion_runs
            WHERE run_id IS NOT NULL AND config_name=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(config_path),),
        ).fetchone()
        if row and row["run_id"] not in runs_before:
            run_id = row["run_id"]
            error = f"{type(exc).__name__}: {exc}"
            storage.record_crawl_error(
                row["institution_id"],
                None,
                "pipeline",
                error,
                run_id,
            )
            if row["status"] == "running":
                metrics = json.loads(row["metrics_json"] or "{}")
                metrics["crawl_complete"] = False
                metrics["fatal_error"] = error
                storage.finish_ingestion_run(
                    run_id,
                    metrics,
                    int(row["people_extracted"] or 0),
                    int(row["emails_extracted"] or 0),
                    "failed",
                    False,
                )
        raise
