from __future__ import annotations

from dataclasses import replace
import json

import pytest

from pi_index.cli import build_parser
from pi_index.models import (
    CanonicalPIRecord,
    EmailEvidence,
    OfficialPublicationFingerprint,
    PersonEvidence,
    PIContactVerdict,
)
from pi_index.pipeline.reviewed_identity_merge import apply_reviewed_identity_merges
from pi_index.storage import PIIndexStorage


def _person(
    person_id: str,
    *,
    institution_id: str = "inst_hku",
    display_name: str = "Same Display Name",
) -> CanonicalPIRecord:
    return CanonicalPIRecord(
        person_id=person_id,
        display_name=display_name,
        given_name=None,
        family_name=None,
        aliases=[],
        institution_id=institution_id,
        institution_name="Test University",
        ror_id=None,
        department="Test Department",
        title="Lecturer",
        profile_url=f"https://example.edu/people/{person_id}",
        lab_url=None,
        emails=[],
        research_areas=["test research"],
        publications_summary={},
        external_ids={},
        source_evidence_ids=[],
        last_checked_at="2026-07-14T00:00:00+00:00",
        first_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="crawl-run",
        profile_urls=[f"https://example.edu/people/{person_id}"],
        membership_status="active",
    )


def _make_database(path, *records: CanonicalPIRecord) -> None:
    storage = PIIndexStorage(path)
    for record in records:
        storage.upsert_pi_record(record)
    storage.close()


