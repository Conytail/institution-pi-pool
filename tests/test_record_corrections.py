from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from pi_index.cli import _print_json, build_parser
from pi_index.models import CanonicalPIRecord, EmailEvidence, InstitutionRecord, utc_now_iso
from pi_index.pipeline.record_corrections import _load_corrections, apply_record_corrections
from pi_index.storage import PIIndexStorage


INSTITUTION_ID = "inst_hku_test"
HUB_URL = "https://hub.hku.hk/cris/rp/rp02773"
ARCH_URL = "https://www.arch.hku.hk/staff/rec/shi-alex-shuai/"


def _add_hku(storage: PIIndexStorage) -> None:
    storage.upsert_institution(
        InstitutionRecord(
            institution_id=INSTITUTION_ID,
            name="The University of Hong Kong",
            country="Hong Kong",
            region="Asia",
            ror_id="https://ror.org/02zhqgq86",
            homepage_url="https://www.hku.hk",
            official_domains=["hku.hk"],
        )
    )


def _person(person_id: str = "pi_alex", name: str = "Alex") -> CanonicalPIRecord:
    now = utc_now_iso()
    return CanonicalPIRecord(
        person_id=person_id,
        display_name=name,
        given_name=None,
        family_name=name,
        aliases=[],
        institution_id=INSTITUTION_ID,
        institution_name="The University of Hong Kong",
        ror_id="https://ror.org/02zhqgq86",
        department="Department of Real Estate and Construction",
        title=None,
        profile_url=HUB_URL,
        lab_url=None,
        emails=["wrong@hku.hk"],
        research_areas=["urban economics"],
        publications_summary={},
        external_ids={},
        source_evidence_ids=[],
        last_checked_at=now,
        first_seen_at=now,
        last_seen_at=now,
        last_seen_run_id="crawl-run",
        email_association="person_local",
    )


