from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..models import ParsedPerson
from .generic_html import clean_text, external_ids_from_links
from .mailto import extract_emails_from_html, split_person_and_ambiguous_emails
from .publications import extract_publication_fingerprints


PURE_PERSON_CLASS = "person-details"
PURE_PERSON_UUID_RE = re.compile(
    r'\{[^{}]{0,1000}"id"\s*:\s*"'
    r'([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"'
    r'[^{}]{0,1000}"recordType"\s*:\s*"person"[^{}]*\}',
    re.I,
)


def _iter_jsonld_nodes(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _iter_jsonld_nodes(item)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "mainEntity"):
            if key in value:
                yield from _iter_jsonld_nodes(value[key])


def _person_jsonld(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.select('script[type*="ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _iter_jsonld_nodes(data):
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if any(str(value).lower() == "person" for value in types):
                return node
    return {}


def _clean_person_name(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"^(?:Professor|Prof\.?|Doctor|Dr\.?|Mr\.?|Ms\.?|Mrs\.?)\s+", "", value, flags=re.I)
    value = re.sub(r",\s*(?:Professor|Prof\.?|Doctor|Dr\.?)$", "", value, flags=re.I)
    # Pure renders a missing family-name value as a terminal standalone dash
    # for some legitimate mononymous people (for example, "Rashmi-Supriya -").
    value = re.sub(r"\s+[-\N{EN DASH}\N{EM DASH}]\s*$", "", value)
    return clean_text(value)


def _jsonld_affiliations(node: dict[str, Any]) -> list[str]:
    affiliations = node.get("affiliation") or []
    if isinstance(affiliations, (str, dict)):
        affiliations = [affiliations]
    values: list[str] = []
    for affiliation in affiliations:
        value = affiliation.get("name") if isinstance(affiliation, dict) else affiliation
        text = clean_text(str(value or ""))
        if text and text not in values:
            values.append(text)
    return values


def _decode_pure_emails(soup: BeautifulSoup) -> list[str]:
    contact = soup.select_one(".rendering_personorganisationcontactrendererportal")
    header = soup.select_one(".person-details")
    return extract_emails_from_html(str(contact or header or ""))


def _pure_official_person_id(soup: BeautifulSoup, source_url: str) -> str | None:
    """Extract Pure's immutable record UUID from its first-party page data.

    Pure commonly redirects an opaque UUID URL to a human-readable slug.  The
    directory and final profile therefore look unrelated unless the UUID that
    Pure embeds in its page-load payload is retained as an official identity.
    Concept and organisation UUIDs occur on the same page, so the match is
    deliberately restricted to an object whose ``recordType`` is ``person``.
    """

    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        match = PURE_PERSON_UUID_RE.search(raw)
        if not match:
            continue
        host = (urlparse(source_url).hostname or "").lower().removeprefix("www.")
        if host:
            return f"{host}:uuid:{match.group(1).lower()}"
    return None


def _pure_research_areas(soup: BeautifulSoup) -> list[str]:
    values: list[str] = []
    for heading in soup.select("h2, h3, h4"):
        label = clean_text(heading.get_text(" ", strip=True)).lower()
        if label not in {"research interest", "research interests", "research areas", "expertise"}:
            continue
        block = heading.find_next_sibling()
        if block is None:
            continue
        for raw in block.get_text("\n", strip=True).splitlines():
            value = clean_text(raw).strip("-;,")
            if value and len(value) <= 200 and value.lower() not in {"powered by", "fingerprint"}:
                values.append(value)
        if values:
            break
    return list(dict.fromkeys(values))[:20]


def parse_pure_person(html_text: str, source_url: str, config: dict | None = None) -> list[ParsedPerson]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    header = soup.select_one(f".{PURE_PERSON_CLASS}")
    node = _person_jsonld(soup)
    if header is None or not node:
        return []

    heading = header.select_one("h1")
    name = _clean_person_name(str(node.get("name") or (heading.get_text(" ", strip=True) if heading else "")))
    if not name:
        return []

    titles = [clean_text(item.get_text(" ", strip=True)) for item in soup.select(".job-title")]
    jsonld_title = clean_text(str(node.get("jobTitle") or ""))
    if jsonld_title:
        titles.append(jsonld_title)
    titles = list(dict.fromkeys(value for value in titles if value))
    title = "; ".join(titles) or None

    departments = [
        clean_text(item.get_text(" ", strip=True))
        for item in soup.select('.rendering_personorganisationlistrendererportal a[rel="Organisation"]')
    ]
    departments.extend(_jsonld_affiliations(node))
    departments = list(dict.fromkeys(value for value in departments if value))
    department = "; ".join(departments) or None

    person_emails, ambiguous_emails = split_person_and_ambiguous_emails(_decode_pure_emails(soup))
    canonical = soup.select_one('link[rel="canonical"]')
    profile_url = urljoin(source_url, canonical.get("href")) if canonical and canonical.get("href") else source_url
    evidence_text = clean_text(header.get_text(" ", strip=True))
    external_ids = external_ids_from_links(soup, source_url)
    official_person_id = _pure_official_person_id(soup, source_url)
    if official_person_id:
        external_ids.setdefault("official_person_id", official_person_id)
    same_as = node.get("sameAs") or []
    if isinstance(same_as, str):
        same_as = [same_as]
    for value in same_as:
        lower = str(value).lower()
        if "orcid.org/" in lower:
            external_ids.setdefault("orcid_url", str(value))

    return [
        ParsedPerson(
            name=name,
            title=title,
            department=department,
            profile_url=profile_url,
            emails=person_emails,
            ambiguous_emails=ambiguous_emails,
            research_areas=_pure_research_areas(soup),
            external_ids=external_ids,
            publication_fingerprints=extract_publication_fingerprints(html_text, source_url),
            source_url=source_url,
            source_type="official_research_profile",
            extraction_method="elsevier_pure_person",
            evidence_text=evidence_text[:1000],
            confidence=0.95 if title and department else 0.85,
            email_association="person_local" if person_emails else "none",
        )
    ]


def pure_person_candidate_count(html_text: str) -> int:
    soup = BeautifulSoup(html_text or "", "html.parser")
    return 1 if soup.select_one(f".{PURE_PERSON_CLASS}") else 0
