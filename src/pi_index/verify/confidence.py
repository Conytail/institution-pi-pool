from __future__ import annotations

from ..models import CanonicalPIRecord, EmailEvidence, PIContactVerdict, utc_now_iso
from .supervisor_signal import has_negative_title, supervisor_confidence


def topic_confidence_for_record(record: CanonicalPIRecord) -> str:
    if len(record.research_areas) >= 2:
        return "high"
    if record.research_areas:
        return "medium"
    if record.publications_summary:
        return "medium"
    return "low"


def contact_confidence_for_record(record: CanonicalPIRecord, email_evidence: list[EmailEvidence]) -> tuple[str, list[str], str]:
    if not email_evidence:
        return "none", ["No email was extracted from person-local official evidence."], "unknown"

    official_aligned_local = [
        e for e in email_evidence if e.official_source and e.domain_aligned and e.association == "person_local"
    ]
    official_aligned = [e for e in email_evidence if e.official_source and e.domain_aligned]
    official_conflict = [e for e in email_evidence if e.official_source and not e.domain_aligned]

    has_name = bool(record.display_name)
    has_title = bool(record.title)
    if official_aligned_local and has_name and has_title:
        return "high", ["Official source has person-local email, aligned domain, and sufficient name/title evidence."], "high"
    if official_aligned:
        return "medium", ["Official aligned email exists, but local person association or title evidence is incomplete."], "medium"
    if official_conflict:
        return "low", ["Official source email domain conflicts with configured institution email domains."], "low"
    return "low", ["Email evidence is not from an official aligned person-local source."], "low"


def contact_verdict_for_pi(
    record: CanonicalPIRecord,
    email_evidence: list[EmailEvidence],
    negative_title_patterns: list[str],
) -> PIContactVerdict:
    reasons: list[str] = []
    contact_confidence, contact_reasons, affiliation_confidence = contact_confidence_for_record(record, email_evidence)
    pi_confidence, likely_supervisor, supervisor_reasons = supervisor_confidence(
        record.title,
        " ".join(record.research_areas + record.supervision_signals),
        negative_title_patterns,
    )
    topic_confidence = topic_confidence_for_record(record)
    reasons.extend(contact_reasons)
    reasons.extend(supervisor_reasons)
    if topic_confidence in {"low", "unknown"}:
        reasons.append("Limited topic evidence on the parsed record.")

    negative = has_negative_title(record.title, negative_title_patterns)
    if negative:
        return PIContactVerdict(
            person_id=record.person_id,
            verdict="retired_or_emeritus_risk",
            reasons=[f"title matched negative pattern: {negative}"],
            recommended_action="Do not prioritize unless fresh active-supervision evidence is added.",
            last_live_checked_at=utc_now_iso(),
            contact_confidence=contact_confidence,
            pi_supervisor_confidence="low",
            topic_match_confidence=topic_confidence,
            likely_supervisor_candidate="false",
            current_affiliation_confidence=affiliation_confidence,
        )
    if likely_supervisor == "false":
        return PIContactVerdict(
            person_id=record.person_id,
            verdict="not_pi_or_low_supervision_signal",
            reasons=reasons,
            recommended_action="Keep for audit; do not treat as a likely supervisor yet.",
            last_live_checked_at=utc_now_iso(),
            contact_confidence=contact_confidence,
            pi_supervisor_confidence=pi_confidence,
            topic_match_confidence=topic_confidence,
            likely_supervisor_candidate=likely_supervisor,
            current_affiliation_confidence=affiliation_confidence,
        )
    if contact_confidence == "none":
        return PIContactVerdict(
            person_id=record.person_id,
            verdict="no_official_email",
            reasons=reasons,
            recommended_action="Review profile manually or refresh source templates.",
            last_live_checked_at=utc_now_iso(),
            contact_confidence=contact_confidence,
            pi_supervisor_confidence=pi_confidence,
            topic_match_confidence=topic_confidence,
            likely_supervisor_candidate=likely_supervisor,
            current_affiliation_confidence=affiliation_confidence,
        )
    if contact_confidence == "high":
        return PIContactVerdict(
            person_id=record.person_id,
            verdict="high_confidence_contactable",
            reasons=reasons,
            recommended_action="Contact evidence is strong; use supervisor flag separately before ranking as a supervisor.",
            last_live_checked_at=utc_now_iso(),
            contact_confidence=contact_confidence,
            pi_supervisor_confidence=pi_confidence,
            topic_match_confidence=topic_confidence,
            likely_supervisor_candidate=likely_supervisor,
            current_affiliation_confidence=affiliation_confidence,
        )
    if any(e.official_source and not e.domain_aligned for e in email_evidence):
        verdict = "current_affiliation_conflict"
    else:
        verdict = "stale_risk"
    return PIContactVerdict(
        person_id=record.person_id,
        verdict=verdict,
        reasons=reasons,
        recommended_action="Verify current affiliation before contact or ranking.",
        last_live_checked_at=utc_now_iso(),
        contact_confidence=contact_confidence,
        pi_supervisor_confidence=pi_confidence,
        topic_match_confidence=topic_confidence,
        likely_supervisor_candidate=likely_supervisor,
        current_affiliation_confidence=affiliation_confidence,
    )