def _write_corrections(path, *, fields=None, evidence_url=ARCH_URL) -> None:
    payload = {
        "schema_version": 1,
        "corrections": [
            {
                "correction_id": "hku_arch_rp02773",
                "institution_id": INSTITUTION_ID,
                "profile_url": HUB_URL,
                "fields": fields
                or {
                    "display_name": "Shi, Alex S.",
                    "given_name": "Alex S.",
                    "family_name": "Shi",
                    "title": "Assistant Professor",
                    "email": "alexshi@hku.hk",
                    "profile_url": ARCH_URL,
                    "profile_urls": [ARCH_URL, HUB_URL],
                },
                "official_evidence_url": evidence_url,
                "reason": "Official Architecture profile manually verified.",
                "verified_at": "2026-07-14T12:00:00+08:00",
            }
        ],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_apply_record_correction_updates_fields_evidence_verdict_and_identity(tmp_path):
    storage = PIIndexStorage(tmp_path / "target.db")
    _add_hku(storage)
    record = _person()
    storage.upsert_pi_record(record)
    storage.insert_email_evidence(
        EmailEvidence(
            email="wrong@hku.hk",
            source_url=HUB_URL,
            source_type="official_research_directory",
            domain_aligned=True,
            official_source=True,
            extracted_at=record.last_checked_at,
            confidence=0.95,
            verdict="official_domain_aligned",
            person_id=record.person_id,
            association="person_local",
            run_id="crawl-run",
        )
    )
    corrections_path = tmp_path / "corrections.yaml"
    report_path = tmp_path / "report.json"
    _write_corrections(corrections_path)

    report = apply_record_corrections(
        storage,
        corrections_path,
        report_path=report_path,
    )

    corrected = storage.get_pi_record(record.person_id)
    assert corrected is not None
    assert corrected.display_name == "Shi, Alex S."
    assert corrected.given_name == "Alex S."
    assert corrected.family_name == "Shi"
    assert corrected.title == "Assistant Professor"
    assert corrected.emails == ["alexshi@hku.hk"]
    assert corrected.email_association == "person_local"
    assert corrected.profile_url == ARCH_URL
    assert corrected.profile_urls == [ARCH_URL, HUB_URL]
    assert corrected.field_sources["display_name"] == "official_profile_manual_verification"

    evidence_rows = storage.conn.execute(
        "SELECT field_name, source_url, confidence FROM person_evidence WHERE person_temp_id=?",
        (record.person_id,),
    ).fetchall()
    assert {row["field_name"] for row in evidence_rows} == {
        "display_name",
        "given_name",
        "family_name",
        "title",
        "emails",
        "profile_url",
        "profile_urls",
    }
    assert all(row["source_url"] == ARCH_URL and row["confidence"] == 1.0 for row in evidence_rows)

    email_rows = storage.conn.execute(
        "SELECT email, source_url, official_source, domain_aligned FROM email_evidence WHERE person_id=?",
        (record.person_id,),
    ).fetchall()
    assert [tuple(row) for row in email_rows] == [("alexshi@hku.hk", ARCH_URL, 1, 1)]
    verdict = storage.get_contact_verdict_records()[record.person_id]
    assert verdict.verdict == "high_confidence_contactable"
    assert verdict.contact_confidence == "high"

    identity_rows = storage.conn.execute(
        "SELECT identity_kind, identity_value FROM pi_identity_keys WHERE person_id=?",
        (record.person_id,),
    ).fetchall()
    identities = {tuple(row) for row in identity_rows}
    assert ("email", "alexshi@hku.hk") in identities
    assert ("external:official_person_id", "hub.hku.hk:rp:rp02773") in identities
    assert report["applied"] == 1
    assert report["records"][0]["field_changes"]["display_name"]["action"] == (
        "overwrote_non_empty_value"
    )
    assert "display_name" in report["records"][0]["non_empty_fields_overwritten"]
    assert report["records"][0]["email_evidence"] == {"removed": 1, "inserted": 1}
    assert json.loads(report_path.read_text(encoding="utf-8"))["applied"] == 1

    # The old exact Hub URL remains a locator through profile_urls, so reruns are idempotent.
    second = apply_record_corrections(
        storage,
        corrections_path,
        report_path=report_path,
    )
    assert second["records"][0]["field_changes"]["display_name"]["action"] == "unchanged"
    storage.close()


def test_correction_rejects_unsupported_field_without_mutating_target(tmp_path):
    storage = PIIndexStorage(tmp_path / "target.db")
    _add_hku(storage)
    storage.upsert_pi_record(_person())
    corrections_path = tmp_path / "unsafe.yaml"
    report_path = tmp_path / "report.json"
    _write_corrections(
        corrections_path,
        fields={"membership_status": "inactive"},
    )

    with pytest.raises(ValueError, match="Unsupported correction fields"):
        apply_record_corrections(storage, corrections_path, report_path=report_path)

    assert storage.get_pi_record("pi_alex").display_name == "Alex"
    assert not report_path.exists()
    storage.close()


def test_correction_can_clear_a_legacy_unusable_profile_url(tmp_path):
    storage = PIIndexStorage(tmp_path / "target.db")
    _add_hku(storage)
    record = _person()
    storage.upsert_pi_record(record)
    legacy = storage.get_pi_record(record.person_id)
    legacy.profile_url = "mailto:alex@hku.hk"
    legacy.profile_urls = ["mailto:alex@hku.hk"]
    storage.conn.execute(
        "UPDATE canonical_pi_records SET profile_url=?, record_json=? WHERE person_id=?",
        (
            legacy.profile_url,
            json.dumps(legacy.to_dict(), ensure_ascii=False, sort_keys=True),
            record.person_id,
        ),
    )
    storage.conn.commit()
    corrections_path = tmp_path / "clear-profile.yaml"
    report_path = tmp_path / "report.json"
    payload = {
        "schema_version": 1,
        "corrections": [
            {
                "correction_id": "clear_legacy_mailto_profile",
                "institution_id": INSTITUTION_ID,
                "person_id": record.person_id,
                "fields": {"profile_url": None, "profile_urls": []},
                "official_evidence_url": "https://www.hku.hk/",
                "reason": "The legacy value is an email link, not a profile page.",
                "verified_at": "2026-07-14T12:00:00+08:00",
            }
        ],
    }
    corrections_path.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )

    report = apply_record_corrections(
        storage,
        corrections_path,
        report_path=report_path,
    )

    corrected = storage.get_pi_record(record.person_id)
    assert corrected.profile_url is None
    assert corrected.profile_urls == []
    identity_rows = storage.conn.execute(
        "SELECT identity_kind FROM pi_identity_keys WHERE person_id=?",
        (record.person_id,),
    ).fetchall()
    assert all(not row["identity_kind"].startswith("profile") for row in identity_rows)
    assert report["records"][0]["field_changes"]["profile_url"]["after"] is None
    storage.close()


