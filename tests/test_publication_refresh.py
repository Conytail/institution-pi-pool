from __future__ import annotations

from collections import deque
from threading import Lock
import time

import pytest

from pi_index.adapters.institution_adapter import ProfilePublicationParse
from pi_index.config import load_institution_config
from pi_index.crawl.archive import ContentArchive
from pi_index.crawl.fetcher import FetchResult
from pi_index.models import CanonicalPIRecord, RawSourceRecord
from pi_index.normalize.institution import institution_from_config
from pi_index.pipeline import refresh_official_publications as module
from pi_index.storage import PIIndexStorage


CONFIG = "configs/institutions/hku.yaml"
PROFILE_URL = "https://www.hku.hk/people/jane-doe"


def test_publication_source_url_normalizes_percent_escape_hex_case():
    assert module._canonical_source_url(
        "https://example.edu/people/uta-sch%d3%a7nberg/#publications"
    ) == module._canonical_source_url(
        "https://example.edu/people/uta-sch%D3%A7nberg/"
    )


def _seed_pi(storage: PIIndexStorage) -> CanonicalPIRecord:
    institution = institution_from_config(load_institution_config(CONFIG))
    record = CanonicalPIRecord(
        person_id="pi_jane",
        display_name="Jane Doe",
        given_name="Jane",
        family_name="Doe",
        aliases=[],
        institution_id=institution.institution_id,
        institution_name=institution.name,
        ror_id=institution.ror_id,
        department="Example Department",
        title="Lecturer",
        profile_url=PROFILE_URL,
        lab_url=None,
        emails=["jane@hku.hk"],
        research_areas=[],
        publications_summary={},
        external_ids={},
        source_evidence_ids=[],
        last_checked_at="2026-07-01T00:00:00+00:00",
        profile_urls=[PROFILE_URL],
    )
    storage.upsert_pi_record(record)
    return record


def _result(body_hash: str, sequence: int, *, not_modified: bool = False) -> FetchResult:
    html = f"<html><h1>Jane Doe</h1><h2>Publications</h2>{body_hash}</html>"
    return FetchResult(
        url=PROFILE_URL,
        final_url=PROFILE_URL,
        status_code=304 if not_modified else 200,
        text=html,
        content_type="text/html; charset=utf-8",
        content_hash=body_hash,
        body=html.encode(),
        fetched_at=f"2026-07-{sequence:02d}T00:00:00+00:00",
        etag=f'"{body_hash}"',
        not_modified=not_modified,
    )


def _install_fetcher(monkeypatch, results):
    queue = deque(results)

    class FakeFetcher:
        def __init__(self, **_kwargs):
            pass

        def set_domain_delay(self, *_args):
            pass

        def fetch(self, *_args, **_kwargs):
            if not queue:
                raise AssertionError("unexpected fetch")
            return queue.popleft()

    monkeypatch.setattr(module, "Fetcher", FakeFetcher)
    return queue


def _fingerprint(title: str, year: int, doi: str | None = None):
    return {
        "title": title,
        "citation_text": f"{title}, {year}",
        "publication_year": year,
        "doi": doi,
        "publication_url": f"https://doi.org/{doi}" if doi else None,
        "confidence": 0.9,
    }


def _parsed(*fingerprints, authoritative=True):
    return ProfilePublicationParse(
        fingerprints=list(fingerprints),
        parser_names=("test",),
        parser_errors=(),
        person_match_count=1,
        parsed_person_count=1,
        authoritative=authoritative,
        truncated=False,
        reason=None if authoritative else "publication_inventory_not_authoritative",
    )


