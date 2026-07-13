from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import yaml

from ..adapters.institution_adapter import ConfigDrivenInstitutionAdapter
from ..config import load_institution_config
from ..crawl.fetcher import Fetcher
from ..models import (
    CanonicalPIRecord,
    PersonEvidence,
    content_hash,
    stable_id,
    utc_now_iso,
)
from ..normalize.department import normalize_department
from ..normalize.institution import institution_from_config
from ..normalize.person_name import split_name
from ..normalize.title import normalize_title
from ..normalize.topic import normalize_topics
from ..storage import PIIndexStorage
from ..verify.confidence import contact_verdict_for_pi
from ..verify.email import verify_email
from ..verify.supervisor_signal import supervisor_signals
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


def _evidence_for_person(person, institution_id: str) -> list[PersonEvidence]:
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
                    evidence_id=stable_id("ev", person.person_temp_id, field_name, field_value, person.source_url),
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
                )
            )
    return evidence


def _merge_records(existing: CanonicalPIRecord, incoming: CanonicalPIRecord) -> CanonicalPIRecord:
    incoming.display_name = existing.display_name or incoming.display_name
    incoming.given_name = existing.given_name or incoming.given_name
    incoming.family_name = existing.family_name or incoming.family_name
    incoming.title = existing.title or incoming.title
    incoming.department = existing.department or incoming.department
    incoming.profile_url = existing.profile_url or incoming.profile_url
    incoming.lab_url = existing.lab_url or incoming.lab_url
    incoming.aliases = sorted(set(existing.aliases + incoming.aliases))
    incoming.emails = sorted(set(existing.emails + incoming.emails))
    incoming.research_areas = sorted(set(existing.research_areas + incoming.research_areas))
    incoming.supervision_signals = sorted(set(existing.supervision_signals + incoming.supervision_signals))
    incoming.source_evidence_ids = sorted(set(existing.source_evidence_ids + incoming.source_evidence_ids))
    incoming.publications_summary = existing.publications_summary or incoming.publications_summary
    incoming.external_ids = {**(incoming.external_ids or {}), **(existing.external_ids or {})}
    return incoming


def _canonicalize(person, institution_record, config: dict, evidence_ids: list[str]) -> CanonicalPIRecord:
    given, family, normalized_name = split_name(person.name)
    raw_title = person.title
    title = normalize_title(raw_title)
    department = normalize_department(person.department)
    research_areas = normalize_topics(person.research_areas)
    signals = supervisor_signals(title, research_areas, config.get("pi_detection", {}).get("positive_title_patterns") or [])
    person_id = stable_id("pi", institution_record.institution_id, normalized_name.lower(), person.profile_url or person.source_url)
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
        supervision_signals=signals,
        source_evidence_ids=evidence_ids,
        last_checked_at=utc_now_iso(),
    )


def ingest_institution(
    config_path: str | Path,
    storage: PIIndexStorage,
    crawl_policy_path: str | Path = "configs/crawl_policy.yaml",
    snapshot_root: str | Path | None = None,
) -> dict[str, Any]:
    config = load_institution_config(config_path)
    crawl_policy = load_crawl_policy(crawl_policy_path)
    institution_record = institution_from_config(config)
    institution_record.source = f"config:{config_path}"
    storage.upsert_institution(institution_record)

    fetcher = Fetcher(
        user_agent=crawl_policy.get("user_agent", "pi-index-mvp/0.1"),
        timeout_seconds=int(config.get("crawl", {}).get("timeout_seconds") or crawl_policy.get("timeout_seconds") or 20),
        max_retries=int(config.get("crawl", {}).get("max_retries") or crawl_policy.get("max_retries") or 2),
        backoff_seconds=float(config.get("crawl", {}).get("backoff_seconds") or crawl_policy.get("backoff_seconds") or 1.5),
        default_delay_seconds=float(crawl_policy.get("default_crawl_delay_seconds") or 1.0),
        respect_robots=bool(config.get("crawl", {}).get("respect_robots_txt", crawl_policy.get("respect_robots_txt", True))),
    )
    adapter = ConfigDrivenInstitutionAdapter(config, crawl_policy, fetcher, storage, institution_record.institution_id)
    parsed_results = adapter.crawl_and_parse()

    upserted_people: set[str] = set()
    email_count = 0
    for _fetch_result, people in parsed_results:
        for person in people:
            if not person.name:
                continue
            evidence = _evidence_for_person(person, institution_record.institution_id)
            evidence_ids = []
            for item in evidence:
                storage.insert_person_evidence(item)
                evidence_ids.append(item.evidence_id)
            record = _canonicalize(person, institution_record, config, evidence_ids)
            existing_same_id = storage.get_pi_record(record.person_id)
            duplicate = None if existing_same_id else storage.find_existing_duplicate(record)
            if existing_same_id:
                record = _merge_records(existing_same_id, record)
            elif duplicate:
                kept_person_id, reason = duplicate
                generated_person_id = record.person_id
                storage.record_duplicate(
                    institution_record.institution_id,
                    f"{institution_record.institution_id}|{record.display_name.lower()}",
                    kept_person_id,
                    generated_person_id,
                    reason,
                )
                existing = storage.get_pi_record(kept_person_id)
                record.person_id = kept_person_id
                if existing:
                    record = _merge_records(existing, record)
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
                storage.insert_email_evidence(ev)
                email_evidence.append(ev)
                email_count += 1
            verdict = contact_verdict_for_pi(
                record,
                email_evidence,
                config.get("pi_detection", {}).get("negative_title_patterns") or [],
            )
            record.contact_confidence = verdict.contact_confidence
            record.pi_supervisor_confidence = verdict.pi_supervisor_confidence
            record.topic_match_confidence = verdict.topic_match_confidence
            record.likely_supervisor_candidate = verdict.likely_supervisor_candidate
            record.current_affiliation_confidence = verdict.current_affiliation_confidence
            storage.upsert_pi_record(record)
            storage.upsert_contact_verdict(verdict)
            upserted_people.add(record.person_id)

    page_counts = storage.conn.execute(
        """
        SELECT
            COUNT(*) AS attempted,
            SUM(CASE WHEN http_status BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS succeeded,
            SUM(CASE WHEN http_status IS NULL OR http_status < 200 OR http_status >= 300 THEN 1 ELSE 0 END) AS failed
        FROM raw_sources
        WHERE institution_id=?
        """,
        (institution_record.institution_id,),
    ).fetchone()
    pages_attempted = int(page_counts["attempted"] or 0)
    pages_successfully_fetched = int(page_counts["succeeded"] or 0)
    pages_failed = int(page_counts["failed"] or 0)
    storage.record_ingestion_run(
        institution_record.institution_id,
        institution_record.name,
        str(config_path),
        pages_attempted,
        pages_successfully_fetched,
        pages_failed,
        len(upserted_people),
        email_count,
        "success" if pages_successfully_fetched else "failed",
    )
    result: dict[str, Any] = {
        "institution_id": institution_record.institution_id,
        "institution_name": institution_record.name,
        "pages_parsed": len(parsed_results),
        "canonical_pi_records": len(upserted_people),
        "emails": email_count,
    }
    if snapshot_root is not None:
        snapshot = create_institution_snapshot(
            storage,
            institution_record.institution_id,
            snapshot_root,
            config=config,
            config_path=config_path,
        )
        result.update(
            {
                "snapshot_dir": str(snapshot.snapshot_dir),
                "snapshot_run_id": snapshot.run_id,
                "snapshot_quality_status": snapshot.manifest["quality_status"],
            }
        )
    return result
