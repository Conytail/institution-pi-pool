from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import ParsedPerson
from .generic_html import clean_text
from .mailto import extract_emails_from_html, split_person_and_ambiguous_emails


CARD_SELECTOR = ".ppl-detail-blk"


def _clean_name(value: str) -> str:
    value = clean_text(value)
    value = re.sub(
        r"^(?:(?:Ar|Ir)\.\s*)?(?:Professor|Prof\.?|Doctor|Dr\.?|Mr\.?|Ms\.?)\s+",
        "",
        value,
        flags=re.I,
    )
    return clean_text(value)


def _source_unit(config: dict, source_url: str) -> str | None:
    best: tuple[int, str] | None = None
    for unit in (config.get("pool_scope") or {}).get("units") or []:
        for seed in unit.get("seed_urls") or []:
            prefix = seed.split("/people/", 1)[0].rstrip("/") + "/"
            if source_url.startswith(prefix):
                candidate = (len(prefix), str(unit.get("name") or ""))
                if candidate[1] and (best is None or candidate[0] > best[0]):
                    best = candidate
    return best[1] if best else None


def parse_polyu_academic_directory(html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    department = _source_unit(config, source_url)
    people: list[ParsedPerson] = []

    for card in soup.select(CARD_SELECTOR):
        name_node = card.select_one(".ppl-detail-blk__name")
        title_node = card.select_one(".ppl-detail-blk__title")
        raw_name = clean_text(name_node.get_text(" ", strip=True) if name_node else "")
        title = clean_text(title_node.get_text(" ", strip=True) if title_node else "") or None
        name = _clean_name(raw_name)
        if not name:
            continue
        profile_link = card.select_one("a.underline-link[href]") or card.select_one("a[href]")
        profile_url = urljoin(source_url, profile_link.get("href") or "") if profile_link else None
        emails, ambiguous_emails = split_person_and_ambiguous_emails(extract_emails_from_html(str(card)))
        lab_url = None
        for link in card.find_all("a", href=True):
            label = clean_text(link.get_text(" ", strip=True)).lower()
            if label in {"personal website", "personal web", "homepage"}:
                lab_url = urljoin(source_url, link.get("href") or "")
                break
        evidence_text = clean_text(card.get_text(" ", strip=True))
        people.append(
            ParsedPerson(
                name=name,
                title=title,
                department=department,
                profile_url=profile_url,
                lab_url=lab_url,
                emails=emails,
                ambiguous_emails=ambiguous_emails,
                source_url=source_url,
                source_type="official_directory",
                extraction_method="polyu_sitecore_academic_directory",
                evidence_text=evidence_text[:1000],
                confidence=0.95 if title and emails else 0.85,
                email_association="person_local" if emails else "none",
            )
        )
    return people


def polyu_academic_candidate_count(html_text: str) -> int:
    return len(BeautifulSoup(html_text or "", "html.parser").select(CARD_SELECTOR))
