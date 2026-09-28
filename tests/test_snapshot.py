import hashlib
import json

from pi_index.models import (
    CanonicalPIRecord,
    InstitutionRecord,
    PersonEvidence,
    PIContactVerdict,
    RawSourceRecord,
)
from pi_index.pipeline.snapshot import create_institution_snapshot
from pi_index.storage import PIIndexStorage


def _config():
    return {
        "schema_version": 2,
        "config_version": 2,
        "institution": {
            "name": "Example University",
            "homepage_url": "https://example.edu",
            "official_domains": ["example.edu"],
        },
        "pool_scope": {"type": "department", "name": "Computer Science"},
        "site": {"template_family": "generic_faculty_directory_v1"},
        "parsing": {"extract_publication_fingerprints": True},
        "capture": {
            "archive_enabled": True,
            "compression": "gzip",
            "conditional_requests": True,
            "missing_runs_before_inactive": 2,
        },
        "quality_gate": {
            "minimum_people": 1,
            "maximum_duplicate_rate": 0.1,
            "minimum_profile_url_coverage": 1.0,
            "minimum_seed_url_coverage": 1.0,
            "minimum_unit_coverage": 1.0,
            "minimum_profile_fetch_coverage": 1.0,
            "minimum_profile_parse_coverage": 1.0,
            "require_pagination_complete": True,
        },
    }


def _pi(
    title="Professor",
    checked_at="2026-01-01T00:00:00+00:00",
    evidence_ids=None,
    run_id="run-001",
):
    return CanonicalPIRecord(
        person_id="pi_0123456789abcdef",
        display_name="Jane Doe",
        given_name="Jane",
        family_name="Doe",
        aliases=[],
        institution_id="inst_example",
        institution_name="Example University",
        ror_id=None,
        department="Computer Science",
        title=title,
        profile_url="https://example.edu/people/jane-doe",
        lab_url=None,
        emails=["jane.doe@example.edu"],
        research_areas=["machine learning"],
        publications_summary={},
        external_ids={},
        source_evidence_ids=evidence_ids or ["ev_one"],
        last_checked_at=checked_at,
        contact_confidence="high",
        current_affiliation_confidence="high",
        first_seen_at="2026-01-01T00:00:00+00:00",
        last_seen_at=checked_at,
        last_seen_run_id=run_id,
        pool_scope="Computer Science",
    )


def _finish_run(storage, run_id):
    storage.start_ingestion_run(
        run_id,
        "inst_example",
        "Example University",
        "example.yaml",
        "config-sha256",
        "Computer Science",
    )
    metrics = {
        "pages_attempted": 1,
        "pages_succeeded": 1,
        "pages_failed": 0,
        "seed_url_coverage": 1.0,
        "unit_coverage": 1.0,
        "profile_fetch_coverage": 1.0,
        "profile_parse_coverage": 1.0,
        "pagination_complete": True,
        "crawl_complete": True,
    }
    storage.finish_ingestion_run(run_id, metrics, 1, 1, "success", True)


