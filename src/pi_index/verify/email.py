from __future__ import annotations

from ..models import EmailEvidence, utc_now_iso
from .official_source import is_official_url


def email_domain(email: str) -> str:
    return email.lower().rsplit("@", 1)[-1]


def domain_aligned(email: str, allowed_domains: list[str]) -> bool:
    domain = email_domain(email)
    normalized = [d.lower().removeprefix("www.") for d in allowed_domains]
    return any(domain == allowed or domain.endswith("." + allowed) for allowed in normalized)


def verify_email(
    email: str,
    source_url: str,
    source_type: str,
    official_domains: list[str],
    allowed_email_domains: list[str],
    person_id: str | None = None,
    association: str = "person_local",
) -> EmailEvidence:
    official = is_official_url(source_url, official_domains)
    aligned = domain_aligned(email, allowed_email_domains)
    if official and aligned:
        verdict = "official_domain_aligned"
        confidence = 0.95
    elif official and not aligned:
        verdict = "official_source_domain_conflict"
        confidence = 0.55
    elif aligned:
        verdict = "aligned_domain_third_party_source"
        confidence = 0.45
    else:
        verdict = "unverified_email"
        confidence = 0.25
    return EmailEvidence(
        email=email,
        source_url=source_url,
        source_type=source_type,
        domain_aligned=aligned,
        official_source=official,
        extracted_at=utc_now_iso(),
        confidence=confidence,
        verdict=verdict,
        person_id=person_id,
        association=association,
    )
