from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from ..models import ParsedPerson
from .mailto import extract_emails_from_html, split_person_and_ambiguous_emails
from .publications import extract_publication_fingerprints


TITLE_RE = re.compile(
    r"\b(Distinguished Research Professor|Distinguished Professor|Research Assistant Professor|Research Associate Professor|Research Professor|Clinical Professor|Chair Professor|Honorary Professor|Emeritus Professor|Adjunct Associate Professor|Visiting Associate Professor|Associate Professor|Assistant Professor|Senior Lecturer|Professor of Teaching|Principal Investigator|Group Leader|Lab Director|Deputy Dean|Dean|Department Chairperson|Programme Leader|Program Leader|Research Fellow|Professor|Reader|Lecturer)\b",
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
    r"CCC-[A-Za-z-]+)\s*\.?$",
    re.I,
)

# Fellowship abbreviations such as ``FIEEE`` are occasionally appended to a
# name, but an unconstrained ``F[A-Z]+`` suffix also matches common all-caps
# surnames such as ``FUNG``.  Require the punctuation used for a credential so
# a person's surname is never discarded merely because it begins with F.
ACADEMIC_FELLOWSHIP_SUFFIX_RE = re.compile(r",\s*(?-i:F[A-Z]{2,})\s*\.?$")

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
    "other academic affiliations",
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
    "university of",
    "city university",
    "editorial board",
    "serves or served",
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
        candidate = ACADEMIC_FELLOWSHIP_SUFFIX_RE.sub("", candidate).strip(" .,-|:")
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
        elif "openalex.org/a" in lower:
            match = re.search(r"openalex\.org/(A\d+)", href, flags=re.I)
            if match:
                author_id = match.group(1).upper()
                ids.setdefault("openalex_author_id", author_id)
                ids.setdefault("openalex_url", f"https://openalex.org/{author_id}")
    return ids


RESEARCH_SECTION_LABELS = {
    "area of expertise",
    "areas of expertise",
    "expertise",
    "research area",
    "research areas",
    "research interest",
    "research interests",
}

RESEARCH_SECTION_STOP_LABELS = {
    "academic & professional qualifications",
    "academic and professional qualifications",
    "awards",
    "biography",
    "courses taught",
    "notable publications",
    "professional qualifications",
    "publications",
    "selected publications",
    "sdgs focus",
    "teaching areas",
    "what are you looking for?",
}


def _section_label(value: str) -> str:
    return clean_text(value).strip(" :").lower()


def _inside_navigation(node: Tag) -> bool:
    """Reject site chrome whose links merely enumerate broad research areas."""

    for depth, current in enumerate([node, *node.parents]):
        if not isinstance(current, Tag):
            continue
        if current.name in {"nav", "header", "footer", "aside"}:
            return True
        # Menu markers on a distant ``body`` describe the site's enabled
        # navigation system, not the current content block.  Only use class/id
        # markers on the local ancestry; semantic navigation tags remain
        # authoritative at any depth.
        if depth > 8:
            continue
        marker = " ".join(
            [
                clean_text(str(current.get("id") or "")),
                *(clean_text(str(value)) for value in (current.get("class") or [])),
            ]
        ).lower()
        if re.search(r"(?:^|[-_\s])(?:breadcrumb|footer|main-menu|menu|navbar|navigation)(?:$|[-_\s])", marker):
            return True
    return False


def _is_explicit_research_container(node: Tag) -> bool:
    marker = " ".join(
        [
            clean_text(str(node.get("id") or "")),
            *(clean_text(str(value)) for value in (node.get("class") or [])),
        ]
    ).lower()
    compact = re.sub(r"[^a-z]+", "-", marker).strip("-")
    return any(
        token in compact
        for token in (
            "area-of-expertise",
            "areas-of-expertise",
            "expertise",
            "research-area",
            "research-areas",
            "research-interest",
            "research-interests",
            "researcharea",
            "researchinterest",
        )
    )


