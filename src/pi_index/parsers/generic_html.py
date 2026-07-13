from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from ..models import ParsedPerson
from .mailto import extract_emails_from_html, split_person_and_ambiguous_emails


TITLE_RE = re.compile(
    r"\b(Distinguished Research Professor|Distinguished Professor|Adjunct Associate Professor|Visiting Associate Professor|Associate Professor|Assistant Professor|Senior Lecturer|Professor of Teaching|Principal Investigator|Group Leader|Lab Director|Deputy Dean|Dean|Programme Leader|Program Leader|Research Fellow|Professor|Reader|Lecturer)\b",
    re.I,
)

ACADEMIC_NAME_PREFIX_RE = re.compile(
    r"^(?:(?:Adjunct|Visiting|Distinguished(?: Research)?|Emeritus)\s+)?(?:(?:Associate|Assistant)\s+)?Professor\b\.?"
    r"|^Senior Lecturer\b\.?"
    r"|^Lecturer\b\.?"
    r"|^Dr\.-Ing\.?"
    r"|^Prof\b\.?"
    r"|^Dr\b\.?"
    r"|^Ts\b\.?"
    r"|^Ir\b\.?"
    r"|^Dato(?:'|\u2019)?(?:\s+Sri)?\b\.?"
    r"|^Datuk\b\.?",
    re.I,
)

ACADEMIC_NAME_SUFFIX_RE = re.compile(
    r"(?:,?\s+)(?:Ph\.?\s?D\.?|D\.?Phil\.?|M\.?D\.?|Sc\.?D\.?|Dr\.?PH\.?|"
    r"M\.?P\.?H\.?|M\.?B\.?A\.?|M\.?S\.?|M\.?Sc\.?|B\.?Sc\.?|"
    r"CCC-[A-Za-z-]+|F[A-Z]{2,})\s*\.?$",
    re.I,
)

GENERIC_NAME_LABELS = {
    "academic & professional qualifications",
    "academic and professional qualifications",
    "apply now",
    "biography",
    "contact",
    "contact information",
    "computing and artificial intelligence",
    "courses taught",
    "course spotlights",
    "faculty",
    "faculty by name",
    "featured schools",
    "google scholar",
    "information for",
    "invited panel",
    "main navigation",
    "more here",
    "name",
    "notable publications",
    "people",
    "profile",
    "publications",
    "research areas",
    "research gate",
    "researchgate",
    "research groups",
    "research interests",
    "share on",
    "sort ascending",
    "sort descending",
    "staff profiles",
    "smart photonics research laboratory",
    "teaching areas",
    "view details",
    "engineering and",
}

GENERIC_NAME_TERMS = [
    "navigation",
    "apply now",
    "research area",
    "research group",
    "information for",
    "course spotlights",
    "more here",
    "featured schools",
    "google scholar",
    "contact information",
    "view details",
    "sort ascending",
    "sort descending",
    "department of",
    "school of",
    "faculty of",
    "staff profiles",
    "laboratory",
    "electrical & computer engineering",
    "computer engineering",
    "field member",
    "associate member",
    "vice-president",
    "admin staff",
    "faculty member",
    "their cv",
    "tech staff",
]


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_name_candidate(value: str) -> str:
    candidate = clean_text(value).strip(" -|,:")
    previous = None
    while candidate and candidate != previous:
        previous = candidate
        candidate = ACADEMIC_NAME_PREFIX_RE.sub("", candidate).strip(" .,-|:")
        candidate = ACADEMIC_NAME_SUFFIX_RE.sub("", candidate).strip(" .,-|:")
    return clean_text(candidate)


