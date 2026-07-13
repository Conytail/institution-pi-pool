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
        "schema_version": 1,
        "config_version": 1,
        "institution": {
            "name": "Example University",
            "homepage_url": "https://example.edu",
            "official_domains": ["example.edu"],
        },
        "pool_scope": {"type": "department", "name": "Computer Science"},
        "site": {"template_family": "generic_faculty_directory_v1"},
        "quality_gate": {
            "minimum_people": 1,
            "maximum_duplicate_rate": 0.1,
            "minimum_profile_url_coverage": 1.0,
        },
    }


def _pi(title="Professor", checked_at="2026-01-01T00:00:00+00:00", evidence_ids=None):
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
        supervision_signals=["Professor"],
        source_evidence_ids=evidence_ids or ["ev_one"],
        last_checked_at=checked_at,
        contact_confidence="high",
        pi_supervisor_confidence="high",
        likely_supervisor_candidate="true",
        current_affiliation_confidence="high",
    )


def test_snapshot_is_versioned_checksummed_and_reports_changes(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
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
            pi_supervisor_confidence="high",
            likely_supervisor_candidate="true",
            current_affiliation_confidence="high",
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
        )
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
    assert manifest["counts"]["pi_records"] == 1
    assert manifest["template_family"] == "generic_faculty_directory_v1"
    pi_bytes = (first.snapshot_dir / "pi_records.jsonl").read_bytes()
    assert manifest["files"]["pi_records.jsonl"]["sha256"] == hashlib.sha256(pi_bytes).hexdigest()
    assert json.loads((tmp_path / "snapshots" / "inst_example" / "current.json").read_text())["run_id"] == "run-001"

    storage.upsert_pi_record(
        _pi(
            title="Associate Professor",
            checked_at="2026-02-01T00:00:00+00:00",
            evidence_ids=["ev_one", "ev_two"],
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
    storage.close()
