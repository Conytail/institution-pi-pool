from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from ..models import ParsedPerson
from .generic_html import clean_text, external_ids_from_links, extract_title, likely_name, normalize_name_candidate
from .mailto import extract_emails_from_html, split_person_and_ambiguous_emails


PERSON_CONTAINER_SELECTORS = [
    ".hpfboxcon",
    ".views-row",
    ".faculty-row",
    ".person",
    ".profile",
    ".directory-listing",
    ".people-listing",
    ".card",
]

NESTED_PERSON_CONTAINER_SELECTORS = [
    ".hpfboxcon",
    ".faculty-row",
    ".person",
    ".profile",
    ".directory-listing",
    ".people-listing",
    ".card",
]

ROLE_LINE_RE = re.compile(
    r"\b(professor|lecturer|dean|programme leader|program leader|principal investigator|group leader|lab director|research fellow)\b",
    re.I,
)

NON_ROLE_LINE_TERMS = {
    "image",
    "view profile",
    "google scholar",
    "orcid",
    "scopus",
    "researchgate",
}


def _safe_lines(tag: Tag) -> list[str]:
    return [clean_text(line) for line in tag.get_text("\n", strip=True).splitlines() if clean_text(line)]


def _extract_research_areas(tag: Tag) -> list[str]:
    areas: list[str] = []
    for link in tag.find_all("a", href=True):
        text = clean_text(link.get_text(" ", strip=True))
        href = link.get("href") or ""
        if text and any(token in href.lower() for token in ["research-area", "research_groups", "research"]):
            areas.append(text)
    text = tag.get_text("\n", strip=True)
    for label in ["Research Areas", "Interests", "Research interests"]:
        if label.lower() in text.lower():
            after = re.split(label + r"\s*:?", text, maxsplit=1, flags=re.I)
            if len(after) == 2:
                chunk = after[1].split("\n", 1)[0]
                areas.extend([clean_text(part) for part in re.split(r"[,;|]", chunk) if clean_text(part)])
    deduped = []
    for area in areas:
        if area not in deduped and len(area) <= 120:
            deduped.append(area)
    return deduped[:12]


def _extract_profile_url(tag: Tag, source_url: str, name: str | None = None) -> str | None:
    candidates: list[str] = []
    for link in tag.find_all("a", href=True):
        href = link.get("href") or ""
        text = clean_text(link.get_text(" ", strip=True))
        if href.startswith("mailto:") or href.startswith("#"):
            continue
        if not href or "/cdn-cgi/l/email-protection" in href:
            continue
        if href.lower().endswith((".pdf", ".jpg", ".png", ".gif")):
            continue
        absolute = urljoin(source_url, href)
        if name and text and name.lower() in text.lower():
            return absolute
        if text.lower() == "view profile":
            return absolute
        candidates.append(absolute)
    return candidates[0] if candidates else None


def _extract_name(tag: Tag) -> str | None:
    for selector in ["h1", "h2", "h3", "h4", ".name", ".field--name-title", ".person-name", "a"]:
        for node in tag.select(selector):
            text = clean_text(node.get_text(" ", strip=True))
            if likely_name(text):
                return normalize_name_candidate(text)
    lines = _safe_lines(tag)
    for line in lines[:6]:
        if likely_name(line):
            return normalize_name_candidate(line)
    return None


def _is_school_or_department_line(line: str) -> bool:
    lower = line.lower()
    return any(term in lower for term in ["department", "school", "faculty", "institute"])


def _extract_title_from_container(tag: Tag, text: str, positive_title_patterns: list[str]) -> str | None:
    role_lines: list[str] = []
    collect_programme_detail = False
    for line in _safe_lines(tag)[:60]:
        lower = line.lower()
        if lower in NON_ROLE_LINE_TERMS or "@" in line:
            continue
        if likely_name(line) or _is_school_or_department_line(line):
            collect_programme_detail = False
            continue
        has_role = ROLE_LINE_RE.search(line) or any(pattern.lower() in lower for pattern in positive_title_patterns)
        if has_role:
            role_lines.append(line)
            collect_programme_detail = "programme leader" in lower or "program leader" in lower
            continue
        if collect_programme_detail:
            if any(term in lower for term in ["doctor of philosophy", "phd", "master of", "msc", "programme", "program"]):
                role_lines.append(line)
                continue
            collect_programme_detail = False

    deduped: list[str] = []
    for line in role_lines:
        if line not in deduped:
            deduped.append(line)
    if deduped:
        return "; ".join(deduped[:8])
    return extract_title(text, positive_title_patterns)


