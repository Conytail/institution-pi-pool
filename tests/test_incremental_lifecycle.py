from __future__ import annotations

from dataclasses import replace
import json

from pi_index.models import (
    CanonicalPIRecord,
    InstitutionRecord,
    OfficialPublicationFingerprint,
    PIContactVerdict,
    RawSourceRecord,
)
from pi_index.storage import PIIndexStorage


def _pi(**overrides):
    values = {
        "person_id": "pi_0123456789abcdef",
        "display_name": "Jane Doe",
        "given_name": "Jane",
        "family_name": "Doe",
        "aliases": [],
        "institution_id": "inst_example",
        "institution_name": "Example University",
        "ror_id": None,
        "department": "Computer Science",
        "title": "Professor",
        "profile_url": "https://example.edu/people/jane-doe",
        "lab_url": None,
        "emails": ["jane@example.edu"],
        "research_areas": ["machine learning"],
        "publications_summary": {},
        "external_ids": {},
        "source_evidence_ids": ["ev-run-1"],
        "last_checked_at": "2026-01-01T00:00:00+00:00",
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "last_seen_at": "2026-01-01T00:00:00+00:00",
        "last_seen_run_id": "run-1",
        "membership_status": "active",
        "missing_streak": 0,
        "pool_scope": "Computer Science",
    }
    values.update(overrides)
    return CanonicalPIRecord(**values)