def html_text(html_text: str) -> str:
    soup = BeautifulSoup(html_text or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return clean_text(soup.get_text(" ", strip=True))


def external_ids_from_links(soup: BeautifulSoup, source_url: str) -> dict[str, str]:
    ids: dict[str, str] = {}
    for link in soup.find_all("a", href=True):
        href = urljoin(source_url, link.get("href") or "")
        lower = href.lower()
        if "orcid.org/" in lower:
            match = re.search(r"orcid\.org/(\d{4}-\d{4}-\d{4}-[\dXx]{4})", href)
            if match:
                ids.setdefault("orcid", match.group(1).upper())
                ids.setdefault("orcid_url", f"https://orcid.org/{match.group(1).upper()}")
        elif "scholar.google." in lower:
            ids.setdefault("google_scholar_url", href)
            user = parse_qs(urlparse(href).query).get("user")
            if user:
                ids.setdefault("google_scholar_id", user[0])
        elif "scopus.com/authid" in lower:
            ids.setdefault("scopus_url", href)
            author = parse_qs(urlparse(href).query).get("authorId")
            if author:
                ids.setdefault("scopus_author_id", author[0])
        elif "researchgate.net/profile/" in lower:
            ids.setdefault("researchgate_url", href)
    return ids


def research_areas_from_profile(soup: BeautifulSoup) -> list[str]:
    stop_labels = {
        "academic & professional qualifications",
        "academic and professional qualifications",
        "awards",
        "biography",
        "courses taught",
        "notable publications",
        "publications",
        "sdgs focus",
        "teaching areas",
    }
    areas: list[str] = []
    for link in soup.find_all("a", href=True):
        text = clean_text(link.get_text(" ", strip=True))
        href = (link.get("href") or "").lower()
        if text and ("/research-interests/" in href or "/research-area" in href):
            areas.append(text)

    lines = [clean_text(line) for line in soup.get_text("\n", strip=True).splitlines()]
    for index, line in enumerate(lines):
        if line.lower() != "research interests":
            continue
        for item in lines[index + 1 : index + 20]:
            lower = item.lower()
            if not item or lower in stop_labels:
                break
            if len(item) <= 120 and "@" not in item and not item.lower().startswith("http"):
                areas.append(item)
        break

    deduped: list[str] = []
    for area in areas:
        if area not in deduped and area.lower() not in {"research", "research interests"}:
            deduped.append(area)
    return deduped[:20]


def likely_name(value: str) -> bool:
    value = clean_text(value)
    candidate = normalize_name_candidate(value)
    lower = candidate.lower()
    if not candidate or len(candidate) > 90:
        return False
    if candidate[0].islower():
        return False
    if re.search(r"\d{3}[-.\s]\d{3}[-.\s]\d{4}", candidate) or re.search(r"\d", candidate):
        return False
    if re.search(r"\S+@\S+\.\S+", candidate) or lower in GENERIC_NAME_LABELS:
        return False
    if any(term in lower for term in GENERIC_NAME_TERMS):
        return False
    if re.search(r"\b(professor|lecturer|reader|principal investigator|lab director|group leader|programme leader|program leader|research fellow|dean|head of|director)\b", lower):
        return False
    return bool(
        re.search(r"[A-Za-z][A-Za-z'.-]+ [A-Za-z]", candidate)
        or re.search(r"\b[A-Z]\.?(?:\s+[A-Z]\.?)+\s+[A-Za-z][A-Za-z'.-]+", candidate)
        or re.search(r"[A-Za-z]+,\s+[A-Za-z]", candidate)
    )


def extract_title(text: str, fallback_patterns: list[str] | None = None) -> str | None:
    patterns = fallback_patterns or []
    for pattern in sorted(patterns, key=len, reverse=True):
        match = re.search(rf"\b{re.escape(pattern)}\b", text, flags=re.I)
        if match:
            return clean_text(match.group(0))
    match = TITLE_RE.search(text)
    return clean_text(match.group(0)) if match else None


def extract_profile_title_near_name(
    soup: BeautifulSoup,
    name: str,
    positive_title_patterns: list[str] | None = None,
) -> str | None:
    lines = [clean_text(line) for line in soup.get_text("\n", strip=True).splitlines() if clean_text(line)]
    stop_labels = {
        "google scholar",
        "orcid",
        "researchgate",
        "research gate",
        "scopus",
        "web of science",
        "sdgs focus",
        "biography",
        "academic & professional qualifications",
        "academic and professional qualifications",
    }
    target = normalize_name_candidate(name).lower()
    for index, line in enumerate(lines):
        if normalize_name_candidate(line).lower() != target:
            continue
        for item in lines[index + 1 : index + 12]:
            lower = item.lower()
            if lower in stop_labels:
                break
            if likely_name(item) or "@" in item or len(item) > 240:
                continue
            if any(term in lower for term in ["school of", "faculty of", "department of"]):
                continue
            if TITLE_RE.search(item) or any(pattern.lower() in lower for pattern in (positive_title_patterns or [])):
                return item
        break
    return None


def _emails_from_profile_blocks(soup: BeautifulSoup, name: str) -> tuple[list[str], list[str], str]:
    page_person_emails, page_ambiguous = split_person_and_ambiguous_emails(extract_emails_from_html(str(soup)))
    if not page_person_emails:
        return [], page_ambiguous, "none"
    if len(page_person_emails) == 1:
        return page_person_emails, page_ambiguous, "person_local"

    scoped: set[str] = set()
    for link in soup.select("a[href^=mailto], a[href*='/cdn-cgi/l/email-protection']"):
        for parent in link.parents:
            if not isinstance(parent, Tag):
                continue
            text = clean_text(parent.get_text(" ", strip=True))
            classes = " ".join(parent.get("class") or []).lower()
            if len(text) > 1200:
                break
            if name.lower() in text.lower() or any(term in classes for term in ["contact", "profile", "person", "email"]):
                person_emails, _ambiguous = split_person_and_ambiguous_emails(extract_emails_from_html(str(parent)))
                scoped.update(person_emails)
                break
    if scoped and len(scoped) <= 2:
        ambiguous = sorted(set(page_person_emails) - scoped).copy()
        ambiguous.extend(page_ambiguous)
        return sorted(scoped), sorted(set(ambiguous)), "person_local"
    return [], sorted(set(page_person_emails + page_ambiguous)), "ambiguous_email"


def parse_profile_page(
    html_text_value: str,
    source_url: str,
    positive_title_patterns: list[str] | None = None,
) -> ParsedPerson | None:
    soup = BeautifulSoup(html_text_value or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title_text = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    candidates: list[tuple[int, str]] = []
    for selector in ["h1", "h2", ".name", ".person-name", "[itemprop=name]"]:
        for node in soup.select(selector):
            raw = clean_text(node.get_text(" ", strip=True))
            if not likely_name(raw):
                continue
            candidate = normalize_name_candidate(raw)
            score = 1
            if raw != candidate:
                score += 3
            if candidate and candidate.lower() in title_text.lower():
                score += 2
            candidates.append((score, candidate))
    if soup.title:
        title_bits = re.split(r"[\-|,]", title_text)
        if title_bits and likely_name(title_bits[0]):
            candidates.append((3, normalize_name_candidate(title_bits[0])))
    if not candidates:
        return None
    name = max(candidates, key=lambda item: item[0])[1]
    page_text = clean_text(soup.get_text(" ", strip=True))
    emails, ambiguous_emails, email_association = _emails_from_profile_blocks(soup, name)
    profile_url = source_url
    lab_url = None
    for link in soup.find_all("a", href=True):
        text = clean_text(link.get_text(" ", strip=True)).lower()
        href = urljoin(source_url, link["href"])
        if any(term in text for term in ["lab", "group", "personal page", "homepage"]):
            lab_url = href
            break
    return ParsedPerson(
        name=name,
        title=extract_profile_title_near_name(soup, name, positive_title_patterns) or extract_title(page_text, positive_title_patterns),
        department=None,
        profile_url=profile_url,
        lab_url=lab_url,
        emails=emails,
        ambiguous_emails=ambiguous_emails,
        research_areas=research_areas_from_profile(soup),
        external_ids=external_ids_from_links(soup, source_url),
        source_url=source_url,
        source_type="official_profile",
        extraction_method="generic_html_profile",
        evidence_text=page_text[:1000],
        confidence=0.65 if emails else 0.5,
        email_association=email_association,
    )