def _person_from_container(
    tag: Tag,
    source_url: str,
    method: str,
    positive_title_patterns: list[str],
) -> ParsedPerson | None:
    text = clean_text(tag.get_text(" ", strip=True))
    if len(text) < 20:
        return None
    all_emails = extract_emails_from_html(str(tag))
    if len(all_emails) > 3:
        return None
    emails, ambiguous_emails = split_person_and_ambiguous_emails(all_emails)
    name = _extract_name(tag)
    if not name:
        return None
    title = _extract_title_from_container(tag, text, positive_title_patterns)
    profile_url = _extract_profile_url(tag, source_url, name)
    if not title and not emails:
        return None
    if not emails and not profile_url and not _has_explicit_person_container_class(tag):
        return None
    department = None
    for line in _safe_lines(tag):
        if any(word in line.lower() for word in ["department", "school", "institute"]):
            department = line
            break
    return ParsedPerson(
        name=name,
        title=title,
        department=department,
        profile_url=profile_url,
        emails=emails,
        ambiguous_emails=ambiguous_emails,
        research_areas=_extract_research_areas(tag),
        external_ids=external_ids_from_links(BeautifulSoup(str(tag), "html.parser"), source_url),
        source_url=source_url,
        source_type="official_directory",
        extraction_method=method,
        evidence_text=text[:1000],
        confidence=0.8 if emails else 0.6,
    )