def test_snapshot_is_versioned_checksummed_and_reports_changes(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    _finish_run(storage, "run-001")
    storage.upsert_institution(
        InstitutionRecord(
            institution_id="inst_example",
            name="Example University",
            homepage_url="https://example.edu",
            official_domains=["example.edu"],
        )
    )
    storage.upsert_pi_record(_pi())
    storage.upsert_contact_verdict(
        PIContactVerdict(
            person_id="pi_0123456789abcdef",
            verdict="high_confidence_contactable",
            reasons=["official profile"],
            recommended_action="contact",
            last_live_checked_at="2026-01-01T00:00:00+00:00",
            contact_confidence="high",
            current_affiliation_confidence="high",
            run_id="run-001",
        )
    )
    storage.insert_person_evidence(
        PersonEvidence(
            evidence_id="ev_one",
            person_temp_id="tmp_one",
            institution_id="inst_example",
            field_name="title",
            field_value="Professor",
            source_url="https://example.edu/people/jane-doe",
            source_type="official_profile",
            extraction_method="jsonld_person",
            extracted_at="2026-01-01T00:00:00+00:00",
            confidence=0.9,
            evidence_text="Jane Doe, Professor",
            content_hash="abc",
            run_id="run-001",
        )
    )
    storage.insert_raw_source(
        RawSourceRecord(
            source_url="https://example.edu/people/jane-doe",
            source_type="official_profile",
            institution_id="inst_example",
            fetched_at="2026-01-01T00:00:00+00:00",
            http_status=200,
            content_hash="abc",
            parser_used="jsonld_person",
            run_id="run-001",
        )
    )
    storage.record_duplicate(
        "inst_example",
        "inst_example|identity|pi_0123456789abcdef",
        "pi_0123456789abcdef",
        "pi_already_merged_alias",
        "same_orcid",
        "run-001",
    )

    first = create_institution_snapshot(
        storage,
        "inst_example",
        tmp_path / "snapshots",
        config=_config(),
        config_path="example.yaml",
        run_id="run-001",
    )
    manifest_path = first.snapshot_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["quality_status"] == "pass"
    first_quality = json.loads((first.snapshot_dir / "quality_report.json").read_text(encoding="utf-8"))
    assert first_quality["metrics"]["identity_merge_count"] == 1
    assert first_quality["metrics"]["duplicate_count"] == 0
    assert first_quality["metrics"]["duplicate_rate"] == 0.0
    assert manifest["snapshot_schema_version"] == 2
    assert manifest["counts"]["pi_records"] == 1
    assert manifest["template_family"] == "generic_faculty_directory_v1"
    pi_bytes = (first.snapshot_dir / "pi_records.jsonl").read_bytes()
    assert manifest["files"]["pi_records.jsonl"]["sha256"] == hashlib.sha256(pi_bytes).hexdigest()
    assert json.loads((tmp_path / "snapshots" / "inst_example" / "current.json").read_text())["run_id"] == "run-001"
    assert (first.snapshot_dir / "run_metrics.json").exists()
    assert (first.snapshot_dir / "pi_observations.jsonl").exists()
    assert (first.snapshot_dir / "publication_fingerprints.jsonl").exists()
    assert (first.snapshot_dir / "pi_identity_aliases.jsonl").exists()

    _finish_run(storage, "run-002")
    storage.upsert_pi_record(
        _pi(
            title="Associate Professor",
            checked_at="2026-02-01T00:00:00+00:00",
            evidence_ids=["ev_one", "ev_two"],
            run_id="run-002",
        )
    )
    second = create_institution_snapshot(
        storage,
        "inst_example",
        tmp_path / "snapshots",
        config=_config(),
        config_path="example.yaml",
        run_id="run-002",
    )
    changes = [
        json.loads(line)
        for line in (second.snapshot_dir / "changes.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert second.manifest["previous_run_id"] == "run-001"
    assert changes == [
        {
            "change_type": "changed",
            "person_id": "pi_0123456789abcdef",
            "changed_fields": ["title"],
        }
    ]

    failing_config = _config()
    failing_config["quality_gate"]["minimum_people"] = 2
    _finish_run(storage, "run-003")
    third = create_institution_snapshot(
        storage,
        "inst_example",
        tmp_path / "snapshots",
        config=failing_config,
        config_path="example.yaml",
        run_id="run-003",
    )
    institution_root = tmp_path / "snapshots" / "inst_example"
    assert third.manifest["quality_status"] == "fail"
    assert json.loads((institution_root / "latest.json").read_text())["run_id"] == "run-003"
    assert json.loads((institution_root / "current.json").read_text())["run_id"] == "run-002"

    profile_follow_config = _config()
    profile_follow_config["quality_gate"]["require_profile_follow"] = True
    _finish_run(storage, "run-004")
    fourth = create_institution_snapshot(
        storage,
        "inst_example",
        tmp_path / "snapshots",
        config=profile_follow_config,
        config_path="example.yaml",
        run_id="run-004",
    )
    assert fourth.manifest["quality_status"] == "fail"
    fourth_quality = json.loads((fourth.snapshot_dir / "quality_report.json").read_text(encoding="utf-8"))
    assert fourth_quality["checks"]["profile_follow_exercised"] is False
    storage.close()