def test_refresh_establishes_baseline_and_304_skips_parser(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    _seed_pi(storage)
    _install_fetcher(
        monkeypatch,
        [_result("hash-a", 1), _result("hash-a", 2, not_modified=True)],
    )
    publications = [
        _fingerprint("Reliable Scientific Workflows", 2024),
        _fingerprint("Causal Representation Learning", 2025),
    ]

    first = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
        parse_profile=lambda *_args: _parsed(*publications),
    )

    def exploding_parser(*_args):
        raise AssertionError("304 baseline was reparsed")

    second = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
        parse_profile=exploding_parser,
    )

    assert first["baseline_established"] == 1
    assert first["added"] == 2
    assert second["not_modified"] == 1
    assert second["reparsed"] == 0
    assert len(storage.get_official_publication_source_claims(person_id="pi_jane")) == 2
    assert len(storage.iter_vector_dirty_queue()) == 1
    storage.close()


def test_changed_complete_snapshots_need_two_confirmations_to_tombstone(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    _seed_pi(storage)
    a = _fingerprint("Reliable Scientific Workflows", 2024)
    b = _fingerprint("Causal Representation Learning", 2025)
    c = _fingerprint("Graph Models for Biology", 2026)
    _install_fetcher(
        monkeypatch,
        [_result("h1", 1), _result("h2", 2), _result("h3", 3)],
    )
    snapshots = deque([_parsed(a, b), _parsed(b, c), _parsed(b, c)])
    parser = lambda *_args: snapshots.popleft()

    baseline = module.refresh_official_publications(
        CONFIG, storage, archive_root=tmp_path / "archive", person_ids=["pi_jane"], parse_profile=parser
    )
    first_missing = module.refresh_official_publications(
        CONFIG, storage, archive_root=tmp_path / "archive", person_ids=["pi_jane"], parse_profile=parser
    )
    second_missing = module.refresh_official_publications(
        CONFIG, storage, archive_root=tmp_path / "archive", person_ids=["pi_jane"], parse_profile=parser
    )

    assert baseline["added"] == 2
    assert first_missing["added"] == 1
    assert first_missing["pending_missing"] == 1
    assert first_missing["tombstoned"] == 0
    assert second_missing["tombstoned"] == 1
    claims = storage.get_official_publication_source_claims(person_id="pi_jane")
    removed = next(item for item in claims if item["fingerprint_id"] not in {
        claim["fingerprint_id"] for claim in claims if claim["claim_status"] == "active"
    })
    assert removed["claim_status"] == "tombstoned"
    assert storage.publication_summary("pi_jane")["official_fingerprint_count"] == 2
    storage.close()


def test_title_only_work_gaining_doi_keeps_fingerprint_identity(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    _seed_pi(storage)
    title_only = _fingerprint("Reliable Scientific Workflows", 2024)
    with_doi = _fingerprint("Reliable Scientific Workflows", 2024, "10.1234/workflow")
    _install_fetcher(monkeypatch, [_result("h1", 1), _result("h2", 2)])
    snapshots = deque([_parsed(title_only), _parsed(with_doi)])

    module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
        parse_profile=lambda *_args: snapshots.popleft(),
    )
    original_id = storage.conn.execute(
        "SELECT fingerprint_id FROM official_publication_fingerprints"
    ).fetchone()[0]
    result = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
        parse_profile=lambda *_args: snapshots.popleft(),
    )

    rows = storage.conn.execute(
        "SELECT fingerprint_id, doi FROM official_publication_fingerprints"
    ).fetchall()
    assert [(row["fingerprint_id"], row["doi"]) for row in rows] == [
        (original_id, "10.1234/workflow")
    ]
    assert result["added"] == 0
    assert result["updated"] == 1
    storage.close()


def test_dry_run_reports_addition_without_writing_domain_tables(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    _seed_pi(storage)
    _install_fetcher(monkeypatch, [_result("h1", 1)])

    result = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
        dry_run=True,
        parse_profile=lambda *_args: _parsed(
            _fingerprint("Reliable Scientific Workflows", 2024)
        ),
    )

    assert result["added"] == 1
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM official_publication_fingerprints"
    ).fetchone()[0] == 0
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM official_publication_source_claims"
    ).fetchone()[0] == 0
    assert storage.conn.execute("SELECT COUNT(*) FROM raw_sources").fetchone()[0] == 0
    assert storage.conn.execute("SELECT COUNT(*) FROM publication_refresh_runs").fetchone()[0] == 0
    assert not (tmp_path / "archive").exists()
    storage.close()


