from __future__ import annotations

import re

from ..models import CanonicalPIRecord, EmailEvidence, PIContactVerdict, utc_now_iso


APPOINTMENT_STATUS_RISK_PATTERNS = (
    "emeritus",
    "retired",
    "former",
)


def appointment_status_risk(title: str | None) -> str | None:
    text = title or ""
    for pattern in APPOINTMENT_STATUS_RISK_PATTERNS:
        if re.search(rf"\b{re.escape(pattern)}\b", text, re.I):
            return pattern
    return None


def topic_confidence_for_record(record: CanonicalPIRecord) -> str:
    if len(record.research_areas) >= 2:
        return "high"
    if record.research_areas:
        return "medium"
    if int((record.publications_summary or {}).get("official_fingerprint_count") or 0) > 0:
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
    if official_aligned_local and has_name:
        return "high", ["Official source has a person-local aligned email and identified person."], "high"
    if official_aligned:
        return "medium", ["Official aligned email exists, but its person-local association is incomplete."], "medium"
    if official_conflict:
        return "low", ["Official source email domain conflicts with configured institution email domains."], "low"
    return "low", ["Email evidence is not from an official aligned person-local source."], "low"


def contact_verdict_for_pi(
    record: CanonicalPIRecord,
    email_evidence: list[EmailEvidence],
) -> PIContactVerdict:
    reasons: list[str] = []
    contact_confidence, contact_reasons, affiliation_confidence = contact_confidence_for_record(record, email_evidence)
    topic_confidence = topic_confidence_for_record(record)
    reasons.extend(contact_reasons)
    if topic_confidence in {"low", "unknown"}:
        reasons.append("Limited topic evidence on the parsed record.")

    appointment_risk = appointment_status_risk(record.title)
    if appointment_risk:
        reasons.append(
            f"Appointment status may merit manual review because the title contains: {appointment_risk}."
        )

    if contact_confidence == "none":
        return PIContactVerdict(
            person_id=record.person_id,
            verdict="no_official_email",
            reasons=reasons,
            recommended_action="Review profile manually or refresh source templates.",
            last_live_checked_at=utc_now_iso(),
            contact_confidence=contact_confidence,
            topic_match_confidence=topic_confidence,
            current_affiliation_confidence=affiliation_confidence,
        )
    if any(e.official_source and not e.domain_aligned for e in email_evidence):
        verdict = "current_affiliation_conflict"
        recommended_action = "Verify current affiliation before contact or ranking."
    elif contact_confidence == "high":
        verdict = "high_confidence_contactable"
        recommended_action = (
            "Contact evidence is strong; optionally review appointment status before outreach."
            if appointment_risk
            else "Contact evidence is strong; rank by research fit and publication evidence."
        )
    else:
        verdict = "stale_risk"
        recommended_action = "Verify current affiliation before contact or ranking."

    return PIContactVerdict(
        person_id=record.person_id,
        verdict=verdict,
        reasons=reasons,
        recommended_action=recommended_action,
        last_live_checked_at=utc_now_iso(),
        contact_confidence=contact_confidence,
        topic_match_confidence=topic_confidence,
        current_affiliation_confidence=affiliation_confidence,
    )
