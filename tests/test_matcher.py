from pi_index.match.matcher import match_applicant
from pi_index.models import CanonicalPIRecord, OfficialPublicationFingerprint
from pi_index.storage import PIIndexStorage


def pi_record(
    person_id: str,
    institution_name: str,
    research_area: str,
    title: str = "Professor",
) -> CanonicalPIRecord:
    return CanonicalPIRecord(
        person_id=person_id,
        display_name=f"{institution_name} PI",
        given_name=None,
        family_name=None,
        aliases=[],
        institution_id=f"{person_id}_institution",
        institution_name=institution_name,
        ror_id=None,
        department="Computing",
        title=title,
        profile_url=f"https://{person_id}.example.edu/profile",
        lab_url=None,
        emails=[],
        research_areas=[research_area],
        publications_summary={},
        external_ids={},
        source_evidence_ids=[],
        last_checked_at="2026-01-01T00:00:00+00:00",
    )


def test_match_applicant_restricts_candidates_to_selected_institution(tmp_path):
    applicant = tmp_path / "applicant.txt"
    applicant.write_text("active learning protein engineering biofoundry", encoding="utf-8")
    storage = PIIndexStorage(tmp_path / "pi_index.db")
    storage.upsert_pi_record(pi_record("sunway", "Sunway University", "machine learning"))
    storage.upsert_pi_record(pi_record("other", "Other University", "active learning protein engineering"))

    results = match_applicant(applicant, storage, institution="Sunway University")

    assert len(results) == 1
    assert results[0]["institution_name"] == "Sunway University"
    assert results[0]["institution_fit_score"] == 1.0


def test_match_applicant_ranks_specific_semantic_fit_above_broad_method_overlap(tmp_path):
    applicant = tmp_path / "applicant.txt"
    applicant.write_text("causal graph discovery for clinical treatment response biomarkers", encoding="utf-8")
    storage = PIIndexStorage(tmp_path / "pi_index.db")
    storage.upsert_pi_record(pi_record("broad", "Target University", "deep learning optimization systems"))
    storage.upsert_pi_record(
        pi_record("specific", "Target University", "causal graph discovery clinical treatment response biomarkers")
    )

    results = match_applicant(applicant, storage, institution="Target University")

    assert results[0]["person_id"] == "specific"
    assert results[0]["research_fit_score"] > results[1]["research_fit_score"]


def test_title_neither_filters_nor_downranks_research_matches(tmp_path):
    applicant = tmp_path / "applicant.txt"
    applicant.write_text("causal graph discovery", encoding="utf-8")
    storage = PIIndexStorage(tmp_path / "pi_index.db")
    storage.upsert_pi_record(
        pi_record(
            "rap",
            "Target University",
            "causal graph discovery",
            title="Research Assistant Professor",
        )
    )
    storage.upsert_pi_record(
        pi_record(
            "coordinator",
            "Target University",
            "causal graph discovery",
            title="Programme Coordinator",
        )
    )

    results = match_applicant(applicant, storage, institution="Target University")

    assert {result["person_id"] for result in results} == {"rap", "coordinator"}
    assert len({result["total_score"] for result in results}) == 1


def test_official_publication_only_researcher_is_recalled_but_evidenceless_staff_are_not(tmp_path):
    applicant = tmp_path / "applicant.txt"
    applicant.write_text(
        "causal representation learning single-cell biology",
        encoding="utf-8",
    )
    storage = PIIndexStorage(tmp_path / "pi_index.db")
    publication_only = pi_record(
        "publication_only",
        "Target University",
        "",
        title="Lecturer",
    )
    publication_only.research_areas = []
    administrator = pi_record(
        "administrator",
        "Target University",
        "",
        title="Professor and Programme Director",
    )
    administrator.research_areas = []
    polluted = pi_record(
        "polluted",
        "Target University",
        "",
        title="Research Assistant Professor",
    )
    polluted.research_areas = []
    for record in (publication_only, administrator, polluted):
        record.department = "Computing"
        storage.upsert_pi_record(record)
    now = "2026-07-14T00:00:00+00:00"
    storage.upsert_publication_fingerprint(
        OfficialPublicationFingerprint(
            fingerprint_id="fp_meaningful",
            person_id=publication_only.person_id,
            institution_id=publication_only.institution_id,
            title="Causal Representation Learning for Single-Cell Biology",
            citation_text="Causal Representation Learning for Single-Cell Biology. Bioinformatics, 2025.",
            source_url=publication_only.profile_url,
            run_id="run-1",
            first_seen_at=now,
            last_seen_at=now,
            last_seen_run_id="run-1",
            publication_year=2025,
        )
    )
    storage.upsert_publication_fingerprint(
        OfficialPublicationFingerprint(
            fingerprint_id="fp_polluted",
            person_id=polluted.person_id,
            institution_id=polluted.institution_id,
            title="Research output per year",
            citation_text="Research output per year",
            source_url=polluted.profile_url,
            run_id="run-1",
            first_seen_at=now,
            last_seen_at=now,
            last_seen_run_id="run-1",
        )
    )

    results = match_applicant(applicant, storage, institution="Target University")

    assert [result["person_id"] for result in results] == ["publication_only"]
    assert results[0]["research_fit_score"] > 0
