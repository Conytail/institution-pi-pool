from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

from ..adapters.institution_adapter import (
    PROFILE_PUBLICATION_PARSER_VERSION,
    ProfilePublicationParse,
    parse_profile_publications,
)
from ..config import load_institution_config
from ..crawl.archive import ContentArchive
from ..crawl.fetcher import FetchResult, Fetcher
from ..models import (
    CanonicalPIRecord,
    OfficialPublicationFingerprint,
    RawSourceRecord,
    content_hash,
    stable_id,
    utc_now_iso,
)
from ..normalize.institution import institution_from_config
from ..parsers.publications import is_meaningful_publication_fingerprint, normalize_doi
from ..storage import PIIndexStorage, is_unusable_profile_url
from .ingest_institution import load_crawl_policy


ParseProfile = Callable[[str, str, dict, list[str]], ProfilePublicationParse]


def _canonical_source_url(value: str) -> str:
    parsed = urlparse(value)
    without_fragment = urlunparse(parsed._replace(fragment=""))
    # Percent-escape hex case does not identify a different URL.  Normalizing
    # it prevents one legacy lower-case archive URL plus one canonical
    # upper-case profile URL from being refreshed twice.
    return re.sub(
        r"%[0-9a-fA-F]{2}",
        lambda match: match.group(0).upper(),
        without_fragment,
    )


def _normal_title(value: str | None) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value or "", flags=re.UNICODE).casefold().split())


def _config_sha256(config: dict[str, Any]) -> str:
    return content_hash(json.dumps(config, ensure_ascii=False, sort_keys=True))


def _parser_signature(config: dict[str, Any]) -> str:
    parsing = config.get("parsing") or {}
    payload = {
        "version": PROFILE_PUBLICATION_PARSER_VERSION,
        "adapters": parsing.get("profile_adapters") or parsing.get("preferred_adapters") or [],
        "extract_publication_fingerprints": parsing.get("extract_publication_fingerprints"),
    }
    return content_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _state_value(state: Any, key: str, default: Any = None) -> Any:
    if state is None:
        return default
    if isinstance(state, dict):
        return state.get(key, default)
    try:
        return state[key]
    except (KeyError, IndexError, TypeError):
        return getattr(state, key, default)


def _profile_sources(storage: PIIndexStorage, record: CanonicalPIRecord) -> list[str]:
    candidates = [record.profile_url, *(record.profile_urls or [])]
    # Legacy ingests may have discovered a dedicated publications/profile URL
    # without copying it to the canonical profile_urls field.  Preserve it as a
    # source, but do not assume that the legacy snapshot was complete.
    candidates.extend(
        str(row[0])
        for row in storage.conn.execute(
            """
            SELECT DISTINCT source_url
            FROM official_publication_fingerprints
            WHERE person_id=? AND source_url IS NOT NULL AND source_url!=''
            """,
            (record.person_id,),
        ).fetchall()
    )
    output: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        if not value or is_unusable_profile_url(value):
            continue
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        canonical = _canonical_source_url(value)
        if canonical in seen:
            continue
        seen.add(canonical)
        output.append(canonical)
    return output


def _existing_source_claims(
    storage: PIIndexStorage,
    person_id: str,
    source_url: str,
) -> list[dict[str, Any]]:
    table = storage.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='official_publication_source_claims'"
    ).fetchone()
    if table:
        rows = storage.conn.execute(
            """
            SELECT c.fingerprint_id, c.claim_status AS state, f.title, f.citation_text,
                   f.publication_year, f.doi, f.publication_url
            FROM official_publication_source_claims AS c
            LEFT JOIN official_publication_fingerprints AS f
              ON f.fingerprint_id=c.fingerprint_id
            WHERE c.person_id=? AND c.source_url=?
            """,
            (person_id, source_url),
        ).fetchall()
        return [dict(row) for row in rows]
    rows = storage.conn.execute(
        """
        SELECT fingerprint_id, 'current' AS state, title, citation_text,
               publication_year, doi, publication_url
        FROM official_publication_fingerprints
        WHERE person_id=? AND source_url=?
        """,
        (person_id, source_url),
    ).fetchall()
    return [dict(row) for row in rows]


