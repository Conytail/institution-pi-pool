from pi_index.match.paper_backtrace import (
    Paper,
    align_papers_to_pi_records,
    extract_proposal_topics,
    final_score,
    name_similarity,
    overall_fit_score,
    research_fit_score,
    run_paper_backtrace_match,
)
from pi_index.models import CanonicalPIRecord, OfficialPublicationFingerprint, PIContactVerdict
from pi_index.storage import PIIndexStorage


def record(
    name="Jane Doe",
    profile_url="https://target.edu/staff-profiles/jane-doe",
    person_id="pi_1",
    institution_id="inst_1",
    institution_name="Target University",
    research_areas=None,
):
    return CanonicalPIRecord(
        person_id=person_id,
        display_name=name,
        given_name="Jane",
        family_name="Doe",
        aliases=[],
        institution_id=institution_id,
        institution_name=institution_name,
        ror_id=None,
        department="School of Computing and Artificial Intelligence",
        title="Associate Professor",
        profile_url=profile_url,
        lab_url=None,
        emails=["jane@target.edu"],
        research_areas=["machine learning"] if research_areas is None else research_areas,
        publications_summary={},
        external_ids={},
        source_evidence_ids=[],
        last_checked_at="2026-01-01T00:00:00+00:00",
    )


def verdict(contact="high", affiliation="high"):
    return PIContactVerdict(
        person_id="pi_1",
        verdict="high_confidence_contactable",
        reasons=[],
        recommended_action="",
        last_live_checked_at="2026-01-01T00:00:00+00:00",
        contact_confidence=contact,
        current_affiliation_confidence=affiliation,
    )


def test_extract_proposal_topics_builds_queries():
    topics = extract_proposal_topics(
        "I propose reliable machine learning systems for human-AI interaction using robotics sensor data."
    )
    assert "machine learning" in topics.methods
    assert "robotics" in topics.application_domain
    assert "sensor" in topics.data_type
    assert topics.expanded_search_queries


def test_author_backtrace_requires_existing_official_pi_and_matching_institution_affiliation():
    paper = Paper(
        title="Machine learning systems",
        abstract="",
        year=2025,
        authors=[{"name": "Jane Doe", "institutions": ["Target University"]}],
        institutions=["Target University"],
        openalex_id="https://openalex.org/W1",
        relevance_score=0.8,
    )
    matches = align_papers_to_pi_records([paper], [record()])
    assert "pi_1" in matches

    no_affiliation = Paper(
        title="Machine learning systems",
        abstract="",
        year=2025,
        authors=[{"name": "Jane Doe", "institutions": ["Other University"]}],
        institutions=["Other University"],
        openalex_id="https://openalex.org/W2",
        relevance_score=0.8,
    )
    assert align_papers_to_pi_records([no_affiliation], [record()]) == {}
    assert align_papers_to_pi_records([paper], [record(profile_url="")]) == {}


def test_name_similarity_handles_initials():
    assert name_similarity("M A Hannan", "M A Hannan") == 1.0
    assert name_similarity("Professor M A Hannan", "M A Hannan") >= 0.9


def test_final_score_is_research_fit_only():
    assert final_score(0.8, 0.5) == 0.8


def test_run_backtrace_scores_selected_institution_without_requiring_paper_match(tmp_path, monkeypatch):
    applicant = tmp_path / "applicant.txt"
    applicant.write_text("machine learning for protein engineering and active learning", encoding="utf-8")
    storage = PIIndexStorage(tmp_path / "pi_index.db")
    storage.upsert_pi_record(record())
    storage.upsert_pi_record(
        record(
            person_id="pi_2",
            institution_id="inst_2",
            institution_name="Other University",
            profile_url="https://other.edu/jane-doe",
        )
    )
    storage.upsert_contact_verdict(verdict())
    monkeypatch.setattr("pi_index.match.paper_backtrace.retrieve_papers", lambda *args, **kwargs: [])

    results = run_paper_backtrace_match(
        applicant,
        storage,
        tmp_path / "matches.csv",
        institution="Target University",
    )

    assert [result.record.institution_name for result in results] == ["Target University"]
    assert results[0].paper_backtrace_score == 0.0
    assert results[0].semantic_fallback_score > 0.0
    assert results[0].research_fit_score == results[0].final_research_fit_score
    assert results[0].overall_score > 0.0
    assert "no_backtraced_paper" in results[0].risk_flags