def test_correction_rejects_nonofficial_evidence_and_report_alias(tmp_path):
    storage = PIIndexStorage(tmp_path / "target.db")
    _add_hku(storage)
    storage.upsert_pi_record(_person())
    corrections_path = tmp_path / "corrections.yaml"
    _write_corrections(corrections_path, evidence_url="https://example.org/alex")

    with pytest.raises(ValueError, match="not on an official institution domain"):
        apply_record_corrections(
            storage,
            corrections_path,
            report_path=tmp_path / "report.json",
        )
    with pytest.raises(ValueError, match="must not overwrite"):
        apply_record_corrections(
            storage,
            corrections_path,
            report_path=storage.db_path,
        )
    assert storage.get_pi_record("pi_alex").display_name == "Alex"
    storage.close()


def test_correction_exact_profile_locator_rejects_multiple_matches_before_writes(tmp_path):
    storage = PIIndexStorage(tmp_path / "target.db")
    _add_hku(storage)
    storage.upsert_pi_record(_person("pi_alex_one", "Alex One"))
    storage.upsert_pi_record(_person("pi_alex_two", "Alex Two"))
    corrections_path = tmp_path / "corrections.yaml"
    report_path = tmp_path / "report.json"
    _write_corrections(corrections_path)

    with pytest.raises(ValueError, match="matches=2"):
        apply_record_corrections(storage, corrections_path, report_path=report_path)

    assert storage.get_pi_record("pi_alex_one").display_name == "Alex One"
    assert storage.get_pi_record("pi_alex_two").display_name == "Alex Two"
    assert storage.conn.execute("SELECT COUNT(*) FROM person_evidence").fetchone()[0] == 0
    assert not report_path.exists()
    storage.close()


def test_apply_record_corrections_cli_contract():
    args = build_parser().parse_args(
        [
            "apply-record-corrections",
            "--db",
            "target.db",
            "--corrections",
            "corrections.yaml",
            "--report",
            "report.json",
        ]
    )
    assert args.db == "target.db"
    assert args.corrections == "corrections.yaml"
    assert args.report == "report.json"


def test_cli_json_output_falls_back_on_legacy_console_encoding(monkeypatch):
    class AsciiConsole:
        def __init__(self):
            self.output = ""

        def write(self, text):
            text.encode("ascii")
            self.output += text

        def flush(self):
            return None

    console = AsciiConsole()
    monkeypatch.setattr("sys.stdout", console)

    _print_json({"display_name": "Juha Merilä"})

    assert "Juha Meril\\u00e4" in console.output


def test_apply_record_corrections_cli_refuses_to_create_a_missing_database(tmp_path):
    args = build_parser().parse_args(
        [
            "apply-record-corrections",
            "--db",
            str(tmp_path / "missing.db"),
            "--corrections",
            str(tmp_path / "corrections.yaml"),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )
    with pytest.raises(FileNotFoundError, match="existing target database"):
        args.func(args)
    assert not (tmp_path / "missing.db").exists()


def test_hong_kong_official_corrections_manifest_is_valid():
    manifest = (
        Path(__file__).parent.parent
        / "data"
        / "institutions"
        / "hong_kong"
        / "record_corrections.v1.yaml"
    )

    corrections = _load_corrections(manifest)

    assert len(corrections) == 12
    assert len({item["correction_id"] for item in corrections}) == 12