def test_pi_lifecycle_only_advances_after_complete_crawls_and_can_reactivate(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    first = _pi()
    storage.upsert_pi_record(first)
    storage.insert_pi_observation(first, "run-1", first.profile_url, {"title": "Professor"})

    storage.reconcile_pi_membership(
        "inst_example",
        "run-incomplete",
        set(),
        crawl_complete=False,
        missing_runs_before_inactive=2,
    )
    unchanged = storage.get_pi_record(first.person_id)
    assert unchanged.membership_status == "active"
    assert unchanged.missing_streak == 0

    first_missing = storage.reconcile_pi_membership(
        "inst_example",
        "run-2",
        set(),
        crawl_complete=True,
        missing_runs_before_inactive=2,
    )
    missing = storage.get_pi_record(first.person_id)
    assert first_missing == {"active": 0, "newly_missing": 1, "newly_inactive": 0}
    assert missing.membership_status == "missing"
    assert missing.missing_streak == 1
    assert [record.person_id for record in storage.iter_pi_records()] == [first.person_id]

    second_missing = storage.reconcile_pi_membership(
        "inst_example",
        "run-3",
        set(),
        crawl_complete=True,
        missing_runs_before_inactive=2,
    )
    inactive = storage.get_pi_record(first.person_id)
    assert second_missing == {"active": 0, "newly_missing": 0, "newly_inactive": 1}
    assert inactive.membership_status == "inactive"
    assert inactive.missing_streak == 2
    assert list(storage.iter_pi_records()) == []
    assert [record.person_id for record in storage.iter_pi_records(include_inactive=True)] == [first.person_id]

    reappeared = replace(
        inactive,
        title="Distinguished Professor",
        last_checked_at="2027-01-01T00:00:00+00:00",
        first_seen_at="2027-01-01T00:00:00+00:00",
        last_seen_at="2027-01-01T00:00:00+00:00",
        last_seen_run_id="run-4",
        membership_status="active",
        missing_streak=0,
        current_affiliation_confidence="high",
        source_evidence_ids=["ev-run-4"],
    )
    storage.upsert_pi_record(reappeared)
    storage.insert_pi_observation(reappeared, "run-4", reappeared.profile_url, {"title": reappeared.title})
    storage.reconcile_pi_membership(
        "inst_example",
        "run-4",
        {first.person_id},
        crawl_complete=True,
        missing_runs_before_inactive=2,
    )

    current = storage.get_pi_record(first.person_id)
    assert current.first_seen_at == "2026-01-01T00:00:00+00:00"
    assert current.last_seen_run_id == "run-4"
    assert current.membership_status == "active"
    assert current.missing_streak == 0
    assert current.title == "Distinguished Professor"
    assert storage.conn.execute("SELECT COUNT(*) FROM pi_observations").fetchone()[0] == 2
    storage.close()


def test_publication_fingerprints_are_incremental_and_preserve_first_seen(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    storage.upsert_pi_record(_pi())
    first = OfficialPublicationFingerprint(
        fingerprint_id="pubfp_0123456789abcdef",
        person_id="pi_0123456789abcdef",
        institution_id="inst_example",
        title="Causal representation learning for biology",
        citation_text="Causal representation learning for biology (2025)",
        source_url="https://example.edu/people/jane-doe",
        run_id="run-1",
        first_seen_at="2026-01-01T00:00:00+00:00",
        last_seen_at="2026-01-01T00:00:00+00:00",
        last_seen_run_id="run-1",
        publication_year=2025,
        doi="10.1000/example",
        publication_url="https://doi.org/10.1000/example",
        confidence=0.9,
    )
    storage.upsert_publication_fingerprint(first)
    storage.upsert_publication_fingerprint(
        replace(
            first,
            citation_text="Causal representation learning for biology. Journal, 2025.",
            run_id="run-2",
            first_seen_at="2027-01-01T00:00:00+00:00",
            last_seen_at="2027-01-01T00:00:00+00:00",
            last_seen_run_id="run-2",
        )
    )

    row = storage.conn.execute(
        "SELECT first_seen_at, last_seen_run_id, record_json FROM official_publication_fingerprints"
    ).fetchone()
    payload = json.loads(row["record_json"])
    assert row["first_seen_at"] == "2026-01-01T00:00:00+00:00"
    assert row["last_seen_run_id"] == "run-2"
    assert payload["first_seen_at"] == "2026-01-01T00:00:00+00:00"
    assert payload["run_id"] == "run-2"
    assert storage.publication_summary(first.person_id) == {
        "official_fingerprint_count": 1,
        "official_fingerprint_latest_year": 2025,
        "official_fingerprint_doi_count": 1,
    }
    storage.close()


def test_profile_url_path_change_can_resolve_to_existing_pi(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    existing = _pi(profile_url="https://example.edu/people/jane-doe")
    storage.upsert_pi_record(existing)
    incoming = _pi(
        person_id="pi_fedcba9876543210",
        profile_url="https://profiles.example.edu/faculty/jane-doe",
        emails=[],
    )

    assert storage.find_existing_duplicate(incoming) == (
        existing.person_id,
        "same_normalized_name_and_profile_slug",
    )
    storage.upsert_person_id_alias(
        incoming.person_id,
        existing.person_id,
        "inst_example",
        "same_normalized_name_and_profile_slug",
        "run-2",
    )
    assert storage.resolve_person_id(incoming.person_id, run_id="run-3") == existing.person_id
    alias = storage.conn.execute(
        "SELECT first_seen_at, last_seen_run_id FROM pi_identity_aliases WHERE alias_person_id=?",
        (incoming.person_id,),
    ).fetchone()
    assert alias["last_seen_run_id"] == "run-3"
    storage.close()


def test_exact_person_profile_can_resolve_a_name_alias_but_not_a_shared_directory(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    existing = _pi(
        display_name="Jonathan York Heng Hui",
        profile_url="https://example.edu/people/jonathan-york-heng-hui",
    )
    storage.upsert_pi_record(existing)

    alias = _pi(
        person_id="pi_alias",
        display_name="Jonathan Hui",
        profile_url=existing.profile_url,
    )
    assert storage.find_existing_duplicate(alias) == (
        existing.person_id,
        "same_profile_url_with_name_alias",
    )

    shared_directory = _pi(
        person_id="pi_shared",
        display_name="Alice Smith",
        profile_url="https://example.edu/people/academic-staff",
        emails=[],
    )
    storage.upsert_pi_record(shared_directory)
    another_person = _pi(
        person_id="pi_other",
        display_name="Bob Jones",
        profile_url=shared_directory.profile_url,
        emails=[],
    )
    assert storage.find_existing_duplicate(another_person) is None
    storage.close()


def test_audit_and_service_exports_treat_304_as_success_and_exclude_inactive_pi(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    storage.upsert_institution(
        InstitutionRecord(
            institution_id="inst_example",
            name="Example University",
            homepage_url="https://example.edu",
            official_domains=["example.edu"],
        )
    )
    active = _pi()
    inactive = _pi(
        person_id="pi_fedcba9876543210",
        display_name="John Smith",
        given_name="John",
        family_name="Smith",
        profile_url="https://example.edu/people/john-smith",
        emails=["john@example.edu"],
        membership_status="inactive",
        missing_streak=2,
        current_affiliation_confidence="none",
    )
    storage.upsert_pi_record(active)
    storage.upsert_pi_record(inactive)
    for person_id in (active.person_id, inactive.person_id):
        storage.upsert_contact_verdict(
            PIContactVerdict(
                person_id=person_id,
                verdict="high_confidence_contactable",
                reasons=["official profile"],
                recommended_action="contact",
                last_live_checked_at="2026-01-01T00:00:00+00:00",
                contact_confidence="high",
                current_affiliation_confidence="high",
            )
        )
    storage.insert_raw_source(
        RawSourceRecord(
            source_url="https://example.edu/faculty",
            source_type="official_directory",
            institution_id="inst_example",
            fetched_at="2026-01-01T00:00:00+00:00",
            http_status=304,
            content_hash="abc",
            not_modified=True,
        )
    )
    storage.insert_raw_source(
        RawSourceRecord(
            source_url="https://example.edu/broken",
            source_type="official_directory",
            institution_id="inst_example",
            fetched_at="2026-01-01T00:01:00+00:00",
            http_status=None,
            content_hash="def",
            error_reason="timeout",
        )
    )

    counts = storage.audit_counts()
    assert counts["pages_successfully_fetched"] == 1
    assert counts["pages_failed"] == 1
    assert counts["canonical_pi_records_created"] == 2
    assert counts["canonical_pi_records_current"] == 1
    assert counts["canonical_pi_records_inactive"] == 1
    assert counts["high_confidence_contactable"] == 1

    output = tmp_path / "export"
    storage.export(output)
    current_rows = (output / "pi_records.jsonl").read_text(encoding="utf-8").splitlines()
    inactive_rows = (output / "inactive_pi_records.jsonl").read_text(encoding="utf-8").splitlines()
    contact_rows = (output / "contact_verdicts.csv").read_text(encoding="utf-8").splitlines()
    assert len(current_rows) == 1 and active.person_id in current_rows[0]
    assert len(inactive_rows) == 1 and inactive.person_id in inactive_rows[0]
    assert len(contact_rows) == 2 and active.person_id in contact_rows[1]
    storage.close()