def test_refresh_fetches_profiles_concurrently_but_serializes_storage(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    template = _seed_pi(storage)
    for index in range(1, 4):
        storage.upsert_pi_record(
            CanonicalPIRecord(
                **{
                    **template.__dict__,
                    "person_id": f"pi_jane_{index}",
                    "display_name": f"Jane Doe {index}",
                    "profile_url": f"https://www.hku.hk/people/jane-doe-{index}",
                    "profile_urls": [f"https://www.hku.hk/people/jane-doe-{index}"],
                }
            )
        )

    guard = Lock()
    activity = {"active": 0, "maximum": 0}

    class ConcurrentFetcher:
        def __init__(self, **_kwargs):
            pass

        def set_domain_delay(self, *_args):
            pass

        def fetch(self, url, **_kwargs):
            with guard:
                activity["active"] += 1
                activity["maximum"] = max(activity["maximum"], activity["active"])
            time.sleep(0.03)
            with guard:
                activity["active"] -= 1
            suffix = url.rsplit("/", 1)[-1]
            return FetchResult(
                url=url,
                final_url=url,
                status_code=200,
                text="profile",
                content_type="text/html",
                content_hash=f"hash-{suffix}",
                body=b"profile",
                fetched_at="2026-07-01T00:00:00+00:00",
            )

    monkeypatch.setattr(module, "Fetcher", ConcurrentFetcher)
    result = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        department_patterns=["Example Department"],
        workers=4,
        parse_profile=lambda *_args: _parsed(
            _fingerprint("Reliable Scientific Workflows", 2024)
        ),
    )

    assert result["sources_checked"] == 4
    assert activity["maximum"] >= 2
    assert storage.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    storage.close()


def test_refresh_can_limit_pilot_to_one_faculty(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    engineering = _seed_pi(storage)
    engineering.department = "Faculty of Engineering; Department of Computing"
    storage.upsert_pi_record(engineering)
    storage.upsert_pi_record(
        CanonicalPIRecord(
            **{
                **engineering.__dict__,
                "person_id": "pi_law",
                "display_name": "John Doe",
                "department": "Faculty of Law",
                "profile_url": "https://www.hku.hk/people/john-doe",
                "profile_urls": ["https://www.hku.hk/people/john-doe"],
            }
        )
    )
    queue = _install_fetcher(monkeypatch, [_result("engineering", 1)])

    result = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        department_patterns=["faculty of engineering"],
        parse_profile=lambda *_args: _parsed(
            _fingerprint("Reliable Scientific Workflows", 2024)
        ),
    )

    assert result["sources_considered"] == 1
    assert result["sources_checked"] == 1
    assert not queue
    assert storage.conn.execute(
        "SELECT COUNT(DISTINCT person_id) FROM official_publication_source_claims"
    ).fetchone()[0] == 1
    storage.close()


def test_due_only_uses_success_watermark_not_failed_or_legacy_check(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    record = _seed_pi(storage)
    storage.upsert_publication_refresh_state(
        record.person_id,
        record.institution_id,
        PROFILE_URL,
        checked_at="2999-01-01T00:00:00+00:00",
        parse_status="pilot_legacy_imported",
        parse_complete=False,
    )
    queue = _install_fetcher(monkeypatch, [_result("legacy-baseline", 1)])

    first = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
        due_only=True,
        parse_profile=lambda *_args: _parsed(
            _fingerprint("Reliable Scientific Workflows", 2024)
        ),
    )

    assert first["sources_checked"] == 1
    assert first["sources_not_due"] == 0
    assert not queue

    second = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
        due_only=True,
        parse_profile=lambda *_args: (_ for _ in ()).throw(
            AssertionError("successful source should not be due")
        ),
    )
    assert second["sources_checked"] == 0
    assert second["sources_not_due"] == 1
    storage.close()


