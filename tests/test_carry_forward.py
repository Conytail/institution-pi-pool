from __future__ import annotations

import json

import pytest

from pi_index.models import (
    CanonicalPIRecord,
    EmailEvidence,
    OfficialPublicationFingerprint,
    utc_now_iso,
)
from pi_index.pipeline.carry_forward import carry_forward_records
from pi_index.storage import PIIndexStorage


def _person(person_id: str, name: str, profile_url: str) -> CanonicalPIRecord:
    now = utc_now_iso()
    return CanonicalPIRecord(
        person_id=person_id,
        display_name=name,
        given_name=name.split()[0],
        family_name=name.split()[-1],
        aliases=[],
        institution_id="inst_hku",
        institution_name="The University of Hong Kong",
        ror_id="https://ror.org/02zhqgq86",
        department="Faculty of Architecture",
        title=None,
        profile_url=profile_url,
        lab_url=None,
        emails=["person@hku.hk"],
        research_areas=["urban analytics"],
        publications_summary={},
        external_ids={"orcid": "0000-0001-2345-6789"},
        source_evidence_ids=[],
        last_checked_at=now,
        first_seen_at=now,
        last_seen_at=now,
        last_seen_run_id="archived-run",
        membership_status="active",
        email_association="person_local",
    )


def test_carry_forward_is_selective_read_only_and_neutralizes_legacy_json(tmp_path):
    source_path = tmp_path / "source.db"
    target_path = tmp_path / "target.db"
    report_path = tmp_path / "carry.json"
    source = PIIndexStorage(source_path)
    protected = _person(
        "pi_archived",
        "Alex Shi",
        "https://hub.hku.hk/cris/rp/rp02773",
    )
    source.upsert_pi_record(protected)
    source.upsert_pi_record(
        _person("pi_other", "Other Person", "https://example.hku.hk/people/other")
    )
    source.conn.execute(
        """
        INSERT INTO pi_observations
        (observation_id, person_id, institution_id, run_id, source_url, observed_at, record_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "obs_archived",
            protected.person_id,
            protected.institution_id,
            "archived-run",
            "https://hub.hku.hk/simple-search?filter_value_1=ou00005",
            protected.last_checked_at,
            protected.to_json(),
        ),
    )
    source.insert_email_evidence(
        EmailEvidence(
            email="person@hku.hk",
            source_url=protected.profile_url or "",
            source_type="official_profile",
            domain_aligned=True,
            official_source=True,
            extracted_at=protected.last_checked_at,
            confidence=0.95,
            verdict="official_domain_aligned",
            person_id=protected.person_id,
            association="person_local",
            run_id="archived-run",
        )
    )
    source.upsert_publication_fingerprint(
        OfficialPublicationFingerprint(
            fingerprint_id="pubfp_archived",
            person_id=protected.person_id,
            institution_id=protected.institution_id,
            title="Urban analytics for dense cities",
            citation_text="Urban analytics for dense cities (2025)",
            source_url=protected.profile_url or "",
            run_id="archived-run",
            first_seen_at=protected.last_checked_at,
            last_seen_at=protected.last_checked_at,
            last_seen_run_id="archived-run",
            publication_year=2025,
        )
    )
    source.conn.execute(
        "UPDATE canonical_pi_records SET record_json=json_set(record_json, '$.pi_supervisor_confidence', 'high') WHERE person_id=?",
        (protected.person_id,),
    )
    source.conn.commit()
    source.close()

    before = source_path.read_bytes()
    target = PIIndexStorage(target_path)
    report = carry_forward_records(
        source_path,
        target,
        ["%filter_value_1=ou00005%"],
        reason="official source temporarily unreachable",
        report_path=report_path,
    )

    assert report["selected"] == 1
    assert report["inserted"] == 1
    assert target.get_pi_record("pi_archived") is not None
    assert target.get_pi_record("pi_other") is None
    stored_json = target.conn.execute(
        "SELECT record_json FROM canonical_pi_records WHERE person_id='pi_archived'"
    ).fetchone()[0]
    assert "pi_supervisor_confidence" not in json.loads(stored_json)
    assert target.publication_summary("pi_archived")["official_fingerprint_count"] == 1
    assert target.conn.execute(
        "SELECT COUNT(*) FROM pi_identity_keys WHERE person_id='pi_archived'"
    ).fetchone()[0] > 0
    assert source_path.read_bytes() == before
    assert json.loads(report_path.read_text(encoding="utf-8"))["reason"] == (
        "official source temporarily unreachable"
    )
    target.close()


def test_carry_forward_current_target_and_colliding_evidence_win(tmp_path):
    source_path = tmp_path / "source-old.db"
    target_path = tmp_path / "target-current.db"
    source = PIIndexStorage(source_path)
    carried = _person(
        "pi_archived_alias",
        "Jane Doe",
        "https://old.hku.hk/people/jane-doe",
    )
    carried.title = "Chair Professor"
    carried.emails = ["old@hku.hk"]
    carried.research_areas = ["urban analytics"]
    carried.external_ids = {
        "openalex_author_id": "A123456789",
        "orcid": "0000-0001-2345-6789",
    }
    carried.last_checked_at = "2024-01-01T00:00:00+00:00"
    carried.first_seen_at = carried.last_checked_at
    carried.last_seen_at = carried.last_checked_at
    carried.last_seen_run_id = "archived-2024"
    carried.field_sources = {
        "display_name": "official_profile",
        "title": "official_profile",
        "profile_url": "official_profile",
        "emails": "official_profile",
    }
    source.upsert_pi_record(carried)
    source.conn.execute(
        """
        INSERT INTO pi_observations
        (observation_id, person_id, institution_id, run_id, source_url, observed_at, record_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "obs_old",
            carried.person_id,
            carried.institution_id,
            "archived-2024",
            "https://old.hku.hk/unit/architecture",
            carried.last_checked_at,
            carried.to_json(),
        ),
    )
    source.insert_email_evidence(
        EmailEvidence(
            email="shared@hku.hk",
            source_url="https://hku.hk/contact/jane-doe",
            source_type="official_profile",
            domain_aligned=True,
            official_source=True,
            extracted_at="2024-01-01T00:00:00+00:00",
            confidence=0.8,
            verdict="archived",
            person_id=carried.person_id,
            association="person_local",
            run_id="archived-2024",
        )
    )
    source.upsert_publication_fingerprint(
        OfficialPublicationFingerprint(
            fingerprint_id="old-colliding-doi",
            person_id=carried.person_id,
            institution_id=carried.institution_id,
            title="Old title for the same DOI",
            citation_text="Old citation (2024)",
            source_url=carried.profile_url or "",
            run_id="archived-2024",
            first_seen_at="2024-01-01T00:00:00+00:00",
            last_seen_at="2024-01-01T00:00:00+00:00",
            last_seen_run_id="archived-2024",
            publication_year=2024,
            doi="10.1000/current-wins",
        )
    )
    source.upsert_publication_fingerprint(
        OfficialPublicationFingerprint(
            fingerprint_id="old-distinct-publication",
            person_id=carried.person_id,
            institution_id=carried.institution_id,
            title="Archived evidence for dense city design",
            citation_text="Archived evidence for dense city design (2023)",
            source_url=carried.profile_url or "",
            run_id="archived-2024",
            first_seen_at="2024-01-01T00:00:00+00:00",
            last_seen_at="2024-01-01T00:00:00+00:00",
            last_seen_run_id="archived-2024",
            publication_year=2023,
            doi="10.1000/archived-distinct",
        )
    )
    source.close()

    target = PIIndexStorage(target_path)
    current = _person(
        "pi_current",
        "Jane Doe",
        "https://new.hku.hk/directory/jane-doe",
    )
    current.title = "Professor"
    current.emails = ["new@hku.hk"]
    current.research_areas = []
    current.external_ids = {"openalex_author_id": "A123456789"}
    current.last_checked_at = "2026-07-14T00:00:00+00:00"
    current.first_seen_at = "2026-01-01T00:00:00+00:00"
    current.last_seen_at = current.last_checked_at
    current.last_seen_run_id = "current-2026"
    current.membership_status = "missing"
    current.missing_streak = 1
    current.field_sources = {
        "display_name": "official_directory",
        "title": "official_directory",
        "profile_url": "official_directory",
        "emails": "official_directory",
    }
    target.upsert_pi_record(current)
    target.insert_email_evidence(
        EmailEvidence(
            email="shared@hku.hk",
            source_url="https://hku.hk/contact/jane-doe",
            source_type="official_directory",
            domain_aligned=True,
            official_source=True,
            extracted_at="2026-07-14T00:00:00+00:00",
            confidence=0.99,
            verdict="current",
            person_id=current.person_id,
            association="person_local",
            run_id="current-2026",
        )
    )
    current_publication = OfficialPublicationFingerprint(
        fingerprint_id="placeholder",
        person_id=current.person_id,
        institution_id=current.institution_id,
        title="Current title for the same DOI",
        citation_text="Current citation (2026)",
        source_url=current.profile_url or "",
        run_id="current-2026",
        first_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="current-2026",
        publication_year=2026,
        doi="10.1000/current-wins",
    )
    from pi_index.models import stable_id

    current_publication.fingerprint_id = stable_id(
        "pubfp", current.person_id, "10.1000/current-wins"
    )
    target.upsert_publication_fingerprint(current_publication)

    report = carry_forward_records(
        source_path,
        target,
        ["%old.hku.hk/unit/%"],
        reason="temporary outage",
    )

    assert report["inserted"] == 0
    assert report["merged_with_current"] == 1
    assert report["email_evidence_copied"] == 0
    assert report["publication_fingerprints_copied"] == 1
    assert report["records"] == [
        {
            "source_person_id": carried.person_id,
            "target_person_id": current.person_id,
            "resolution": "same_openalex_author_id",
        }
    ]
    merged = target.get_pi_record(current.person_id)
    assert merged is not None
    assert merged.title == "Professor"
    assert merged.profile_url == "https://new.hku.hk/directory/jane-doe"
    assert merged.emails == ["new@hku.hk"]
    assert merged.research_areas == ["urban analytics"]
    assert merged.membership_status == "missing"
    assert merged.missing_streak == 1
    assert merged.last_checked_at == "2026-07-14T00:00:00+00:00"
    assert merged.external_ids == {
        "openalex_author_id": "A123456789",
        "orcid": "0000-0001-2345-6789",
    }
    assert "https://old.hku.hk/people/jane-doe" in merged.profile_urls
    assert target.resolve_person_id(carried.person_id) == current.person_id
    identity_kinds = {
        row["identity_kind"]
        for row in target.conn.execute(
            "SELECT identity_kind FROM pi_identity_keys WHERE person_id=?",
            (current.person_id,),
        ).fetchall()
    }
    assert {"external:openalex_author_id", "external:orcid"}.issubset(identity_kinds)

    email_row = target.conn.execute(
        """
        SELECT extracted_at, confidence, verdict, run_id
        FROM email_evidence
        WHERE email='shared@hku.hk' AND person_id=?
        """,
        (current.person_id,),
    ).fetchone()
    assert dict(email_row) == {
        "extracted_at": "2026-07-14T00:00:00+00:00",
        "confidence": 0.99,
        "verdict": "current",
        "run_id": "current-2026",
    }
    publication_row = target.conn.execute(
        """
        SELECT title, citation_text, publication_year, last_seen_run_id
        FROM official_publication_fingerprints
        WHERE person_id=? AND doi='10.1000/current-wins'
        """,
        (current.person_id,),
    ).fetchone()
    assert dict(publication_row) == {
        "title": "Current title for the same DOI",
        "citation_text": "Current citation (2026)",
        "publication_year": 2026,
        "last_seen_run_id": "current-2026",
    }
    assert target.publication_summary(current.person_id)["official_fingerprint_count"] == 2
    verdict = target.conn.execute(
        "SELECT last_live_checked_at, run_id FROM contact_verdicts WHERE person_id=?",
        (current.person_id,),
    ).fetchone()
    assert dict(verdict) == {
        "last_live_checked_at": "2026-07-14T00:00:00+00:00",
        "run_id": "current-2026",
    }

    reactivation = carry_forward_records(
        source_path,
        target,
        ["%old.hku.hk/unit/%"],
        reason="explicitly retained inaccessible official source",
        reactivate_existing=True,
    )
    assert reactivation["reactivate_existing"] is True
    reactivated = target.get_pi_record(current.person_id)
    assert reactivated is not None
    assert reactivated.membership_status == "active"
    assert reactivated.missing_streak == 0
    target.close()