def _is_structural_heading(node: Tag) -> bool:
    if node.name in {"h1", "h2", "h3", "h4", "h5", "h6", "dt"} or node.get("role") == "heading":
        return True
    marker = " ".join(clean_text(str(value)) for value in (node.get("class") or [])).lower()
    return bool(re.search(r"(?:^|[-_\s])(?:h[1-6]|heading)(?:$|[-_\s])", marker))


def _research_texts(node: Tag | NavigableString) -> list[str]:
    if isinstance(node, NavigableString):
        value = clean_text(str(node))
        return [value] if value else []
    list_items = [clean_text(item.get_text(" ", strip=True)) for item in node.find_all("li")]
    if list_items:
        return [value for value in list_items if value]
    paragraphs = [clean_text(item.get_text(" ", strip=True)) for item in node.find_all("p")]
    if paragraphs:
        return [value for value in paragraphs if value]
    return [clean_text(value) for value in node.get_text("\n", strip=True).splitlines() if clean_text(value)]


def research_areas_from_profile(soup: BeautifulSoup) -> list[str]:
    """Extract only structurally identified, person-local research sections.

    A site-wide link to ``/research-areas/...`` is not evidence that every
    person works in that area.  Consequently this parser deliberately avoids
    whole-page text/link scans: it accepts either an explicit research section
    container or the siblings immediately following a research heading.
    """

    areas: list[str] = []

    def add_values(values: list[str]) -> bool:
        """Append valid values and report whether a following section began."""

        for value in values:
            value = clean_text(value)
            label = _section_label(value)
            if label in RESEARCH_SECTION_STOP_LABELS:
                return True
            if not value or label in RESEARCH_SECTION_LABELS:
                continue
            if len(value) > 120 or "@" in value or value.lower().startswith(("http://", "https://")):
                continue
            if value not in areas:
                areas.append(value)
            if len(areas) >= 20:
                return True
        return False

    headings: list[Tag] = []
    for heading in soup.find_all(True):
        if (
            _is_structural_heading(heading)
            and _section_label(heading.get_text(" ", strip=True)) in RESEARCH_SECTION_LABELS
            and not _inside_navigation(heading)
        ):
            headings.append(heading)

    for heading in headings:
        content_blocks = 0
        visited: set[int] = set()
        for element in heading.next_elements:
            if not isinstance(element, Tag) or heading in element.parents:
                continue
            if _is_structural_heading(element):
                break
            if _inside_navigation(element):
                continue
            if element.name not in {"li", "p"}:
                continue
            # A paragraph can contain links/spans, so process the block once
            # rather than treating each descendant as another research area.
            if id(element) in visited:
                continue
            visited.add(id(element))
            content_blocks += 1
            if add_values([clean_text(element.get_text(" ", strip=True))]) or content_blocks >= 50:
                break
        if areas:
            return areas[:20]

    for container in soup.find_all(True):
        if (
            _is_structural_heading(container)
            or not _is_explicit_research_container(container)
            or _inside_navigation(container)
        ):
            continue
        add_values(_research_texts(container))
        if areas:
            return areas[:20]
    return []


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
    if re.search(r"\b(?:school|faculty|department|institute|laboratory|centre|center)$", lower):
        return False
    if re.match(r"^(?:bachelor|master|doctor)\s+of\b", lower):
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
    appointment_title_patterns: list[str] | None = None,
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

    def matches_identity(value: str) -> bool:
        line_identity = normalize_name_candidate(value).lower()
        exact_identity = line_identity == target
        abbreviated_identity = bool(
            line_identity
            and len(line_identity) >= 3
            and len(line_identity.split()) == 1
            and (
                target.startswith(line_identity + " ")
                or target.endswith(" " + line_identity)
            )
        )
        return exact_identity or abbreviated_identity

    for index, line in enumerate(lines):
        if not matches_identity(line):
            continue
        for item in lines[index + 1 : index + 12]:
            lower = item.lower()
            if lower in stop_labels:
                break
            if likely_name(item) or "@" in item or len(item) > 240:
                continue
            if any(term in lower for term in ["school of", "faculty of", "department of"]):
                continue
            if TITLE_RE.search(item) or any(pattern.lower() in lower for pattern in (appointment_title_patterns or [])):
                return item
        break

    # Some official Sitecore profiles place the appointment in the heading
    # immediately before the person's name (for example ``Research Assistant
    # Professor`` followed by ``Dr Andy FUNG``).  Accept only that tight DOM
    # relationship and reject navigation/header/footer ancestry; never scan
    # arbitrary preceding page text.
    for name_node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if _inside_navigation(name_node) or not matches_identity(
            clean_text(name_node.get_text(" ", strip=True))
        ):
            continue
        previous = name_node.find_previous_sibling()
        if not isinstance(previous, Tag) or _inside_navigation(previous):
            continue
        if not _is_structural_heading(previous):
            continue
        item = clean_text(previous.get_text(" ", strip=True))
        lower = item.lower()
        if (
            item
            and len(item) <= 240
            and (
                TITLE_RE.search(item)
                or any(
                    pattern.lower() in lower
                    for pattern in (appointment_title_patterns or [])
                )
            )
        ):
            return item
    return None