def _reuse_fingerprint_id(
    person_id: str,
    fingerprint: dict[str, Any],
    existing: list[dict[str, Any]],
) -> str:
    doi = normalize_doi(fingerprint.get("doi"))
    title_key = _normal_title(str(fingerprint.get("title") or ""))
    year = fingerprint.get("publication_year")
    if doi:
        doi_matches = [
            row for row in existing if normalize_doi(str(row.get("doi") or "")) == doi
        ]
        if len(doi_matches) == 1:
            return str(doi_matches[0]["fingerprint_id"])
    # A title-only official clue often gains a DOI later.  Reusing its original
    # identity avoids a false removal plus addition and preserves first_seen_at.
    title_matches = []
    for row in existing:
        if _normal_title(str(row.get("title") or "")) != title_key:
            continue
        old_year = row.get("publication_year")
        if old_year is None or year is None or int(old_year) == int(year):
            title_matches.append(row)
    if len(title_matches) == 1:
        return str(title_matches[0]["fingerprint_id"])
    identity_key = doi or f"{title_key}|{year or ''}"
    return stable_id("pubfp", person_id, identity_key)


def _materialize_fingerprints(
    storage: PIIndexStorage,
    record: CanonicalPIRecord,
    source_url: str,
    run_id: str,
    fingerprints: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    *,
    dry_run: bool,
    commit: bool = True,
) -> tuple[list[str], set[str]]:
    observed_at = utc_now_iso()
    ids: list[str] = []
    updated: set[str] = set()
    old_by_id = {str(row["fingerprint_id"]): row for row in existing}
    for fingerprint in fingerprints:
        if not is_meaningful_publication_fingerprint(fingerprint):
            continue
        title = " ".join(str(fingerprint.get("title") or "").split())
        if not title:
            continue
        fingerprint_id = _reuse_fingerprint_id(record.person_id, fingerprint, existing)
        if fingerprint_id in ids:
            continue
        ids.append(fingerprint_id)
        publication_year = fingerprint.get("publication_year")
        publication = OfficialPublicationFingerprint(
            fingerprint_id=fingerprint_id,
            person_id=record.person_id,
            institution_id=record.institution_id,
            title=title,
            citation_text=str(fingerprint.get("citation_text") or title),
            publication_year=int(publication_year) if publication_year else None,
            doi=normalize_doi(str(fingerprint.get("doi") or "")),
            publication_url=fingerprint.get("publication_url"),
            source_url=source_url,
            confidence=float(fingerprint.get("confidence") or 0.7),
            run_id=run_id,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
            last_seen_run_id=run_id,
        )
        old = old_by_id.get(fingerprint_id)
        if old and any(
            (old.get(key) or None) != (value or None)
            for key, value in {
                "title": publication.title,
                "citation_text": publication.citation_text,
                "publication_year": publication.publication_year,
                "doi": publication.doi,
                "publication_url": publication.publication_url,
            }.items()
        ):
            updated.add(fingerprint_id)
        if not dry_run:
            storage.upsert_publication_fingerprint(publication, commit=commit)
    return ids, updated