def test_refresh_is_fail_closed_for_empty_or_unmatched_selectors(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    _seed_pi(storage)

    for kwargs in (
        {},
        {"person_ids": []},
        {"department_patterns": []},
        {"person_ids": ["pi_missing"]},
        {"department_patterns": ["Faculty of Nowhere"]},
    ):
        with pytest.raises(ValueError):
            module.refresh_official_publications(
                CONFIG,
                storage,
                archive_root=tmp_path / "archive",
                **kwargs,
            )

    assert storage.conn.execute(
        "SELECT COUNT(*) FROM publication_refresh_runs"
    ).fetchone()[0] == 0
    assert not (tmp_path / "archive").exists()
    storage.close()


def test_refresh_rejects_single_missing_confirmation(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    _seed_pi(storage)
    with pytest.raises(ValueError, match="requires offline"):
        module.refresh_official_publications(
            CONFIG,
            storage,
            person_ids=["pi_jane"],
            missing_confirmations=1,
        )
    storage.close()


def test_offline_refresh_can_explicitly_authorize_single_confirmation(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    _seed_pi(storage)
    result = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
        missing_confirmations=1,
        offline=True,
        allow_single_confirmation_removal=True,
        dry_run=True,
    )
    assert result["single_confirmation_removal_authorized"] is True
    storage.close()


def test_all_fetch_failures_mark_run_failed(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    _seed_pi(storage)
    failed_fetch = FetchResult(
        url=PROFILE_URL,
        final_url=PROFILE_URL,
        status_code=None,
        text="",
        content_type=None,
        content_hash="failed",
        error="transport failed",
        fetched_at="2026-07-01T00:00:00+00:00",
    )
    _install_fetcher(monkeypatch, [failed_fetch])

    result = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
    )

    assert result["status"] == "failed"
    assert result["sources_checked"] == 1
    assert result["sources_usable"] == 0
    assert result["failed"] == 1
    run = storage.get_publication_refresh_run(result["run_id"])
    assert run["status"] == "failed"
    storage.close()


def test_mixed_usable_and_failed_sources_mark_run_partial(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    record = _seed_pi(storage)
    second_url = "https://www.hku.hk/people/jane-doe/publications"
    record.profile_urls = [PROFILE_URL, second_url]
    storage.upsert_pi_record(record)
    failed_fetch = FetchResult(
        url=second_url,
        final_url=second_url,
        status_code=None,
        text="",
        content_type=None,
        content_hash="failed",
        error="transport failed",
        fetched_at="2026-07-02T00:00:00+00:00",
    )
    _install_fetcher(monkeypatch, [_result("good", 1), failed_fetch])

    result = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
        workers=1,
        parse_profile=lambda *_args: _parsed(
            _fingerprint("Reliable Scientific Workflows", 2024)
        ),
    )

    assert result["status"] == "partial"
    assert result["sources_usable"] == 1
    assert result["failed"] == 1
    storage.close()


def test_fingerprint_and_claim_changes_are_atomic_per_source(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    _seed_pi(storage)
    _install_fetcher(monkeypatch, [_result("h1", 1)])

    def fail_reconcile(**_kwargs):
        raise RuntimeError("claim write failed")

    monkeypatch.setattr(storage, "reconcile_official_publication_claims", fail_reconcile)
    with pytest.raises(RuntimeError, match="claim write failed"):
        module.refresh_official_publications(
            CONFIG,
            storage,
            archive_root=tmp_path / "archive",
            person_ids=["pi_jane"],
            parse_profile=lambda *_args: _parsed(
                _fingerprint("Reliable Scientific Workflows", 2024)
            ),
        )

    assert storage.conn.execute(
        "SELECT COUNT(*) FROM official_publication_fingerprints"
    ).fetchone()[0] == 0
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM official_publication_source_claims"
    ).fetchone()[0] == 0
    assert storage.conn.execute("SELECT COUNT(*) FROM raw_sources").fetchone()[0] == 0
    run = storage.conn.execute(
        "SELECT status FROM publication_refresh_runs"
    ).fetchone()
    assert run["status"] == "failed"
    storage.close()


def test_additions_only_source_is_usable_but_not_authoritative(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    _seed_pi(storage)
    _install_fetcher(monkeypatch, [_result("h1", 1)])

    result = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
        parse_profile=lambda *_args: _parsed(
            _fingerprint("Reliable Scientific Workflows", 2024),
            authoritative=False,
        ),
    )

    assert result["status"] == "success"
    assert result["sources_usable"] == 1
    assert result["sources_authoritative"] == 0
    assert result["quarantined"] == 0
    state = storage.get_publication_refresh_state("pi_jane", PROFILE_URL)
    assert state["parse_status"] == "additions_only"
    assert state["parse_complete"] is False
    assert state["last_success_at"]
    storage.close()


def test_offline_dry_run_replays_archive_without_writing_it(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    record = _seed_pi(storage)
    archive_root = tmp_path / "archive"
    body = (
        b"<html><h1>Jane Doe</h1><h2>Publications</h2>"
        b"<p>Reliable Scientific Workflows, 2024</p></html>"
    )
    entry = ContentArchive(archive_root).store(body)
    storage.insert_raw_source(
        RawSourceRecord(
            source_url=PROFILE_URL,
            source_type="official_profile",
            institution_id=record.institution_id,
            fetched_at="2026-07-01T00:00:00+00:00",
            http_status=200,
            content_hash=entry.body_sha256,
            final_url=PROFILE_URL,
            content_type="text/html; charset=utf-8",
            encoding="utf-8",
            archive_key=entry.archive_key,
            body_sha256=entry.body_sha256,
            uncompressed_bytes=entry.uncompressed_bytes,
            compressed_bytes=entry.compressed_bytes,
        )
    )
    before = {
        path.relative_to(archive_root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in archive_root.rglob("*")
        if path.is_file()
    }

    result = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=archive_root,
        person_ids=["pi_jane"],
        offline=True,
        dry_run=True,
        parse_profile=lambda *_args: _parsed(
            _fingerprint("Reliable Scientific Workflows", 2024)
        ),
    )

    after = {
        path.relative_to(archive_root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in archive_root.rglob("*")
        if path.is_file()
    }
    assert result["status"] == "success"
    assert result["added"] == 1
    assert before == after
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM publication_refresh_runs"
    ).fetchone()[0] == 0
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM official_publication_fingerprints"
    ).fetchone()[0] == 0
    storage.close()


def test_due_only_backs_off_quarantine_but_parser_change_retries(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    _seed_pi(storage)
    quarantine_result = _result("quarantine", 15)
    quarantine_result.fetched_at = module.utc_now_iso()
    queue = _install_fetcher(
        monkeypatch,
        [quarantine_result, _result("after-parser-change", 16)],
    )
    quarantined = ProfilePublicationParse(
        fingerprints=[],
        parser_names=("test",),
        parser_errors=(),
        person_match_count=0,
        parsed_person_count=1,
        authoritative=False,
        truncated=False,
        reason="target_identity_not_found",
    )

    first = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
        parse_profile=lambda *_args: quarantined,
    )
    assert first["status"] == "failed"

    backed_off = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
        due_only=True,
        parse_profile=lambda *_args: (_ for _ in ()).throw(
            AssertionError("quarantined source should be in retry backoff")
        ),
    )
    assert backed_off["sources_checked"] == 0
    assert backed_off["sources_retry_backoff"] == 1

    monkeypatch.setattr(module, "PROFILE_PUBLICATION_PARSER_VERSION", "test-new-parser")
    retried = module.refresh_official_publications(
        CONFIG,
        storage,
        archive_root=tmp_path / "archive",
        person_ids=["pi_jane"],
        due_only=True,
        parse_profile=lambda *_args: _parsed(
            _fingerprint("Reliable Scientific Workflows", 2024)
        ),
    )
    assert retried["sources_checked"] == 1
    assert retried["status"] == "success"
    assert not queue
    storage.close()