def _parse_table_rows(soup: BeautifulSoup, source_url: str, positive_title_patterns: list[str]) -> list[ParsedPerson]:
    people: list[ParsedPerson] = []
    for row in soup.select("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        text = clean_text(row.get_text(" ", strip=True))
        if "@" not in text and not any(pattern.lower() in text.lower() for pattern in positive_title_patterns):
            continue
        person = _person_from_container(row, source_url, "faculty_directory_table", positive_title_patterns)
        if person:
            people.append(person)
    return people


def _containers_from_mailto(soup: BeautifulSoup) -> list[Tag]:
    containers: list[Tag] = []
    seen: set[int] = set()
    for link in soup.select("a[href^=mailto], a[href*='/cdn-cgi/l/email-protection']"):
        best: Tag | None = None
        for parent in link.parents:
            if not isinstance(parent, Tag):
                continue
            if parent.name in {"html", "body", "table"}:
                break
            text = clean_text(parent.get_text(" ", strip=True))
            classes = " ".join(parent.get("class") or []).lower()
            mailto_count = len(parent.select("a[href^=mailto]"))
            if mailto_count > 1 and not any(word in classes for word in ["person", "faculty", "profile", "views-row", "directory"]):
                continue
            if 80 <= len(text) <= 2500 and (
                any(word in classes for word in ["person", "faculty", "profile", "views-row", "directory"])
                or re.search(r"\b(professor|lecturer|reader|research|interests)\b", text, re.I)
            ):
                if _extract_name(parent) is None:
                    continue
                best = parent
                break
        if best is not None and id(best) not in seen:
            containers.append(best)
            seen.add(id(best))
    return containers


def _containers_from_role_text(
    soup: BeautifulSoup,
    positive_title_patterns: list[str],
) -> list[Tag]:
    """Find the smallest repeated card around an explicit academic role.

    University directories frequently use site-specific card class names while
    still rendering a name, profile link, and role together.  Anchoring on the
    role keeps this fallback structural and institution-agnostic.
    """

    patterns = [pattern.lower() for pattern in positive_title_patterns if pattern]
    containers: list[Tag] = []
    seen: set[int] = set()
    for text_node in soup.find_all(string=True):
        role_text = clean_text(str(text_node))
        if not role_text or len(role_text) > 240:
            continue
        lower = role_text.lower()
        if not ROLE_LINE_RE.search(role_text) and not any(pattern in lower for pattern in patterns):
            continue
        direct_parent = text_node.parent
        if not isinstance(direct_parent, Tag):
            continue
        best: Tag | None = None
        for parent in direct_parent.parents:
            if not isinstance(parent, Tag) or parent.name in {"html", "body"}:
                break
            if parent.name not in {"article", "li", "div", "td", "section"}:
                continue
            text = clean_text(parent.get_text(" ", strip=True))
            if not 20 <= len(text) <= 2500:
                continue
            name = _extract_name(parent)
            if not name:
                continue
            profile_url = _extract_profile_url(parent, "https://invalid.local/", name)
            if not profile_url:
                continue
            # A directory wrapper can contain hundreds of roles.  A person card
            # should contain only a small number of name-like headings/links.
            name_nodes = 0
            for node in parent.select("h1, h2, h3, h4, a"):
                if likely_name(clean_text(node.get_text(" ", strip=True))):
                    name_nodes += 1
                    if name_nodes > 4:
                        break
            if name_nodes > 4:
                continue
            best = parent
            break
        if best is not None and id(best) not in seen:
            containers.append(best)
            seen.add(id(best))
    return containers


def _same_site_or_external_profile(profile_url: str | None, source_url: str) -> bool:
    if not profile_url:
        return True
    source_host = urlparse(source_url).netloc.lower()
    profile_host = urlparse(profile_url).netloc.lower()
    return not profile_host or profile_host == source_host or not profile_url.lower().endswith((".pdf", ".jpg", ".png"))


def _has_more_specific_person_container(tag: Tag) -> bool:
    classes = set(tag.get("class") or [])
    if "hpfboxcon" in classes:
        return False
    return tag.select_one(",".join(NESTED_PERSON_CONTAINER_SELECTORS)) is not None


def _has_explicit_person_container_class(tag: Tag) -> bool:
    classes = set(tag.get("class") or [])
    return bool(classes.intersection({"hpfboxcon", "faculty-row", "person", "profile", "directory-listing", "people-listing", "card"}))


def parse_faculty_directory(
    html_text: str,
    source_url: str,
    positive_title_patterns: list[str] | None = None,
) -> list[ParsedPerson]:
    positive_title_patterns = positive_title_patterns or [
        "Professor",
        "Associate Professor",
        "Assistant Professor",
        "Lecturer",
        "Reader",
        "Principal Investigator",
    ]
    soup = BeautifulSoup(html_text or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    candidates: list[ParsedPerson] = []
    candidates.extend(_parse_table_rows(soup, source_url, positive_title_patterns))

    containers: list[Tag] = []
    for selector in PERSON_CONTAINER_SELECTORS:
        containers.extend(soup.select(selector))
    containers.extend(_containers_from_mailto(soup))
    role_containers = _containers_from_role_text(soup, positive_title_patterns)
    role_container_ids = {id(tag) for tag in role_containers}
    containers.extend(role_containers)

    seen_container_ids: set[int] = set()
    for tag in containers:
        if id(tag) in seen_container_ids:
            continue
        seen_container_ids.add(id(tag))
        if id(tag) not in role_container_ids and _has_more_specific_person_container(tag):
            continue
        person = _person_from_container(tag, source_url, "faculty_directory_card", positive_title_patterns)
        if person and _same_site_or_external_profile(person.profile_url, source_url):
            candidates.append(person)

    deduped: dict[str, ParsedPerson] = {}
    for person in candidates:
        key = (person.name.lower(), person.profile_url or person.source_url)
        existing = deduped.get(key)
        if not existing or len(person.evidence_text) > len(existing.evidence_text):
            deduped[key] = person
    return list(deduped.values())


def faculty_directory_candidate_block_count(html_text: str) -> int:
    soup = BeautifulSoup(html_text or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    containers: list[Tag] = []
    for selector in PERSON_CONTAINER_SELECTORS:
        containers.extend(soup.select(selector))
    containers.extend(_containers_from_mailto(soup))
    containers.extend(_containers_from_role_text(soup, []))
    seen: set[int] = set()
    count = 0
    for tag in containers:
        if id(tag) in seen:
            continue
        seen.add(id(tag))
        if _has_more_specific_person_container(tag):
            continue
        if len(clean_text(tag.get_text(" ", strip=True))) >= 20:
            count += 1
    return count
