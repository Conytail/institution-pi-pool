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


def test_emeritus_title_adds_review_reason_without_downgrading_official_contact():
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
    verdict = contact_verdict_for_pi(record, [ev])
    assert verdict.verdict == "high_confidence_contactable"
    assert verdict.current_affiliation_confidence == "high"
    assert any("Appointment status may merit manual review" in reason for reason in verdict.reasons)


def test_clinical_professor_title_does_not_change_contact_evidence():
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
    verdict = contact_verdict_for_pi(record, [ev])
    assert verdict.verdict == "high_confidence_contactable"


def test_missing_title_does_not_reduce_person_local_email_confidence():
    record = CanonicalPIRecord(
        person_id="pi_3",
        display_name="Jane Untitled",
        given_name="Jane",
        family_name="Untitled",
        aliases=[],
        institution_id="inst_1",
        institution_name="Example University",
        ror_id=None,
        department="Architecture",
        title=None,
        profile_url="https://example.edu/jane-untitled",
        lab_url=None,
        emails=["jane.untitled@example.edu"],
        research_areas=[],
        publications_summary={},
        external_ids={},
        source_evidence_ids=[],
        last_checked_at="2026-01-01T00:00:00+00:00",
    )
    ev = verify_email(
        "jane.untitled@example.edu",
        "https://example.edu/jane-untitled",
        "official_profile",
        ["example.edu"],
        ["example.edu"],
        person_id="pi_3",
    )

    verdict = contact_verdict_for_pi(record, [ev])

    assert verdict.contact_confidence == "high"
    assert verdict.verdict == "high_confidence_contactable"


def test_research_assistant_professor_is_not_misread_as_research_assistant():
    record = CanonicalPIRecord(
        person_id="pi_4",
        display_name="Anqi Sun",
        given_name="Anqi",
        family_name="Sun",
        aliases=[],
        institution_id="inst_1",
        institution_name="Example University",
        ror_id=None,
        department="Energy and Environment",
        title="Research Assistant Professor",
        profile_url="https://example.edu/anqi-sun",
        lab_url=None,
        emails=["anqi.sun@example.edu"],
        research_areas=["energy systems"],
        publications_summary={},
        external_ids={},
        source_evidence_ids=[],
        last_checked_at="2026-01-01T00:00:00+00:00",
    )
    ev = verify_email(
        "anqi.sun@example.edu",
        "https://example.edu/anqi-sun",
        "official_profile",
        ["example.edu"],
        ["example.edu"],
        person_id="pi_4",
    )

    verdict = contact_verdict_for_pi(record, [ev])

    assert verdict.verdict == "high_confidence_contactable"


def test_honorary_title_without_email_is_only_missing_contact_evidence():
    record = CanonicalPIRecord(
        person_id="pi_5",
        display_name="Aelrun Goette",
        given_name="Aelrun",
        family_name="Goette",
        aliases=[],
        institution_id="inst_1",
        institution_name="Example University",
        ror_id=None,
        department="Architecture",
        title="Honorary Lecturer",
        profile_url="https://example.edu/aelrun-goette",
        lab_url=None,
        emails=[],
        research_areas=[],
        publications_summary={},
        external_ids={},
        source_evidence_ids=[],
        last_checked_at="2026-01-01T00:00:00+00:00",
    )

    verdict = contact_verdict_for_pi(record, [])

    assert verdict.verdict == "no_official_email"
    assert verdict.current_affiliation_confidence == "unknown"
    assert all("appointment" not in reason.lower() for reason in verdict.reasons)
    assert all("supervis" not in reason.lower() for reason in verdict.reasons)