def test_carry_forward_rejects_database_output_aliases(tmp_path):
    source_path = tmp_path / "source.db"
    source = PIIndexStorage(source_path)
    source.close()

    same_target = PIIndexStorage(source_path)
    with pytest.raises(ValueError, match="source and target"):
        carry_forward_records(source_path, same_target, ["%anything%"], reason="test")
    same_target.close()

    target = PIIndexStorage(tmp_path / "target.db")
    source_before = source_path.read_bytes()
    target_before = target.db_path.read_bytes()
    with pytest.raises(ValueError, match="report must not overwrite"):
        carry_forward_records(
            source_path,
            target,
            ["%anything%"],
            reason="test",
            report_path=source_path,
        )
    with pytest.raises(ValueError, match="report must not overwrite"):
        carry_forward_records(
            source_path,
            target,
            ["%anything%"],
            reason="test",
            report_path=target.db_path,
        )
    assert source_path.read_bytes() == source_before
    assert target.db_path.read_bytes() == target_before
    target.close()


def test_carry_forward_closes_source_when_selection_fails(tmp_path, monkeypatch):
    import pi_index.pipeline.carry_forward as carry_forward_module

    class FakeReadonlyConnection:
        closed = False

        def close(self):
            self.closed = True

    fake = FakeReadonlyConnection()
    source_path = tmp_path / "source.db"
    source_path.touch()
    target = PIIndexStorage(tmp_path / "target.db")
    monkeypatch.setattr(carry_forward_module, "_readonly_connection", lambda _path: fake)

    def fail_selection(_connection, _patterns):
        raise RuntimeError("selection failed")

    monkeypatch.setattr(carry_forward_module, "_selected_person_ids", fail_selection)
    with pytest.raises(RuntimeError, match="selection failed"):
        carry_forward_module.carry_forward_records(
            source_path,
            target,
            ["%anything%"],
            reason="test",
        )
    assert fake.closed is True
    target.close()