def test_broad_paper_backtrace_is_retained_as_a_weak_feature(tmp_path, monkeypatch):
    applicant = tmp_path / "applicant.txt"
    applicant.write_text("causal graph discovery for clinical treatment response", encoding="utf-8")
    storage = PIIndexStorage(tmp_path / "pi_index.db")
    storage.upsert_pi_record(record(research_areas=["machine learning"]))
    storage.upsert_contact_verdict(verdict())
    broad = Paper(
        title="Deep learning for cloud load balancing",
        abstract="generic neural network scheduling optimization",
        year=2025,
        authors=[{"name": "Jane Doe", "institutions": ["Target University"]}],
        institutions=["Target University"],
        openalex_id="https://openalex.org/W-broad",
        relevance_score=0.02,
    )
    relevant = Paper(
        title="Causal graph discovery for clinical treatment response",
        abstract="causal inference methods identify treatment response mechanisms",
        year=2025,
        authors=[{"name": "Jane Doe", "institutions": ["Target University"]}],
        institutions=["Target University"],
        openalex_id="https://openalex.org/W-relevant",
        relevance_score=0.31,
    )
    monkeypatch.setattr("pi_index.match.paper_backtrace.retrieve_papers", lambda *args, **kwargs: [broad, relevant])

    results = run_paper_backtrace_match(
        applicant,
        storage,
        tmp_path / "matches.csv",
        institution="Target University",
    )

    assert len(results[0].matched_papers) == 2
    assert results[0].matched_papers[0].openalex_id == "https://openalex.org/W-relevant"
    assert results[0].matched_papers[1].openalex_id == "https://openalex.org/W-broad"
    assert results[0].paper_backtrace_score > 0


def test_paper_backtrace_excludes_evidenceless_administrative_staff(tmp_path, monkeypatch):
    applicant = tmp_path / "applicant.txt"
    applicant.write_text("causal graph discovery", encoding="utf-8")
    storage = PIIndexStorage(tmp_path / "pi_index.db")
    administrator = record(
        name="Alex Administrator",
        person_id="pi_admin",
        research_areas=[],
    )
    administrator.title = "Professor and Programme Director"
    administrator.department = "Causal Graph Discovery Programme"
    storage.upsert_pi_record(administrator)
    monkeypatch.setattr("pi_index.match.paper_backtrace.retrieve_papers", lambda *args, **kwargs: [])

    results = run_paper_backtrace_match(
        applicant,
        storage,
        tmp_path / "matches.csv",
        institution="Target University",
    )

    assert results == []


def test_paper_backtrace_admits_publication_only_researcher(tmp_path, monkeypatch):
    applicant = tmp_path / "applicant.txt"
    applicant.write_text("causal representation learning single-cell biology", encoding="utf-8")
    storage = PIIndexStorage(tmp_path / "pi_index.db")
    researcher = record(person_id="pi_publication_only", research_areas=[])
    storage.upsert_pi_record(researcher)
    now = "2026-07-14T00:00:00+00:00"
    storage.upsert_publication_fingerprint(
        OfficialPublicationFingerprint(
            fingerprint_id="fp_publication_only",
            person_id=researcher.person_id,
            institution_id=researcher.institution_id,
            title="Causal Representation Learning for Single-Cell Biology",
            citation_text="Causal Representation Learning for Single-Cell Biology. Bioinformatics, 2025.",
            source_url=researcher.profile_url,
            run_id="run-1",
            first_seen_at=now,
            last_seen_at=now,
            last_seen_run_id="run-1",
            publication_year=2025,
        )
    )
    monkeypatch.setattr("pi_index.match.paper_backtrace.retrieve_papers", lambda *args, **kwargs: [])

    results = run_paper_backtrace_match(
        applicant,
        storage,
        tmp_path / "matches.csv",
        institution="Target University",
    )

    assert [result.record.person_id for result in results] == [researcher.person_id]
    assert results[0].semantic_fallback_score > 0


def test_institution_membership_cannot_compensate_for_zero_research_fit():
    assert overall_fit_score(1.0, 0.0) == 0.0


def test_research_fit_uses_max_of_paper_semantic_and_profile_scores():
    assert research_fit_score(paper_score=0.2, semantic_score=0.4, profile_score=0.1) == 0.4
    assert research_fit_score(paper_score=0.5, semantic_score=0.1, profile_score=0.2) == 0.5
    assert research_fit_score(paper_score=0.0, semantic_score=0.0, profile_score=0.3) == 0.3
