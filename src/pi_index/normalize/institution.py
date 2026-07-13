from __future__ import annotations

from urllib.parse import urlparse

from ..models import InstitutionRecord, stable_id
from ..sources.ror import RORClient


def domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).netloc.lower().removeprefix("www.")


def institution_from_config(config: dict, use_ror: bool = True) -> InstitutionRecord:
    inst = config.get("institution", {})
    name = inst.get("name") or "Unknown Institution"
    configured_homepage = inst.get("homepage_url")
    configured_ror_id = inst.get("ror_id")
    homepage = configured_homepage
    official_domains = list(inst.get("official_domains") or [])
    homepage_domain = domain_from_url(homepage)
    if homepage_domain and homepage_domain not in official_domains:
        official_domains.append(homepage_domain)
    aliases = list(inst.get("aliases") or [])
    ror_id = configured_ror_id
    country = inst.get("country")
    normalized = None
    if use_ror and not ror_id:
        normalized = RORClient().normalize(name)
        if normalized:
            ror_id = normalized.get("ror_id") or ror_id
            country = country or normalized.get("country")
            aliases.extend(normalized.get("aliases") or [])
            homepage = homepage or normalized.get("homepage_url")
    # Live ROR enrichment may add metadata, but it must never change a configured pool ID.
    institution_id = stable_id(
        "inst",
        configured_ror_id or name.lower(),
        configured_homepage or "",
    )
    return InstitutionRecord(
        institution_id=institution_id,
        name=name,
        aliases=sorted(set(aliases)),
        country=country,
        region=inst.get("region"),
        ror_id=ror_id,
        homepage_url=homepage,
        official_domains=sorted(set(d.lower().removeprefix("www.") for d in official_domains)),
        qs_rank=inst.get("qs_rank"),
        qs_year=inst.get("qs_year"),
        source=inst.get("source", "config"),
        status=inst.get("status", "active"),
    )
