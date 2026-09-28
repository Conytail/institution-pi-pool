from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import ParsedPerson
from .generic_html import clean_text
from .mailto import extract_visible_emails, split_person_and_ambiguous_emails
from .publications import extract_publication_fingerprints


def _iter_nodes(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _iter_nodes(item)
    elif isinstance(value, dict):
        yield value
        for key in ["@graph", "mainEntity", "author", "employee", "member"]:
            if key in value:
                yield from _iter_nodes(value[key])


def _is_person(node: dict[str, Any]) -> bool:
    node_type = node.get("@type")
    if isinstance(node_type, list):
        return any(str(t).lower() == "person" for t in node_type)
    return str(node_type).lower() == "person"


def _external_ids_from_same_as(node: dict[str, Any]) -> dict[str, str]:
    values = node.get("sameAs") or []
    if isinstance(values, str):
        values = [values]
    ids: dict[str, str] = {}
    for value in values:
        url = str(value)
        lower = url.lower()
        if "orcid.org/" in lower:
            ids.setdefault("orcid_url", url)
        elif "scholar.google." in lower:
            ids.setdefault("google_scholar_url", url)
        elif "scopus.com/authid" in lower:
            ids.setdefault("scopus_url", url)
        elif "researchgate.net/profile/" in lower:
            ids.setdefault("researchgate_url", url)
    return ids


def parse_jsonld_people(html_text: str, source_url: str) -> list[ParsedPerson]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    people: list[ParsedPerson] = []
    for script in soup.find_all("script", attrs={"type": lambda v: v and "ld+json" in v}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _iter_nodes(data):
            if not _is_person(node):
                continue
            name = clean_text(node.get("name") if isinstance(node.get("name"), str) else "")
            if not name:
                continue
            email_values: list[str] = []
            email_field = node.get("email")
            if isinstance(email_field, str):
                email_values.extend(extract_visible_emails(email_field))
            elif isinstance(email_field, list):
                for item in email_field:
                    email_values.extend(extract_visible_emails(str(item)))
            url = node.get("url") or node.get("@id")
            job_title = node.get("jobTitle")
            department = node.get("department")
            if isinstance(department, dict):
                department = department.get("name")
            person_emails, ambiguous_emails = split_person_and_ambiguous_emails(email_values)
            people.append(
                ParsedPerson(
                    name=name,
                    title=clean_text(str(job_title)) if job_title else None,
                    department=clean_text(str(department)) if department else None,
                    profile_url=urljoin(source_url, str(url)) if url else source_url,
                    emails=person_emails,
                    ambiguous_emails=ambiguous_emails,
                    research_areas=[],
                    external_ids=_external_ids_from_same_as(node),
                    source_url=source_url,
                    source_type="official_jsonld",
                    extraction_method="jsonld_person",
                    evidence_text=json.dumps(node, ensure_ascii=False)[:1000],
                    confidence=0.85,
                    email_association="person_local" if person_emails else "none",
                )
            )
    if len(people) == 1:
        people[0].publication_fingerprints = extract_publication_fingerprints(html_text, source_url)
    return people
