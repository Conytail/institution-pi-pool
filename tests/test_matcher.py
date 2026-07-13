from pi_index.match.matcher import match_applicant
from pi_index.models import CanonicalPIRecord
from pi_index.storage import PIIndexStorage


def pi_record(person_id: str, institution_name: str, research_area: str) -> CanonicalPIRecord:
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
        title="Professor",
        profile_url=f"https://{person_id}.example.edu/profile",
        lab_url=None,
        emails=[],
        research_areas=[research_area],
        publications_summary={},
        external_ids={},
        supervision_signals=["title:Professor"],
        source_evidence_ids=[],
        last_checked_at="2026-01-01T00:00:00+00:00",
        pi_supervisor_confidence="high",
        likely_supervisor_candidate="true",
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