def _emails_from_profile_blocks(soup: BeautifulSoup, name: str) -> tuple[list[str], list[str], str]:
    page_person_emails, page_ambiguous = split_person_and_ambiguous_emails(extract_emails_from_html(str(soup)))
    if not page_person_emails:
        return [], page_ambiguous, "none"
    if len(page_person_emails) == 1:
        email = page_person_emails[0]
        page_text = clean_text(soup.get_text(" ", strip=True)).lower()
        position = page_text.find(email.lower())
        preceding_context = page_text[max(0, position - 180) : position] if position >= 0 else ""
        if any(
            label in preceding_context
            for label in (
                "general enquiries",
                "general inquiries",
                "department enquiries",
                "department inquiries",
                "faculty office",
                "school office",
                "contact us",
            )
        ):
            return [], sorted(set([email, *page_ambiguous])), "ambiguous_email"
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
    appointment_title_patterns: list[str] | None = None,
    *,
    extra_name_candidates: list[str] | None = None,
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
    for raw in extra_name_candidates or []:
        candidate = normalize_name_candidate(raw)
        # These candidates come from a source-specific person-name field, so
        # retain Unicode names that the conservative generic ASCII heuristic
        # cannot recognize on its own.
        if candidate and len(candidate) <= 90 and "@" not in candidate and not re.search(r"\d", candidate):
            candidates.append((12, candidate))
    # A few legacy researcher systems expose an incomplete display heading
    # (for example, just "Alex") while the first-person biography contains
    # the full name.  Prefer that explicit self-identification over inventing
    # a surname or retaining a one-token record.
    biography_name_pattern = re.compile(
        r"(?<!Assistant )(?<!Associate )(?<!Visiting )(?<!Honorary )"
        r"(?<!Emeritus )(?<!Adjunct )(?<!Research )(?<!Clinical )"
        r"\b(?:Prof(?:essor)?|Dr)\.?\s+"
        r"([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){1,4})\s+"
        r"(?:is|was|joined)\b"
    )
    biography_match = biography_name_pattern.search(clean_text(soup.get_text(" ", strip=True)))
    if biography_match and likely_name(biography_match.group(1)):
        candidates.append((10, normalize_name_candidate(biography_match.group(1))))
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
        # A whole-page title scan is unsafe on university profile templates:
        # navigation labels such as ``Dean`` commonly appear before the actual
        # person content.  Accept only a role structurally adjacent to the
        # identified person name; the official directory can supply a title
        # when a profile does not expose one locally.
        title=extract_profile_title_near_name(soup, name, appointment_title_patterns),
        department=None,
        profile_url=profile_url,
        lab_url=lab_url,
        emails=emails,
        ambiguous_emails=ambiguous_emails,
        research_areas=research_areas_from_profile(soup),
        external_ids=external_ids_from_links(soup, source_url),
        publication_fingerprints=extract_publication_fingerprints(html_text_value, source_url),
        source_url=source_url,
        source_type="official_profile",
        extraction_method="generic_html_profile",
        evidence_text=page_text[:1000],
        confidence=0.65 if emails else 0.5,
        email_association=email_association,
    )
