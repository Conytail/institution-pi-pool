from __future__ import annotations

from pathlib import Path

from .applicant_parser import load_applicant_text
from .candidate_retrieval import retrieve_candidates
from .candidate_retrieval import record_text
from .explanation import explain_match
from .reranker import score_breakdown
from .semantic import research_intent_text, semantic_similarity
from ..storage import PIIndexStorage


def _normalize_institution(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _matches_institution(record, selected_institution: str | None) -> bool:
    if not selected_institution:
        return True
    selected = _normalize_institution(selected_institution)
    return selected in {
        _normalize_institution(record.institution_id),
        _normalize_institution(record.institution_name),
    }


def match_applicant(
    applicant_path: str | Path,
    storage: PIIndexStorage,
    top_k: int = 20,
    institution: str | None = None,
) -> list[dict]:
    query = research_intent_text(load_applicant_text(applicant_path))
    records = [record for record in storage.iter_pi_records() if _matches_institution(record, institution)]
    verdicts = storage.get_contact_verdict_records()
    ranked = []
    for record, base_score, overlap in retrieve_candidates(query, records):
        verdict_record = verdicts.get(record.person_id)
        if verdict_record and verdict_record.likely_supervisor_candidate == "false":
            continue
        verdict = verdict_record.verdict if verdict_record else "unverified"
        profile_score = semantic_similarity(query, record_text(record))
        scores = score_breakdown(profile_score * 5.0, record, verdict_record, institution_fit_score=1.0)
        ranked.append(
            {
                "person_id": record.person_id,
                "display_name": record.display_name,
                "institution_id": record.institution_id,
                "institution_name": record.institution_name,
                "match_score": scores["total_score"],
                **scores,
                "topic_overlap": ", ".join(overlap),
                "contact_verdict": verdict,
                "explanation": explain_match(record, overlap, verdict),
            }
        )
    ranked = sorted(ranked, key=lambda item: item["total_score"], reverse=True)[:top_k]
    for item in ranked:
        storage.insert_match_result(
            applicant_source=str(applicant_path),
            person_id=item["person_id"],
            display_name=item["display_name"],
            institution_name=item["institution_name"],
            match_score=item["match_score"],
            topic_score=item["topic_score"],
            supervision_score=item["supervision_score"],
            contact_score=item["contact_score"],
            institution_score=item["institution_score"],
            total_score=item["total_score"],
            institution_fit_score=item["institution_fit_score"],
            research_fit_score=item["research_fit_score"],
            supervisor_validity_score=item["supervisor_validity_score"],
            topic_overlap=item["topic_overlap"],
            contact_verdict=item["contact_verdict"],
            explanation=item["explanation"],
        )
    return ranked