def _write_review(path, pairs) -> None:
    path.write_text(
        json.dumps(
            {
                "audit_type": "unit_test_review",
                "safe_merge_pairs": pairs,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _pair(keep: str, merge: str, *, evidence: str = "Official cross-appointment evidence"):
    return {
        "display_name": "Informational label only",
        "recommended_keep_person_id": keep,
        "merge_person_id": merge,
        "evidence": evidence,
    }


def test_apply_reviewed_identity_merges_uses_only_explicit_pairs_and_is_idempotent(tmp_path):
    database = tmp_path / "target.db"
    review = tmp_path / "review.json"
    first_report = tmp_path / "first-report.json"
    second_report = tmp_path / "second-report.json"
    _make_database(
        database,
        _person("pi_keep"),
        _person("pi_merge"),
        # Same display name is deliberately not enough to merge this row.
        _person("pi_unreviewed"),
    )
    _write_review(review, [_pair("pi_keep", "pi_merge")])

    first = apply_reviewed_identity_merges(
        database,
        review,
        report_path=first_report,
        run_id="manual-review-1",
    )

    assert first["merged_count"] == 1
    assert first["already_merged_count"] == 0
    assert first["integrity_check"] == "ok"
    assert first["records"][0]["status"] == "merged"
    assert first["records"][0]["evidence"] == "Official cross-appointment evidence"
    assert json.loads(first_report.read_text(encoding="utf-8")) == first

    storage = PIIndexStorage(database)
    assert storage.resolve_person_id("pi_merge") == "pi_keep"
    assert storage.get_pi_record("pi_merge") is None
    assert storage.get_pi_record("pi_unreviewed") is not None
    history = storage.conn.execute("SELECT * FROM duplicates").fetchone()
    assert history["group_key"] == "inst_hku|identity|pi_keep"
    assert history["kept_person_id"] == "pi_keep"
    assert history["duplicate_person_id"] == "pi_merge"
    assert history["reason"] == "reviewed_identity_merge"
    assert history["run_id"] == "manual-review-1"
    storage.close()

    second = apply_reviewed_identity_merges(
        database,
        review,
        report_path=second_report,
        run_id="manual-review-2",
    )
    assert second["merged_count"] == 0
    assert second["already_merged_count"] == 1
    assert second["records"][0]["status"] == "already_merged"
    assert second["records"][0]["canonical_person_id_after"] == "pi_keep"
    storage = PIIndexStorage(database)
    assert storage.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 1
    storage.close()


def test_reviewed_identity_merge_reports_final_canonical_for_chained_pairs(tmp_path):
    database = tmp_path / "target.db"
    review = tmp_path / "review.json"
    _make_database(
        database,
        _person("pi_root"),
        _person("pi_middle"),
        _person("pi_leaf"),
    )
    _write_review(
        review,
        [
            _pair("pi_middle", "pi_leaf", evidence="Leaf belongs to middle"),
            _pair("pi_root", "pi_middle", evidence="Middle belongs to root"),
        ],
    )

    report = apply_reviewed_identity_merges(
        database,
        review,
        report_path=tmp_path / "report.json",
        run_id="chain-review",
    )

    assert report["merged_count"] == 2
    assert [
        record["canonical_person_id_after"] for record in report["records"]
    ] == ["pi_root", "pi_root"]
    storage = PIIndexStorage(database)
    assert storage.resolve_person_id("pi_leaf") == "pi_root"
    assert storage.resolve_person_id("pi_middle") == "pi_root"
    assert storage.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 2
    storage.close()


def test_reviewed_identity_merge_preflights_every_pending_pair_before_writing(tmp_path):
    database = tmp_path / "target.db"
    review = tmp_path / "review.json"
    report = tmp_path / "report.json"
    _make_database(
        database,
        _person("pi_keep"),
        _person("pi_merge"),
        _person("pi_other_school", institution_id="inst_other"),
    )
    _write_review(
        review,
        [
            _pair("pi_keep", "pi_merge"),
            _pair("pi_keep", "pi_other_school", evidence="Other evidence"),
        ],
    )

    with pytest.raises(ValueError, match="crosses institutions"):
        apply_reviewed_identity_merges(database, review, report_path=report)

    storage = PIIndexStorage(database)
    assert storage.get_pi_record("pi_keep") is not None
    assert storage.get_pi_record("pi_merge") is not None
    assert storage.resolve_person_id("pi_merge") == "pi_merge"
    storage.close()
    assert not report.exists()


@pytest.mark.parametrize(
    "pair, message",
    [
        (_pair("pi_keep", "pi_merge", evidence="  "), "evidence"),
        (_pair("pi_keep", "pi_keep"), "two different person IDs"),
    ],
)
def test_reviewed_identity_merge_rejects_unsafe_pair_contracts(
    tmp_path,
    pair,
    message,
):
    database = tmp_path / "target.db"
    review = tmp_path / "review.json"
    _make_database(database, _person("pi_keep"), _person("pi_merge"))
    _write_review(review, [pair])

    with pytest.raises(ValueError, match=message):
        apply_reviewed_identity_merges(
            database,
            review,
            report_path=tmp_path / "report.json",
        )


def test_reviewed_identity_merge_requires_existing_database_and_independent_paths(tmp_path):
    review = tmp_path / "review.json"
    _write_review(review, [_pair("pi_keep", "pi_merge")])
    missing_database = tmp_path / "missing.db"

    with pytest.raises(FileNotFoundError, match="does not exist"):
        apply_reviewed_identity_merges(
            missing_database,
            review,
            report_path=tmp_path / "report.json",
        )
    assert not missing_database.exists()

    database = tmp_path / "target.db"
    _make_database(database, _person("pi_keep"), _person("pi_merge"))
    with pytest.raises(ValueError, match="must not overwrite"):
        apply_reviewed_identity_merges(database, review, report_path=review)
    with pytest.raises(ValueError, match="must not be the target database"):
        apply_reviewed_identity_merges(database, database, report_path=tmp_path / "x.json")


def test_apply_reviewed_identity_merges_cli_contract():
    args = build_parser().parse_args(
        [
            "apply-reviewed-identity-merges",
            "--db",
            "target.db",
            "--review",
            "review.json",
            "--report",
            "report.json",
            "--run-id",
            "review-run",
            "--reason",
            "official_cross_appointment_review",
        ]
    )

    assert args.db == "target.db"
    assert args.review == "review.json"
    assert args.report == "report.json"
    assert args.run_id == "review-run"
    assert args.reason == "official_cross_appointment_review"


def test_consolidation_preserves_fields_evidence_flat_columns_and_publication_summary(tmp_path):
    database = tmp_path / "target.db"
    canonical = replace(
        _person("pi_keep", display_name="Preferred Canonical Name"),
        given_name=None,
        family_name="Canonical",
        title="Professor",
        department="Primary Department",
        departments=["Primary Department", "Shared Centre"],
        profile_url="https://example.edu/preferred-profile",
        profile_urls=["https://example.edu/preferred-profile"],
        lab_url=None,
        research_areas=["Artificial Intelligence"],
        source_evidence_ids=["ev_keep"],
        field_sources={
            "title": "canonical_official_profile",
            "profile_url": "canonical_official_profile",
        },
    )
    duplicate = replace(
        _person("pi_merge", display_name="Duplicate Alias"),
        given_name="Duplicate Given",
        family_name="Duplicate Family",
        title="Chair of Finance (by courtesy)",
        department="Joint Department",
        departments=["Joint Department", "Shared Centre"],
        profile_url="https://example.edu/duplicate-profile",
        profile_urls=["https://example.edu/duplicate-profile"],
        lab_url="https://example.edu/labs/research-lab",
        research_areas=["Robotics", "artificial intelligence"],
        source_evidence_ids=["ev_merge"],
        field_sources={
            "title": "duplicate_official_profile",
            "lab_url": "duplicate_official_profile",
            "research_areas": "duplicate_official_profile",
        },
    )
    _make_database(database, canonical, duplicate)
    storage = PIIndexStorage(database)
    for evidence_id, person_id in (("ev_keep", "pi_keep"), ("ev_merge", "pi_merge")):
        storage.insert_person_evidence(
            PersonEvidence(
                evidence_id=evidence_id,
                person_temp_id=person_id,
                institution_id="inst_hku",
                field_name="research_areas",
                field_value="Research evidence",
                source_url=f"https://example.edu/evidence/{evidence_id}",
                source_type="official_profile",
                extraction_method="unit_test",
                extracted_at="2026-07-14T00:00:00+00:00",
                confidence=0.9,
                evidence_text="Official evidence",
                content_hash=evidence_id,
                run_id="crawl-run",
            )
        )
    for fingerprint_id, person_id, title, doi in (
        ("pub_keep", "pi_keep", "Canonical paper", "10.1000/keep"),
        ("pub_merge", "pi_merge", "Duplicate paper", "10.1000/merge"),
    ):
        storage.upsert_publication_fingerprint(
            OfficialPublicationFingerprint(
                fingerprint_id=fingerprint_id,
                person_id=person_id,
                institution_id="inst_hku",
                title=title,
                citation_text=f"{title} (2025)",
                source_url=f"https://example.edu/people/{person_id}",
                run_id="crawl-run",
                first_seen_at="2026-07-14T00:00:00+00:00",
                last_seen_at="2026-07-14T00:00:00+00:00",
                last_seen_run_id="crawl-run",
                publication_year=2025,
                doi=doi,
            )
        )

    storage.consolidate_person_ids(
        "pi_merge",
        "pi_keep",
        "inst_hku",
        "reviewed_identity_merge",
        "review-run",
    )

    merged = storage.get_pi_record("pi_keep")
    assert merged is not None
    assert merged.display_name == "Preferred Canonical Name"
    assert merged.given_name == "Duplicate Given"
    assert merged.family_name == "Canonical"
    assert merged.title == "Professor; Chair of Finance (by courtesy)"
    assert merged.department == "Primary Department"
    assert merged.departments == [
        "Primary Department",
        "Shared Centre",
        "Joint Department",
    ]
    assert merged.profile_url == "https://example.edu/preferred-profile"
    assert merged.profile_urls == [
        "https://example.edu/preferred-profile",
        "https://example.edu/duplicate-profile",
    ]
    assert merged.lab_url == "https://example.edu/labs/research-lab"
    assert merged.research_areas == ["Artificial Intelligence", "Robotics"]
    assert merged.source_evidence_ids == ["ev_keep", "ev_merge"]
    assert merged.field_sources["title"] == "canonical_official_profile"
    assert merged.field_sources["lab_url"] == "duplicate_official_profile"
    assert merged.publications_summary == {
        "official_fingerprint_count": 2,
        "official_fingerprint_latest_year": 2025,
        "official_fingerprint_doi_count": 2,
    }

    row = storage.conn.execute(
        "SELECT * FROM canonical_pi_records WHERE person_id='pi_keep'"
    ).fetchone()
    assert row["display_name"] == merged.display_name
    assert row["title"] == merged.title
    assert row["department"] == merged.department
    assert row["profile_url"] == merged.profile_url
    assert json.loads(row["emails_json"]) == merged.emails
    assert json.loads(row["research_areas_json"]) == merged.research_areas
    assert row["contact_confidence"] == merged.contact_confidence
    assert row["topic_match_confidence"] == merged.topic_match_confidence
    assert row["current_affiliation_confidence"] == merged.current_affiliation_confidence
    assert json.loads(row["record_json"])["lab_url"] == merged.lab_url
    assert {
        item["person_temp_id"]
        for item in storage.conn.execute(
            "SELECT person_temp_id FROM person_evidence "
            "WHERE evidence_id IN ('ev_keep', 'ev_merge')"
        )
    } == {"pi_keep"}
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM official_publication_fingerprints WHERE person_id='pi_keep'"
    ).fetchone()[0] == 2
    storage.close()


def test_consolidation_unions_parallel_titles_and_suppresses_redundant_bare_rank(
    tmp_path,
):
    database = tmp_path / "target.db"
    canonical = replace(
        _person("pi_keep", display_name="Chen Lin"),
        title=(
            "Stelux Professor in Finance; Director, Centre for Financial "
            "Innovation and Development"
        ),
    )
    duplicate = replace(
        _person("pi_merge", display_name="Chen LIN"),
        title=(
            " professor ; Chair of Finance (by courtesy); "
            "DIRECTOR,   CENTRE FOR FINANCIAL INNOVATION AND DEVELOPMENT "
        ),
    )
    _make_database(database, canonical, duplicate)
    storage = PIIndexStorage(database)

    storage.consolidate_person_ids(
        "pi_merge",
        "pi_keep",
        "inst_hku",
        "reviewed_identity_merge",
        "review-run",
    )

    merged = storage.get_pi_record("pi_keep")
    assert merged is not None
    assert merged.title == (
        "Stelux Professor in Finance; Director, Centre for Financial Innovation "
        "and Development; Chair of Finance (by courtesy)"
    )
    storage.close()


def test_consolidation_recomputes_contact_verdict_after_person_local_email_merge(tmp_path):
    database = tmp_path / "target.db"
    canonical = replace(
        _person("pi_keep", display_name="Contact Person"),
        emails=[],
        email_association="none",
        contact_confidence="none",
    )
    duplicate = replace(
        _person("pi_merge", display_name="Contact Person"),
        emails=["contact@example.edu"],
        email_association="person_local",
        contact_confidence="high",
    )
    _make_database(database, canonical, duplicate)
    storage = PIIndexStorage(database)
    storage.upsert_contact_verdict(
        PIContactVerdict(
            person_id="pi_keep",
            verdict="no_official_email",
            reasons=["No email"],
            recommended_action="Review manually",
            last_live_checked_at="2026-07-14T00:00:00+00:00",
            contact_confidence="none",
            topic_match_confidence="medium",
            current_affiliation_confidence="unknown",
            run_id="crawl-run",
        )
    )
    storage.insert_email_evidence(
        EmailEvidence(
            email="contact@example.edu",
            source_url="https://example.edu/people/pi_merge",
            source_type="official_profile",
            domain_aligned=True,
            official_source=True,
            extracted_at="2026-07-14T00:00:00+00:00",
            confidence=0.95,
            verdict="official_domain_aligned",
            person_id="pi_merge",
            association="person_local",
            run_id="crawl-run",
        )
    )

    storage.consolidate_person_ids(
        "pi_merge",
        "pi_keep",
        "inst_hku",
        "reviewed_identity_merge",
        "review-run",
    )

    merged = storage.get_pi_record("pi_keep")
    assert merged is not None
    assert merged.emails == ["contact@example.edu"]
    assert merged.email_association == "person_local"
    assert merged.contact_confidence == "high"
    verdict_row = storage.conn.execute(
        "SELECT * FROM contact_verdicts WHERE person_id='pi_keep'"
    ).fetchone()
    assert verdict_row["verdict"] == "high_confidence_contactable"
    assert verdict_row["contact_confidence"] == "high"
    assert json.loads(verdict_row["record_json"])["run_id"] == "review-run"
    flat = storage.conn.execute(
        "SELECT emails_json, contact_confidence FROM canonical_pi_records "
        "WHERE person_id='pi_keep'"
    ).fetchone()
    assert json.loads(flat["emails_json"]) == ["contact@example.edu"]
    assert flat["contact_confidence"] == "high"
    storage.close()


def test_consolidation_empty_person_local_duplicate_does_not_erase_email(tmp_path):
    database = tmp_path / "target.db"
    canonical = replace(
        _person("pi_keep", display_name="Songhua HU"),
        emails=["songhuhu@example.edu"],
        email_association="ambiguous_email",
        contact_confidence="medium",
    )
    duplicate = replace(
        _person("pi_merge", display_name="HU Songhua"),
        emails=[],
        email_association="person_local",
        contact_confidence="none",
    )
    _make_database(database, canonical, duplicate)
    storage = PIIndexStorage(database)

    storage.consolidate_person_ids(
        "pi_merge",
        "pi_keep",
        "inst_hku",
        "reviewed_identity_merge",
        "review-run",
    )

    merged = storage.get_pi_record("pi_keep")
    assert merged is not None
    assert merged.emails == ["songhuhu@example.edu"]
    assert merged.email_association == "ambiguous_email"
    storage.close()


def test_reviewed_identity_merge_rolls_back_whole_batch_on_midway_failure(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "target.db"
    review = tmp_path / "review.json"
    report = tmp_path / "report.json"
    _make_database(
        database,
        _person("pi_keep_1"),
        _person("pi_merge_1"),
        _person("pi_keep_2"),
        _person("pi_merge_2"),
    )
    _write_review(
        review,
        [
            _pair("pi_keep_1", "pi_merge_1", evidence="Evidence one"),
            _pair("pi_keep_2", "pi_merge_2", evidence="Evidence two"),
        ],
    )
    original = PIIndexStorage.consolidate_person_ids
    calls = 0

    def fail_on_second(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second-pair failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PIIndexStorage, "consolidate_person_ids", fail_on_second)
    with pytest.raises(RuntimeError, match="injected second-pair failure"):
        apply_reviewed_identity_merges(
            database,
            review,
            report_path=report,
            run_id="review-batch",
        )

    assert not report.exists()
    assert list(tmp_path.glob(".report.json.*.tmp")) == []
    storage = PIIndexStorage(database)
    assert {
        row["person_id"]
        for row in storage.conn.execute("SELECT person_id FROM canonical_pi_records")
    } == {"pi_keep_1", "pi_merge_1", "pi_keep_2", "pi_merge_2"}
    assert storage.conn.execute("SELECT COUNT(*) FROM pi_identity_aliases").fetchone()[0] == 0
    assert storage.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 0
    storage.close()
