from pi_index.verify.email import verify_email
from pi_index.models import CanonicalPIRecord
from pi_index.verify.confidence import contact_verdict_for_pi


def test_high_confidence_email_evidence():
    ev = verify_email(
        "jane@example.edu",
        "https://www.example.edu/people/jane",
        "official_profile",
        ["example.edu"],
        ["example.edu"],
    )
    assert ev.domain_aligned is True
    assert ev.official_source is True
    assert ev.verdict == "official_domain_aligned"


def test_domain_conflict_on_official_source():
    ev = verify_email(
        "jane@old.edu",
        "https://www.example.edu/people/jane",
        "official_profile",
        ["example.edu"],
        ["example.edu"],
    )
    assert ev.official_source is True
    assert ev.domain_aligned is False
    assert ev.verdict == "official_source_domain_conflict"


def test_emeritus_title_is_not_high_confidence_supervisor():
    record = CanonicalPIRecord(
        person_id="pi_1",
        display_name="Jane Doe",
        given_name="Jane",
        family_name="Doe",
        aliases=[],
        institution_id="inst_1",
        institution_name="Example University",
        ror_id=None,
        department=None,
        title="Associate Professor Emeritus",
        profile_url="https://example.edu/jane",
        lab_url=None,
        emails=["jane@example.edu"],
        research_areas=["systems"],
        publications_summary={},
        external_ids={},
        supervision_signals=["title:Associate Professor"],
        source_evidence_ids=[],
        last_checked_at="2026-01-01T00:00:00+00:00",
    )
    ev = verify_email(
        "jane@example.edu",
        "https://example.edu/jane",
        "official_profile",
        ["example.edu"],
        ["example.edu"],
        person_id="pi_1",
    )
    verdict = contact_verdict_for_pi(record, [ev], ["Emeritus"])
    assert verdict.verdict == "retired_or_emeritus_risk"
    assert verdict.likely_supervisor_candidate == "false"


def test_clinical_professor_requires_supervision_review():
    record = CanonicalPIRecord(
        person_id="pi_2",
        display_name="Jane Clinician",
        given_name="Jane",
        family_name="Clinician",
        aliases=[],
        institution_id="inst_1",
        institution_name="Example University",
        ror_id=None,
        department="Medical School",
        title="Clinical Professor in Cardiothoracic Surgery",
        profile_url="https://example.edu/jane-clinician",
        lab_url=None,
        emails=["jane.clinician@example.edu"],
        research_areas=["lung cancer screening with artificial intelligence"],
        publications_summary={},
        external_ids={},
        supervision_signals=["title:Professor", "research_areas_present"],
        source_evidence_ids=[],
        last_checked_at="2026-01-01T00:00:00+00:00",
    )
    ev = verify_email(
        "jane.clinician@example.edu",
        "https://example.edu/jane-clinician",
        "official_profile",
        ["example.edu"],
        ["example.edu"],
        person_id="pi_2",
    )
    verdict = contact_verdict_for_pi(record, [ev], [])
    assert verdict.pi_supervisor_confidence == "medium"
    assert verdict.likely_supervisor_candidate == "unknown"
