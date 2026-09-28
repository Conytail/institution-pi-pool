from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import ParsedPerson
from .generic_html import clean_text, external_ids_from_links
from .mailto import extract_emails_from_html, split_person_and_ambiguous_emails
from .publications import extract_publication_fingerprints


PROFILE_SELECTOR = 'a.profile-link[href*="profiles.php?profile="]'


def _profile_row(link: Tag) -> Tag | None:
    for parent in link.parents:
        if not isinstance(parent, Tag):
            continue
        if "row" in (parent.get("class") or []) and parent.select_one(".name-eng"):
            return parent
    return None


def _appointment_lines(post: Tag | None) -> list[str]:
    if post is None:
        return []
    unit_values = {clean_text(node.get_text(" ", strip=True)) for node in post.select(".unit")}
    values: list[str] = []
    for text in post.stripped_strings:
        value = clean_text(text)
        if not value or value in unit_values:
            continue
        if value not in values:
            values.append(value)
    return values


def _personal_web_url(container: Tag | None, source_url: str) -> str | None:
    if container is None:
        return None
    for candidate in container.find_all("a", href=True):
        if clean_text(candidate.get_text(" ", strip=True)).lower() == "personal web":
            return urljoin(source_url, candidate.get("href") or "")
    return None


def _research_areas(soup: BeautifulSoup) -> list[str]:
    container = soup.select_one("#researchinterest")
    if container is None:
        return []
    values: list[str] = []
    for item in container.select("li"):
        value = clean_text(item.get_text(" ", strip=True))
        if value and value not in values:
            values.append(value)
    return values[:50]


def parse_hkust_faculty_profile(html_text: str, source_url: str) -> ParsedPerson | None:
    """Parse a single official HKUST Faculty Profiles detail page.

    HKUST detail pages contain site-wide addresses and navigation text, so all
    person-local fields are intentionally scoped to ``#profile-div``.  The
    faculty listing itself defines pool membership; appointment wording is
    metadata and is never used as an inclusion or exclusion gate here.
    """

    soup = BeautifulSoup(html_text or "", "html.parser")
    profile = soup.select_one("#profile-div")
    name_node = profile.select_one("#title-name.name-eng, .name .name-eng") if profile else None
    name = clean_text(name_node.get_text(" ", strip=True) if name_node else "")
    if not profile or not name:
        return None

    post = profile.select_one(".post")
    title_values = _appointment_lines(post)
    departments = [clean_text(node.get_text(" ", strip=True)) for node in post.select(".unit")] if post else []
    departments = list(dict.fromkeys(value for value in departments if value))

    contact = profile.select_one(".contact")
    contact_html = str(contact) if contact else ""
    emails, ambiguous_emails = split_person_and_ambiguous_emails(extract_emails_from_html(contact_html))
    evidence_text = clean_text(profile.get_text(" ", strip=True))
    return ParsedPerson(
        name=name,
        title="; ".join(title_values) or None,
        department="; ".join(departments) or None,
        profile_url=source_url,
        lab_url=_personal_web_url(contact, source_url),
        emails=emails,
        ambiguous_emails=ambiguous_emails,
        research_areas=_research_areas(soup),
        external_ids=external_ids_from_links(soup, source_url),
        publication_fingerprints=extract_publication_fingerprints(html_text, source_url),
        source_url=source_url,
        source_type="official_profile",
        extraction_method="hkust_faculty_profile",
        evidence_text=evidence_text[:1000],
        confidence=0.98 if emails else 0.9,
        email_association="person_local" if emails else "none",
    )


def parse_hkust_faculty_directory(html_text: str, source_url: str, config: dict | None = None) -> list[ParsedPerson]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    profile = parse_hkust_faculty_profile(html_text, source_url)
    if profile is not None:
        return [profile]

    people: list[ParsedPerson] = []
    seen_rows: set[int] = set()

    for link in soup.select(PROFILE_SELECTOR):
        row = _profile_row(link)
        if row is None or id(row) in seen_rows:
            continue
        seen_rows.add(id(row))
        name_node = row.select_one(".name-eng")
        name = clean_text(name_node.get_text(" ", strip=True) if name_node else "")
        if not name:
            continue
        title_values = _appointment_lines(row.select_one(".post"))
        title = "; ".join(title_values) or None

        departments = [clean_text(node.get_text(" ", strip=True)) for node in row.select(".post .unit")]
        departments = list(dict.fromkeys(value for value in departments if value))
        emails, ambiguous_emails = split_person_and_ambiguous_emails(extract_emails_from_html(str(row)))
        lab_url = _personal_web_url(row, source_url)
        profile_url = urljoin(source_url, link.get("href") or "")
        evidence_text = clean_text(row.get_text(" ", strip=True))
        people.append(
            ParsedPerson(
                name=name,
                title=title,
                department="; ".join(departments) or None,
                profile_url=profile_url,
                lab_url=lab_url,
                emails=emails,
                ambiguous_emails=ambiguous_emails,
                source_url=source_url,
                source_type="official_directory",
                extraction_method="hkust_faculty_directory",
                evidence_text=evidence_text[:1000],
                confidence=0.95 if emails else 0.85,
                email_association="person_local" if emails else "none",
            )
        )
    return people


def hkust_faculty_candidate_count(html_text: str) -> int:
    soup = BeautifulSoup(html_text or "", "html.parser")
    return len(soup.select(PROFILE_SELECTOR))
