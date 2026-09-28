from __future__ import annotations

from ..models import CanonicalPIRecord, PIContactVerdict


CONFIDENCE_SCORE = {
    "high": 1.0,
    "medium": 0.6,
    "unknown": 0.3,
    "low": 0.15,
    "none": 0.0,
}


def score_breakdown(
    base_score: float,
    record: CanonicalPIRecord,
    verdict: PIContactVerdict | None,
    institution_fit_score: float = 1.0,
) -> dict[str, float]:
    research_fit_score = min(1.0, max(0.0, base_score / 5.0))
    topic_conf = verdict.topic_match_confidence if verdict else record.topic_match_confidence
    if topic_conf == "high":
        research_fit_score = min(1.0, research_fit_score + 0.15)
    elif topic_conf == "medium":
        research_fit_score = min(1.0, research_fit_score + 0.08)

    contact_conf = verdict.contact_confidence if verdict else record.contact_confidence
    contact_score = CONFIDENCE_SCORE.get(contact_conf, 0.0)
    institution_fit_score = 1.0 if institution_fit_score >= 1.0 else 0.0
    total_score = research_fit_score if institution_fit_score else 0.0
    return {
        "institution_fit_score": round(institution_fit_score, 4),
        "research_fit_score": round(research_fit_score, 4),
        "topic_score": round(research_fit_score, 4),
        "contact_score": round(contact_score, 4),
        "institution_score": round(institution_fit_score, 4),
        "total_score": round(total_score, 4),
    }


def rerank_score(base_score: float, record: CanonicalPIRecord, verdict: str) -> float:
    return round(max(0.0, base_score), 4)