def _raw_record(
    result: FetchResult,
    institution_id: str,
    run_id: str,
    parser_signature: str,
) -> RawSourceRecord:
    return RawSourceRecord(
        source_url=result.url,
        source_type="official_profile",
        institution_id=institution_id,
        fetched_at=result.fetched_at,
        http_status=result.status_code,
        content_hash=result.content_hash,
        parser_used=parser_signature,
        crawl_method="publication_refresh",
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


def refresh_official_publications(
    config_path: str | Path,
    storage: PIIndexStorage,
    *,
    archive_root: str | Path | None = None,
    crawl_policy_path: str | Path = "configs/crawl_policy.yaml",
    person_ids: list[str] | None = None,
    department_patterns: list[str] | None = None,
    limit: int | None = None,
    due_only: bool = False,
    dry_run: bool = False,
    offline: bool = False,
    missing_confirmations: int = 2,
    allow_single_confirmation_removal: bool = False,
    workers: int = 4,
    parse_profile: ParseProfile = parse_profile_publications,
) -> dict[str, Any]:
    """Incrementally refresh official publication claims for one institution.

    This deliberately does not call full institution ingestion and never
    reconciles PI membership, contact evidence, or identity records.
    """

    config = load_institution_config(config_path)
    policy = load_crawl_policy(crawl_policy_path)
    institution = institution_from_config(config)
    if int(missing_confirmations) < 1:
        raise ValueError("missing_confirmations must be at least 1")
    if int(missing_confirmations) < 2 and not (
        offline and allow_single_confirmation_removal
    ):
        raise ValueError(
            "missing_confirmations=1 requires offline=True and "
            "allow_single_confirmation_removal=True"
        )
    if limit is not None and int(limit) < 1:
        raise ValueError("limit must be at least 1 when supplied")
    if person_ids is None and department_patterns is None:
        raise ValueError(
            "Refusing an unscoped refresh: supply department_patterns or person_ids"
        )

    def normalize_selector(name: str, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = [str(value).strip() for value in values if str(value).strip()]
        if not normalized or len(normalized) != len(values):
            raise ValueError(f"{name} must contain at least one non-blank value")
        return list(dict.fromkeys(normalized))

    normalized_person_ids = normalize_selector("person_ids", person_ids)
    normalized_departments = normalize_selector("department_patterns", department_patterns)
    config_sha = _config_sha256(config)
    parser_signature = _parser_signature(config)
    run_id = f"pubrefresh_{institution.institution_id}_{uuid4().hex[:12]}"
    started_at = utc_now_iso()
    metrics: dict[str, Any] = {
        "run_id": run_id,
        "institution_id": institution.institution_id,
        "dry_run": dry_run,
        "offline": bool(offline),
        "missing_confirmations": int(missing_confirmations),
        "single_confirmation_removal_authorized": bool(
            int(missing_confirmations) == 1
            and offline
            and allow_single_confirmation_removal
        ),
        "workers": max(1, int(workers)),
        "sources_considered": 0,
        "sources_checked": 0,
        "sources_usable": 0,
        "sources_authoritative": 0,
        "sources_not_due": 0,
        "sources_retry_backoff": 0,
        "not_modified": 0,
        "same_hash": 0,
        "reparsed": 0,
        "baseline_established": 0,
        "added": 0,
        "updated": 0,
        "pending_missing": 0,
        "no_longer_observed": 0,
        "tombstoned": 0,
        "reactivated": 0,
        "quarantined": 0,
        "failed": 0,
        "network_bytes": 0,
        "people_affected": [],
    }
    selected_ids = set(normalized_person_ids or [])
    selected_departments = [
        value.casefold().strip()
        for value in (normalized_departments or [])
    ]

    def department_selected(record: CanonicalPIRecord) -> bool:
        if normalized_departments is None:
            return True
        haystack = "; ".join(
            value
            for value in [record.department, *(record.departments or [])]
            if value
        ).casefold()
        return any(pattern in haystack for pattern in selected_departments)

    records = [
        record
        for record in storage.iter_pi_records()
        if record.institution_id == institution.institution_id
        and (normalized_person_ids is None or record.person_id in selected_ids)
        and department_selected(record)
    ]
    matched_ids = {record.person_id for record in records}
    missing_ids = selected_ids - matched_ids
    if missing_ids:
        raise ValueError(
            "Requested person_ids were not all matched in the selected institution/scope: "
            + ", ".join(sorted(missing_ids))
        )
    if not records:
        raise ValueError("The supplied selector matched zero active PI records")

    source_records: list[tuple[CanonicalPIRecord, str]] = []
    for record in records:
        source_records.extend((record, url) for url in _profile_sources(storage, record))
    if limit is not None:
        source_records = source_records[: int(limit)]
    if not source_records:
        raise ValueError("The supplied selector matched no usable official profile sources")
    if normalized_person_ids is not None:
        sourced_ids = {record.person_id for record, _ in source_records}
        unsourced_ids = selected_ids - sourced_ids
        if unsourced_ids:
            raise ValueError(
                "Requested person_ids were not all represented by usable profile sources: "
                + ", ".join(sorted(unsourced_ids))
            )
    metrics["sources_considered"] = len(source_records)

    refresh_config = config.get("refresh") or {}
    profile_interval_days = int(refresh_config.get("profile_interval_days") or 30)
    failure_retry_hours = int(refresh_config.get("failure_retry_hours") or 24)
    now = datetime.now(timezone.utc)
    cached_sources: dict[str, RawSourceRecord] = {}
    pending_sources: list[tuple[CanonicalPIRecord, str, Any]] = []
    for record, source_url in source_records:
        state = storage.get_publication_refresh_state(record.person_id, source_url)
        # A failed/quarantined check is not a successful refresh watermark.  In
        # particular, pilot/legacy states may have ``checked_at`` populated but
        # deliberately have no valid publication baseline yet.  Using
        # ``last_success_at`` lets ``--due-only`` resume those sources instead
        # of suppressing them for a full refresh interval.
        last_success_at = _parse_iso(_state_value(state, "last_success_at"))
        checked_at = _parse_iso(_state_value(state, "checked_at"))
        parse_key_changed = bool(
            state
            and (
                (_state_value(state, "parser_name") or "") != parser_signature
                or (_state_value(state, "parser_version") or "")
                != PROFILE_PUBLICATION_PARSER_VERSION
                or (_state_value(state, "config_hash") or "") != config_sha
            )
        )
        if (
            due_only
            and not parse_key_changed
            and last_success_at
            and last_success_at + timedelta(days=profile_interval_days) > now
        ):
            metrics["sources_not_due"] += 1
            continue
        if (
            due_only
            and not parse_key_changed
            and not last_success_at
            and checked_at
            and checked_at + timedelta(hours=max(1, failure_retry_hours)) > now
        ):
            # Systematic parser/identity quarantines must not be fetched again
            # on every scheduler invocation.  They retry on a short backoff,
            # while a parser/config change bypasses the backoff immediately.
            metrics["sources_not_due"] += 1
            metrics["sources_retry_backoff"] += 1
            continue
        pending_sources.append((record, source_url, state))
        cached = storage.get_latest_raw_source(record.institution_id, source_url)
        if cached is not None:
            cached_sources[source_url] = cached

    if not dry_run:
        storage.start_publication_refresh_run(
            run_id,
            institution.institution_id,
            source_kind="official_profile",
            dry_run=False,
            started_at=started_at,
            metrics=metrics,
        )

    archive_path = Path(archive_root) if archive_root else storage.db_path.parent / "raw_sources"
    # Preview mode can replay existing cache blobs but never creates or updates
    # archive content.
    archive = ContentArchive(archive_path, read_only=dry_run)
    crawl = config.get("crawl") or {}
    fetcher = Fetcher(
        user_agent=policy.get("user_agent", "pi-index-mvp/0.1"),
        timeout_seconds=int(crawl.get("timeout_seconds") or policy.get("timeout_seconds") or 20),
        max_retries=int(crawl.get("max_retries") or policy.get("max_retries") or 2),
        backoff_seconds=float(crawl.get("backoff_seconds") or policy.get("backoff_seconds") or 1.5),
        default_delay_seconds=float(policy.get("default_crawl_delay_seconds") or 1.0),
        respect_robots=bool(crawl.get("respect_robots_txt", policy.get("respect_robots_txt", True))),
        archive=archive,
        # SQLite connections stay on the coordinator thread.  Workers receive a
        # read-only metadata snapshot while archive blobs remain concurrency-safe.
        cache_lookup=lambda url: cached_sources.get(url),
        offline=offline,
    )
    if crawl.get("crawl_delay_seconds") is not None:
        for seed in crawl.get("seed_urls") or []:
            fetcher.set_domain_delay(seed, crawl.get("crawl_delay_seconds"))

    worker_count = max(1, min(int(workers), len(pending_sources) or 1))

    def fetch_one(item: tuple[CanonicalPIRecord, str, Any]):
        record, source_url, state = item
        try:
            result = fetcher.fetch(
                source_url,
                source_type="official_profile",
                crawl_method="publication_refresh",
            )
        except Exception as exc:  # archive/transport guard outside Fetcher retries
            result = FetchResult(
                url=source_url,
                final_url=source_url,
                status_code=None,
                text="",
                content_type=None,
                content_hash=content_hash(b""),
                error=f"{type(exc).__name__}: {exc}",
                fetched_at=utc_now_iso(),
            )
        return record, source_url, state, result

    def iter_fetches():
        if worker_count == 1:
            for item in pending_sources:
                yield fetch_one(item)
            return
        iterator = iter(pending_sources)
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="publication-refresh",
        ) as executor:
            futures = {}
            for _ in range(worker_count):
                try:
                    item = next(iterator)
                except StopIteration:
                    break
                futures[executor.submit(fetch_one, item)] = item
            while futures:
                completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    futures.pop(future)
                    yield future.result()
                    try:
                        item = next(iterator)
                    except StopIteration:
                        continue
                    futures[executor.submit(fetch_one, item)] = item

    affected_people: set[str] = set()
    status = "success"
    error_reason: str | None = None
    try:
        for record, source_url, state, result in iter_fetches():
            # Every domain mutation for this person/source is held until the
            # source reaches a coherent terminal state.  A later exception
            # rolls back this source without undoing earlier completed ones.
            metrics["sources_checked"] += 1
            metrics["network_bytes"] += int(result.network_bytes or 0)
            if not dry_run:
                storage.insert_raw_source(
                    _raw_record(result, record.institution_id, run_id, parser_signature),
                    commit=False,
                )

            common_state = {
                "person_id": record.person_id,
                "institution_id": record.institution_id,
                "source_url": source_url,
                "final_url": result.final_url or source_url,
                "etag": result.etag,
                "last_modified": result.last_modified,
                "body_sha256": result.content_hash,
                "parser_name": parser_signature,
                "parser_version": PROFILE_PUBLICATION_PARSER_VERSION,
                "config_hash": config_sha,
                "checked_at": result.fetched_at or utc_now_iso(),
                "last_run_id": run_id,
            }
            if result.error:
                metrics["failed"] += 1
                if not dry_run:
                    storage.upsert_publication_refresh_state(
                        **common_state,
                        parse_status="fetch_error",
                        parse_complete=bool(_state_value(state, "parse_complete", False)),
                        publication_count=int(_state_value(state, "publication_count", 0) or 0),
                        error_reason=result.error,
                        commit=False,
                    )
                    storage.conn.commit()
                continue

            decision = storage.publication_refresh_decision(
                record.person_id,
                source_url,
                body_sha256=result.content_hash,
                parser_name=parser_signature,
                parser_version=PROFILE_PUBLICATION_PARSER_VERSION,
                config_hash=config_sha,
            )
            unchanged_kind = None
            if not decision["should_parse"] and result.not_modified:
                unchanged_kind = "not_modified"
            elif not decision["should_parse"]:
                unchanged_kind = "same_hash"
            if unchanged_kind:
                metrics[unchanged_kind] += 1
                metrics["sources_usable"] += 1
                if not dry_run:
                    storage.upsert_publication_refresh_state(
                        **common_state,
                        parse_status=unchanged_kind,
                        parse_complete=True,
                        publication_count=int(_state_value(state, "publication_count", 0) or 0),
                        error_reason=None,
                        commit=False,
                    )
                    storage.conn.commit()
                continue

            parsed = parse_profile(
                result.text,
                result.final_url or source_url,
                config,
                [record.display_name, *(record.aliases or [])],
            )
            metrics["reparsed"] += 1
            if parsed.person_match_count != 1 or parsed.parsed_person_count != 1:
                metrics["quarantined"] += 1
                if not dry_run:
                    storage.upsert_publication_refresh_state(
                        **common_state,
                        parse_status="quarantined",
                        parse_complete=bool(_state_value(state, "parse_complete", False)),
                        publication_count=int(_state_value(state, "publication_count", 0) or 0),
                        error_reason=parsed.reason or "profile_identity_ambiguous",
                        record={"last_parsed_at": utc_now_iso()},
                        commit=False,
                    )
                    storage.conn.commit()
                continue
            if parsed.parser_errors:
                metrics["quarantined"] += 1
                if not dry_run:
                    storage.upsert_publication_refresh_state(
                        **common_state,
                        parse_status="quarantined",
                        parse_complete=bool(_state_value(state, "parse_complete", False)),
                        publication_count=int(_state_value(state, "publication_count", 0) or 0),
                        error_reason="; ".join(parsed.parser_errors),
                        record={"last_parsed_at": utc_now_iso()},
                        commit=False,
                    )
                    storage.conn.commit()
                continue

            existing = _existing_source_claims(storage, record.person_id, source_url)
            observed_ids, updated_ids = _materialize_fingerprints(
                storage,
                record,
                source_url,
                run_id,
                parsed.fingerprints,
                existing,
                dry_run=dry_run,
                commit=False,
            )
            current_count = sum(
                row.get("state") in {"active", "no_longer_observed"}
                for row in existing
            )
            completeness_reason = parsed.reason
            complete = parsed.authoritative and bool(_state_value(state, "parse_complete", False))
            if parsed.authoritative and not bool(_state_value(state, "parse_complete", False)):
                metrics["baseline_established"] += 1
            if complete and current_count and not observed_ids:
                complete = False
                completeness_reason = "catastrophic_empty_inventory"
            elif complete and current_count >= 5 and len(observed_ids) * 5 < current_count:
                complete = False
                completeness_reason = "suspicious_inventory_drop"
            if completeness_reason in {
                "catastrophic_empty_inventory",
                "suspicious_inventory_drop",
            }:
                metrics["quarantined"] += 1
            source_usable = completeness_reason not in {
                "catastrophic_empty_inventory",
                "suspicious_inventory_drop",
            }
            source_authoritative = parsed.authoritative and source_usable

            diff = storage.reconcile_official_publication_claims(
                person_id=record.person_id,
                institution_id=record.institution_id,
                source_url=source_url,
                run_id=run_id,
                observed_fingerprint_ids=observed_ids,
                complete=complete,
                missing_runs_before_tombstone=missing_confirmations,
                dry_run=dry_run,
                observed_at=result.fetched_at or utc_now_iso(),
                commit=False,
            )
            counts = diff.get("counts") or {}
            metrics["added"] += int(counts.get("added_claims", 0))
            metrics["updated"] += len(updated_ids)
            metrics["pending_missing"] += int(counts.get("no_longer_observed", 0))
            metrics["no_longer_observed"] += int(counts.get("tombstoned", 0))
            metrics["tombstoned"] += int(counts.get("tombstoned", 0))
            metrics["reactivated"] += int(counts.get("reactivated_claims", 0)) + int(
                counts.get("recovered_claims", 0)
            )
            changed = bool(updated_ids) or any(
                int(counts.get(key, 0))
                for key in (
                    "added_claims",
                    "recovered_claims",
                    "reactivated_claims",
                    "no_longer_observed",
                    "tombstoned",
                )
            )
            if changed:
                affected_people.add(record.person_id)
                if updated_ids and not dry_run:
                    storage._enqueue_vector_dirty_no_commit(
                        "openalex_works_sync",
                        record.person_id,
                        "official_publication_metadata_changed",
                        run_id=run_id,
                        person_id=record.person_id,
                    )
            if not dry_run:
                storage.upsert_publication_refresh_state(
                    **common_state,
                    parse_status=(
                        "success"
                        if source_authoritative
                        else "additions_only"
                        if source_usable
                        else "quarantined"
                    ),
                    parse_complete=source_authoritative,
                    publication_count=len(observed_ids),
                    error_reason=completeness_reason if not complete else None,
                    last_success_at=utc_now_iso() if source_usable else None,
                    record={
                        "last_parsed_at": utc_now_iso(),
                        "inventory_authoritative": parsed.authoritative,
                        "inventory_truncated": parsed.truncated,
                        "parser_names": list(parsed.parser_names),
                    },
                    commit=False,
                )
                storage.conn.commit()
            if source_usable:
                metrics["sources_usable"] += 1
            if source_authoritative:
                metrics["sources_authoritative"] += 1
        checked = int(metrics["sources_checked"])
        usable = int(metrics["sources_usable"])
        if checked == 0:
            status = "success"
        elif usable == checked and not metrics["failed"] and not metrics["quarantined"]:
            status = "success"
        elif usable > 0:
            status = "partial"
        else:
            status = "failed"
        metrics["status"] = status
        if status != "success":
            error_reason = (
                f"usable={usable}/{checked}; failed={metrics['failed']}; "
                f"quarantined={metrics['quarantined']}"
            )
    except Exception as exc:
        storage.conn.rollback()
        status = "failed"
        metrics["status"] = status
        error_reason = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        metrics["people_affected"] = sorted(affected_people)
        if not dry_run:
            storage.finish_publication_refresh_run(
                run_id,
                status=status,
                metrics=metrics,
                error_reason=error_reason,
                finished_at=utc_now_iso(),
            )
    return metrics
