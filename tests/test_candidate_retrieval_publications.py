from pi_index.match.candidate_retrieval import record_text, retrieve_candidates
from pi_index.models import CanonicalPIRecord


def _record(person_id: str, research_areas: list[str] | None = None) -> CanonicalPIRecord:
    return CanonicalPIRecord(
        person_id=person_id,
        display_name=f"Person {person_id}",
        given_name=None,
        family_name=None,
        aliases=[],
        institution_id="institution",
        institution_name="Example University",
        ror_id="https://ror.org/012345678",
        department="Engineering",
        title="Lecturer",
        profile_url=f"https://example.edu/people/{person_id}",
        lab_url=None,
        emails=[],
        research_areas=research_areas or [],
        publications_summary={
            "official_fingerprint_count": 42,
            "official_fingerprint_latest_year": 2026,
        },
        external_ids={},
        source_evidence_ids=[],
        last_checked_at="2026-07-14T00:00:00+00:00",
    )


def test_record_text_uses_explicit_official_publication_titles_not_count_summary():
    record = _record("publication-only")

    without_titles = record_text(record)
    with_titles = record_text(
        record,
        ["Causal Representation Learning for Single-Cell Biology"],
    )

    assert "42" not in without_titles
    assert "2026" not in without_titles
    assert "Causal Representation Learning" in with_titles


def test_candidate_retrieval_can_rank_a_pi_from_stored_official_publications():
    publication_match = _record("publication-match")
    unrelated = _record("unrelated", ["structural concrete engineering"])

    ranked = retrieve_candidates(
        "causal representation learning single-cell biology",
        [unrelated, publication_match],
        {
            publication_match.person_id: [
                "Causal Representation Learning for Single-Cell Biology",
                "Causal Representation Learning for Single-Cell Biology",
            ]
        },
    )

    assert ranked[0][0].person_id == publication_match.person_id
    assert ranked[0][1] > 0
    assert {"causal", "representation", "single-cell", "biology"}.issubset(
        set(ranked[0][2])
    )


def test_publication_mapping_does_not_leak_evidence_between_people():
    left = _record("left")
    right = _record("right")

    ranked = retrieve_candidates(
        "quantum photonics",
        [left, right],
        {right.person_id: "Quantum Photonics with Integrated Emitters"},
    )

    scores = {record.person_id: score for record, score, _overlap in ranked}
    assert scores[right.person_id] > 0
    assert scores[left.person_id] == 0
