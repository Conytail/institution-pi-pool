from __future__ import annotations

import json
import re
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from ..models import ParsedPerson
from .faculty_directory import parse_faculty_directory
from .generic_html import (
    clean_text,
    external_ids_from_links,
    likely_name,
    parse_profile_page,
)
from .mailto import (
    extract_conflicting_mailto_emails,
    extract_emails_from_html,
    split_person_and_ambiguous_emails,
)


CARD_SELECTOR = ".scholar-result .result"
FEDERATED_CARD_SELECTORS = (
    ".faculty-list.people-list .views-row",
    ".staff-card",
    ".card.g2",
    ".card.ms-staff",
    ".faculty-item .card",
    ".person.person-listing",
    ".cuee-people-fields-container",
    ".people-info-card-item",
    ".views-view-responsive-grid__item-inner",
    ".faculty-card",
    ".faculty-member",
    ".person-card",
    "table.table tr",
)
UNIT_PATTERN = re.compile(
    r"\b(?:department|school|college|centre|center|institute|academy|division|faculty)\b",
    flags=re.I,
)
ACADEMIC_ROLE_RE = re.compile(
    r"\b(?:professor|lecturer|reader|principal investigator|group leader|lab director|"
    r"research (?:assistant|associate|fellow|officer)|post[- ]?doctoral|academic staff)\b",
    flags=re.I,
)


def _looks_like_collective_directory_heading(value: str) -> bool:
    """Reject navigation labels that enumerate categories, not a person.

    Requiring multiple collective role nouns plus a list separator avoids
    turning any single appointment (for example, ``Visiting Professor``) into
    an identity exclusion rule.
    """

    lower = clean_text(value).lower()
    groups = re.findall(
        r"\b(?:faculty|staff|professors?|lecturers?|fellows?|members?)\b",
        lower,
    )
    return len(groups) >= 2 and bool(re.search(r"(?:,|&|/|\band\b)", lower))


def _matches_any(value: str | None, patterns: list[str]) -> bool:
    lower = (value or "").lower()
    return any(pattern.lower() in lower for pattern in patterns if pattern)


def _clean_name(value: str) -> str:
    value = clean_text(value)
    # Legacy EE homepages often put the person name in a decorative page
    # heading instead of a semantic field.  Keep the name, not the chrome.
    value = re.sub(
        r"^Welcome\s+to\s+(.+?)(?:['\u2019]s)\s+Home\s*Page$",
        r"\1",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"^(.+?)(?:['\u2019]s)\s+Home\s*Page$",
        r"\1",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"^(?:(?:Affiliate|Adjunct|Clinical|Emeritus|Honorary|Research|"
        r"Teaching|Visiting)\s+)*(?:Professor|Prof\.?|Doctor|Dr\.?|Mr\.?|"
        r"Ms\.?|Miss|Mrs\.?)\s+",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"\s*[（(][\u3400-\u9fff\s]+[）)]\s*$", "", value)
    value = re.sub(r"\s+[\u3400-\u9fff]+(?:\u6559\u6388|\u535a\u58eb|\u5148\u751f|\u5973\u58eb)?$", "", value)
    return clean_text(value)


def _valid_name(value: str) -> bool:
    lower = clean_text(value).lower()
    if _looks_like_collective_directory_heading(value):
        return False
    if lower in {
        "academic staff",
        "academic rankings",
        "adjunct/visiting professors",
        "adjunct professors",
        "associate head",
        "cityuhk scholars",
        "clerical officer",
        "current/recent research projects",
        "departmental advisory committee",
        "distinguished visiting professors",
        "faculty members",
        "learn more",
        "link to profile",
        "our people",
        "page not found",
        "research assistant",
        "research professors",
        "research staff",
        "orcid id",
        "scopus author id",
        "google scholar",
        "researchgate",
        "teaching staff",
    }:
        return False
    if lower.startswith(("home department:", "international relations discipline:")):
        return False
    if any(
        term in lower
        for term in (
            "university of",
            "phd",
            "ph.d",
            "dphil",
            "about us",
            "faculty and staff",
            "advisory committee",
        )
    ):
        return False
    if re.search(
        r"\b(?:appears for a little time and then vanishes|"
        r"congratulations\b.*\b(?:patent|award|grant)|"
        r"research team on patent grant)\b",
        lower,
    ):
        return False
    return likely_name(value)


def _split_position(value: str) -> tuple[str | None, str | None]:
    parts = [clean_text(part) for part in value.split(",") if clean_text(part)]
    units = list(dict.fromkeys(part for part in parts if UNIT_PATTERN.search(part)))
    roles = list(dict.fromkeys(part for part in parts if not UNIT_PATTERN.search(part)))
    return ("; ".join(roles) or value or None, "; ".join(units) or None)


def parse_cityu_academic_directory(html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    people: list[ParsedPerson] = []

    for card in soup.select(CARD_SELECTOR):
        name_node = card.select_one(".result-name.en") or card.select_one(".result-name")
        position_node = card.select_one(".result-category.position")
        raw_name = clean_text(name_node.get_text(" ", strip=True) if name_node else "")
        raw_position = clean_text(position_node.get_text(" ", strip=True) if position_node else "")
        name = _clean_name(raw_name)
        if not name or not _valid_name(name):
            continue
        title, department = _split_position(raw_position)
        profile_link = card.select_one('a[title="View Profile"][href]')
        profile_url = urljoin(source_url, profile_link.get("href") or "") if profile_link else None
        emails, ambiguous_emails = split_person_and_ambiguous_emails(extract_emails_from_html(str(card)))
        evidence_text = clean_text(card.get_text(" ", strip=True))
        people.append(
            ParsedPerson(
                name=name,
                title=title,
                department=department,
                profile_url=profile_url,
                emails=emails,
                ambiguous_emails=ambiguous_emails,
                source_url=source_url,
                source_type="official_directory",
                extraction_method="cityu_sitecore_academic_directory",
                evidence_text=evidence_text[:1000],
                confidence=0.98 if title and department and emails else 0.9,
                email_association="person_local" if emails else "none",
            )
        )
    return people


def cityu_academic_candidate_count(html_text: str) -> int:
    return len(BeautifulSoup(html_text or "", "html.parser").select(CARD_SELECTOR))


def _source_unit_config(config: dict, source_url: str) -> dict | None:
    parsed_source = urlparse(source_url)
    source_host = parsed_source.netloc.lower().removeprefix("www.")
    source_path = parsed_source.path.rstrip("/").lower()
    best: tuple[int, dict] | None = None
    for unit in (config.get("pool_scope") or {}).get("units") or []:
        match_urls = [*(unit.get("seed_urls") or []), *(unit.get("match_urls") or [])]
        for seed in match_urls:
            parsed_seed = urlparse(str(seed))
            seed_host = parsed_seed.netloc.lower().removeprefix("www.")
            seed_path = parsed_seed.path.rstrip("/").lower()
            if source_host != seed_host:
                continue
            if source_path == seed_path or source_path.startswith(seed_path + "/"):
                candidate = (len(seed_path), unit)
                if unit.get("name") and (best is None or candidate[0] > best[0]):
                    best = candidate
    return best[1] if best else None


def _source_unit(config: dict, source_url: str) -> str | None:
    unit = _source_unit_config(config, source_url)
    return clean_text(str(unit.get("name") or "")) if unit else None


def _role_lines(card) -> list[str]:
    patterns = [
        pattern.lower()
        for pattern in [
            "chair professor",
            "clinical professor",
            "research professor",
            "research assistant professor",
            "associate professor",
            "assistant professor",
            "professor",
            "senior lecturer",
            "lecturer",
            "reader",
            "principal investigator",
            "group leader",
            "lab director",
            "research fellow",
            "research officer",
            "postdoctoral",
            "post-doctoral",
            "academic staff",
        ]
        if pattern
    ]

    explicit_lines: list[str] = []
    for node in card.select(
        ".person__title, .staff-title, .faculty-position, .job-title, "
        ".people-chair, .people-position, .field--name-field-job-title, "
        ".position, .title"
    ):
        line = clean_text(node.get_text(" ", strip=True))
        lower = line.lower()
        is_section_label = bool(
            re.fullmatch(
                r"(?:research (?:interests?|areas?)|biography|publications?|projects?)\s*:?",
                lower,
            )
        )
        if line and len(line) <= 260 and not is_section_label:
            explicit_lines.append(line)
    if explicit_lines:
        return list(dict.fromkeys(explicit_lines))

    for node in card.select("p[align='center'], td, h3, h6"):
        line = clean_text(node.get_text(" ", strip=True))
        lower = line.lower()
        if line and len(line) <= 260 and any(pattern in lower for pattern in patterns):
            explicit_lines.append(line)
    if explicit_lines:
        return list(dict.fromkeys(explicit_lines))

    lines: list[str] = []
    for raw in card.get_text("\n", strip=True).splitlines()[:40]:
        line = clean_text(raw)
        lower = line.lower()
        if not line or "@" in line or len(line) > 160:
            continue
        if any(pattern in lower for pattern in patterns):
            lines.append(line)
    return list(dict.fromkeys(lines))


def _profile_link(card, source_url: str, name: str) -> str | None:
    candidates: list[tuple[int, str]] = []
    name_tokens = [token.lower().strip(",") for token in name.split() if len(token) > 2]
    for link in card.select("a[href]"):
        href = clean_text(link.get("href") or "")
        if not href or href.startswith(("#", "mailt:", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(source_url, href)
        parsed_absolute = urlparse(absolute)
        host = (parsed_absolute.hostname or "").casefold().removeprefix("www.")
        if any(
            host == candidate or host.endswith(f".{candidate}")
            for candidate in (
                "orcid.org",
                "researchgate.net",
                "scholar.google.com",
                "scopus.com",
            )
        ):
            continue
        if unquote(parsed_absolute.path or "").casefold().rstrip("/").endswith(
            "/error/404"
        ):
            continue
        lower = absolute.lower()
        if lower.endswith((".jpg", ".jpeg", ".png", ".gif", ".svg", ".pdf")):
            continue
        text = clean_text(link.get_text(" ", strip=True)).lower()
        score = 0
        if any(token in text for token in name_tokens):
            score += 4
        if any(token in lower for token in name_tokens):
            score += 3
        if any(token in lower for token in ("/people/", "/person/", "/persons/", "/profile", "detail?", "staff/")):
            score += 2
        if text in {"profile", "view profile", "link to profile", "details", "detail"}:
            score += 2
        candidates.append((score, absolute))
    if not candidates:
        eid_link = card.select_one("a[data-eid]")
        eid = clean_text(eid_link.get("data-eid") or "") if eid_link else ""
        if eid and re.fullmatch(r"[A-Za-z0-9._-]+", eid):
            return f"https://www.cb.cityu.edu.hk/staff/{eid}/"
        return None
    candidates.sort(key=lambda item: (-item[0], len(item[1])))
    return candidates[0][1] if candidates[0][0] > 0 else None


def _name_from_card(card) -> str | None:
    selectors = (
        ".result-name.en",
        ".staff-name",
        ".person-name",
        ".person__name",
        ".faculty-name",
        ".people-name",
        ".name",
        ".card-title",
        ".t1",
        "td:nth-of-type(2) a",
        "h2",
        "h3",
        "h4",
        "h5",
    )
    for selector in selectors:
        for node in card.select(selector):
            value = _clean_name(node.get_text(" ", strip=True))
            if _valid_name(value):
                return value
    for link in card.select("a[href]"):
        value = _clean_name(link.get_text(" ", strip=True))
        if _valid_name(value):
            return value
    return None


def _research_areas_from_labeled_table(card) -> list[str]:
    """Read person-local research text from CityU's labelled detail tables.

    The English Department's people lists repeat the same table in mobile and
    desktop markup.  Reading within one ``views-row`` and de-duplicating exact
    values preserves the official research statement without leaking data
    from an adjacent person card.
    """

    areas: list[str] = []
    for row in card.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) < 2:
            continue
        label = clean_text(cells[0].get_text(" ", strip=True)).strip(" :").lower()
        if label not in {"research interest", "research interests", "research area", "research areas"}:
            continue
        value = clean_text(" ".join(cell.get_text(" ", strip=True) for cell in cells[1:]))
        if value and value not in areas:
            areas.append(value)
    return areas[:20]


def _research_areas_from_affiliated_modal(modal) -> list[str]:
    """Extract the expertise block without absorbing its profile link.

    CityU Physics places each person's expertise and contact fields in a
    Bootstrap modal adjacent to the visible card.  The profile anchor is a
    paragraph inside the same body field, so whole-field text extraction would
    incorrectly turn ``Link to profile`` into a research area.
    """

    areas: list[str] = []
    labels = {"expertise", "research interest", "research interests", "research areas"}
    for heading in modal.select("h1, h2, h3, h4, h5, h6"):
        if clean_text(heading.get_text(" ", strip=True)).strip(" :").lower() not in labels:
            continue
        for sibling in heading.next_siblings:
            if not getattr(sibling, "name", None):
                continue
            if sibling.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                break
            classes = {clean_text(str(value)).lower() for value in (sibling.get("class") or [])}
            if "field--name-field-email" in classes:
                break

            list_items = [
                clean_text(item.get_text(" ", strip=True))
                for item in sibling.select("li")
            ]
            values = [value for value in list_items if value]
            if not values:
                values = [
                    clean_text(item.get_text(" ", strip=True))
                    for item in sibling.select("p")
                    if not item.select_one("a[href]")
                ]
            for value in values:
                if value and value not in areas:
                    areas.append(value)
            if areas:
                return areas[:20]
    return _research_areas_from_labeled_table(modal)


def _parse_affiliated_modal_cards(
    soup: BeautifulSoup,
    source_url: str,
    config: dict,
) -> list[ParsedPerson]:
    """Join CityU affiliated-faculty cards to their person-local modals.

    The visible card and modal are siblings rather than one DOM subtree.  The
    card anchor's Bootstrap target is the page's explicit join key; using that
    key prevents a modal email or expertise list from crossing into either a
    neighbouring person or a standalone ``Link to profile`` pseudo-record.
    """

    people: list[ParsedPerson] = []
    for article in soup.select("article.affiliated-faculty.card"):
        anchor = article.find_parent("a")
        target = clean_text(
            (anchor.get("data-bs-target") or anchor.get("data-target") or "")
            if anchor is not None
            else ""
        )
        if not re.fullmatch(r"#[A-Za-z0-9_.:-]+", target):
            continue
        modal = soup.find(id=target[1:])
        if modal is None or "affiliated-faculty-modal" not in (modal.get("class") or []):
            continue

        name = _name_from_card(article)
        if not name:
            continue
        title = "; ".join(_role_lines(article)[:6]) or None
        emails, ambiguous_emails = split_person_and_ambiguous_emails(
            extract_emails_from_html(str(modal))
        )
        evidence = clean_text(
            f"{article.get_text(' ', strip=True)} {modal.get_text(' ', strip=True)}"
        )
        people.append(
            ParsedPerson(
                name=name,
                title=title,
                department=_source_unit(config, source_url),
                profile_url=_profile_link(modal, source_url, name),
                emails=emails,
                ambiguous_emails=ambiguous_emails,
                research_areas=_research_areas_from_affiliated_modal(modal),
                external_ids=external_ids_from_links(modal, source_url),
                source_url=source_url,
                source_type="official_directory",
                extraction_method="cityu_affiliated_modal_card",
                evidence_text=evidence[:1000],
                confidence=0.96 if emails else 0.88,
                email_association="person_local" if emails else "none",
            )
        )
    return people


def _person_from_card(card, source_url: str, config: dict) -> ParsedPerson | None:
    name = _name_from_card(card)
    if not name:
        return None
    role_lines = _role_lines(card)
    evidence = clean_text(card.get_text(" ", strip=True))
    title = "; ".join(role_lines[:6]) or None
    emails, ambiguous_emails = split_person_and_ambiguous_emails(extract_emails_from_html(str(card)))
    return ParsedPerson(
        name=name,
        title=title,
        department=_source_unit(config, source_url),
        profile_url=_profile_link(card, source_url, name),
        emails=emails,
        ambiguous_emails=ambiguous_emails,
        research_areas=_research_areas_from_labeled_table(card),
        external_ids=external_ids_from_links(card, source_url),
        source_url=source_url,
        source_type="official_directory",
        extraction_method="cityu_federated_card",
        evidence_text=evidence[:1000],
        confidence=0.95 if emails else 0.84,
        email_association="person_local" if emails else "none",
    )


def _profile_url_key(value: str | None) -> str:
    """Return a stable key for two links to the same CityU person page."""

    return clean_text(value or "").rstrip("/").lower()


def _punctuation_insensitive_name_key(value: str) -> str:
    """Collapse harmless display punctuation for structured/generic de-dupe."""

    return re.sub(r"[\W_]+", "", clean_text(value).casefold(), flags=re.UNICODE)


def _directory_row_enrichments(soup: BeautifulSoup, source_url: str) -> dict[str, dict]:
    """Read CityU BMS/Neuro's per-person alpha-list rows.

    Those pages render each person twice: an appointment card contains the
    title, while a later ``row g-0`` block contains the email and research
    interests.  Treating the latter as an independent generic card either
    loses the title or risks associating a neighbouring row's email.  The
    shared profile URL is the page's explicit, deterministic join key.
    """

    enrichments: dict[str, dict] = {}
    for profile_link in soup.select("a.block-faculty-name[href]"):
        href = clean_text(profile_link.get("href") or "")
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        row = next(
            (
                parent
                for parent in profile_link.parents
                if parent.name == "div"
                and {"row", "g-0"}.issubset(set(parent.get("class") or []))
            ),
            None,
        )
        if row is None:
            continue

        profile_url = urljoin(source_url, href)
        key = _profile_url_key(profile_url)
        if not key:
            continue

        emails, ambiguous_emails = split_person_and_ambiguous_emails(
            extract_emails_from_html(str(row))
        )
        research_node = row.select_one(".researchinterest")
        research_text = clean_text(
            research_node.get_text(" ", strip=True) if research_node else ""
        )
        research_areas = [
            item
            for item in (
                clean_text(part)
                for part in re.split(r"\s*(?:•|;|\||·)\s*", research_text)
            )
            if item and item.lower().strip("()") not in {"to be confirmed", "tbc"}
        ]
        enrichments[key] = {
            "emails": emails,
            "ambiguous_emails": ambiguous_emails,
            "research_areas": list(dict.fromkeys(research_areas))[:20],
            "evidence_text": clean_text(row.get_text(" ", strip=True))[:1000],
        }
    return enrichments


def _enrich_people_from_directory_rows(
    people: list[ParsedPerson], soup: BeautifulSoup, source_url: str
) -> None:
    enrichments = _directory_row_enrichments(soup, source_url)
    for person in people:
        row = enrichments.get(_profile_url_key(person.profile_url))
        if row is None:
            continue
        person.emails = list(dict.fromkeys([*person.emails, *row["emails"]]))
        person.ambiguous_emails = list(
            dict.fromkeys([*person.ambiguous_emails, *row["ambiguous_emails"]])
        )
        person.research_areas = list(
            dict.fromkeys([*person.research_areas, *row["research_areas"]])
        )[:20]
        if person.emails:
            person.email_association = "person_local"
            person.confidence = max(person.confidence, 0.95)
        row_evidence = row["evidence_text"]
        if row_evidence and row_evidence not in person.evidence_text:
            person.evidence_text = clean_text(
                f"{person.evidence_text} {row_evidence}"
            )[:1000]


def _parse_mgt_api(payload: dict, source_url: str, config: dict) -> list[ParsedPerson]:
    rows: list[dict] = []
    for group in payload.get("Staffs") or []:
        if isinstance(group, dict) and isinstance(group.get("data"), list):
            rows.extend(row for row in group["data"] if isinstance(row, dict))
    people: list[ParsedPerson] = []
    for row in rows:
        def field(name: str) -> str:
            value = row.get(name)
            if isinstance(value, dict):
                value = value.get("data")
            return clean_text(str(value or ""))

        name = _clean_name(field("staffName"))
        title = field("staffTitle")
        if not name or not _valid_name(name):
            continue
        eid = field("eid")
        research = field("research_area")
        research_areas = [clean_text(item) for item in re.split(r"[,;|]", research) if clean_text(item)]
        profile_url = (
            urljoin(source_url, f"/mgt/about-us/faculty-staff/detail?eid={eid}") if eid else None
        )
        people.append(
            ParsedPerson(
                name=name,
                title=title or None,
                department=_source_unit(config, source_url) or "Department of Management",
                profile_url=profile_url,
                research_areas=list(dict.fromkeys(research_areas))[:20],
                source_url=source_url,
                source_type="official_api",
                extraction_method="cityu_mgt_people_api",
                evidence_text=clean_text(f"{name} {title} {research}")[:1000],
                confidence=0.92 if profile_url else 0.82,
            )
        )
    return people


def _parse_profile_api(
    payload: dict,
    source_url: str,
    config: dict,
    *,
    via_reader: bool = False,
) -> list[ParsedPerson]:
    people: list[ParsedPerson] = []
    for row in payload.get("profiles") or []:
        if not isinstance(row, dict):
            continue
        name = _clean_name(str(row.get("profile_name") or ""))
        posts = row.get("profile_post") or []
        if isinstance(posts, str):
            posts = [posts]
        title = "; ".join(clean_text(str(item)) for item in posts if clean_text(str(item)))
        if not name or not _valid_name(name):
            continue
        email = clean_text(str(row.get("profile_Email") or "")).lower()
        emails = [email] if email and "@" in email else []
        sites = row.get("profile_Site") or []
        if isinstance(sites, str):
            sites = [sites]
        profile_url = next(
            (urljoin(source_url, clean_text(str(site))) for site in sites if clean_text(str(site))),
            None,
        )
        research = clean_text(str(row.get("profile_Research_Interes") or ""))
        research_areas = [
            clean_text(item)
            for item in re.split(r"[,;|]", research)
            if clean_text(item)
        ]
        evidence = clean_text(f"{name} {title} {email} {research}")
        people.append(
            ParsedPerson(
                name=name,
                title=title or None,
                department=_source_unit(config, source_url),
                profile_url=profile_url,
                emails=emails,
                research_areas=list(dict.fromkeys(research_areas))[:20],
                source_url=source_url,
                source_type="official_api_via_reader" if via_reader else "official_api",
                extraction_method="cityu_profile_json",
                evidence_text=evidence[:1000],
                confidence=0.97 if emails and profile_url else 0.88,
                email_association="person_local" if emails else "none",
            )
        )
    return people


def _reader_json_payload(markdown_text: str) -> dict | None:
    marker = "Markdown Content:"
    if marker not in markdown_text:
        return None
    content = markdown_text.partition(marker)[2].strip()
    if not content.startswith("{"):
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _parse_reader_markdown(markdown_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
    heading = re.compile(r"^###\s+\[(.+?)\]\((https?://.+)\)\s*$")
    lines = (markdown_text or "").splitlines()
    starts = [index for index, line in enumerate(lines) if heading.match(line.strip())]
    people: list[ParsedPerson] = []
    for position, start in enumerate(starts):
        match = heading.match(lines[start].strip())
        if match is None:
            continue
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        chunk = [clean_text(line) for line in lines[start + 1 : end] if clean_text(line)]
        name = _clean_name(match.group(1))
        if not _valid_name(name):
            continue
        role_lines = [line for line in chunk if ACADEMIC_ROLE_RE.search(line)]
        emails = sorted(
            set(
                value.lower()
                for value in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", " ".join(chunk), flags=re.I)
            )
        )
        people.append(
            ParsedPerson(
                name=name,
                title="; ".join(list(dict.fromkeys(role_lines))[:6]) or None,
                department=_source_unit(config, source_url),
                profile_url=match.group(2),
                emails=emails,
                source_url=source_url,
                source_type="official_directory_via_reader",
                extraction_method="cityu_reader_markdown",
                evidence_text=clean_text(" ".join([lines[start], *chunk]))[:1000],
                confidence=0.9 if emails else 0.8,
                email_association="person_local" if emails else "none",
            )
        )
    return people


def _filtered_generic_people(html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
    unit = _source_unit(config, source_url)
    people: list[ParsedPerson] = []
    for person in parse_faculty_directory(html_text, source_url):
        if not person.name or not _valid_name(_clean_name(person.name)):
            continue
        person.name = _clean_name(person.name)
        if unit:
            person.department = unit
        person.extraction_method = "cityu_federated_generic"
        people.append(person)
    return people


def _parse_cityu_ee_legacy_profile(
    html_text: str,
    source_url: str,
    config: dict,
) -> ParsedPerson | None:
    """Parse person-local EE homepages exported from old Microsoft HTML.

    These pages have no semantic heading or structured person metadata, so the
    generic profile parser correctly refuses to guess a name.  The EE template
    does, however, expose a stable ``<title>... EE CityU HK`` convention (and
    older pages put the name in the first visible line), plus person-local
    appointment and contact evidence.  Restrict this fallback to tilde
    homepages on the official EE host so arbitrary legacy HTML is never treated
    as a person profile.
    """

    parsed_url = urlparse(source_url)
    host = (parsed_url.hostname or "").lower().removeprefix("www.")
    if host != "ee.cityu.edu.hk" or not parsed_url.path.startswith("/~"):
        return None

    soup = BeautifulSoup(html_text or "", "html.parser")
    title_text = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    title_match = re.match(
        r"^(?:Dr|Prof)\.?\s+(.+),\s*EE CityU HK$",
        title_text,
        flags=re.I,
    )
    name = _clean_name(title_match.group(1)) if title_match else ""
    if not name or not _valid_name(name):
        visible_lines = [
            clean_text(value)
            for value in soup.get_text("\n", strip=True).splitlines()
            if clean_text(value)
        ]
        name = next(
            (
                _clean_name(value)
                for value in visible_lines[:25]
                if _valid_name(_clean_name(value))
                and not UNIT_PATTERN.search(value)
                and "city university" not in value.lower()
            ),
            "",
        )
    if not name:
        return None

    person = parse_profile_page(
        html_text,
        source_url,
        extra_name_candidates=[name],
    )
    if person is None:
        return None

    # Prefer the address visibly printed in the contact block.  Some exported
    # pages retain a stale mailto target while displaying the current address;
    # preserve that target as ambiguous evidence rather than assigning it.
    visible_emails = sorted(
        set(
            value.lower()
            for value in re.findall(
                r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
                soup.get_text(" ", strip=True),
                flags=re.I,
            )
        )
    )
    person_emails, rejected_emails = split_person_and_ambiguous_emails(visible_emails)
    if person_emails:
        previous_emails = set(person.emails + person.ambiguous_emails)
        previous_emails.update(extract_conflicting_mailto_emails(soup))
        person.emails = person_emails
        person.ambiguous_emails = sorted(
            (previous_emails | set(rejected_emails)) - set(person_emails)
        )
        person.email_association = "person_local"

    if not person.research_areas:
        page_text = clean_text(soup.get_text(" ", strip=True))
        interests_match = re.search(
            r"\b(?:his|her|their)\s+(?:current\s+)?research interests?\s+"
            r"includes?(?:\s+in)?\s+(.{10,240}?)(?=\.\s|\.$)",
            page_text,
            flags=re.I,
        )
        if interests_match:
            person.research_areas = [clean_text(interests_match.group(1))]
        else:
            heading = soup.find(
                string=lambda value: clean_text(str(value or "")).strip(" :").lower()
                == "research areas"
            )
            paragraph = heading.find_parent("p") if heading else None
            areas: list[str] = []
            while paragraph is not None and len(areas) < 20:
                paragraph_text = clean_text(paragraph.get_text(" ", strip=True))
                if re.search(r"\b(?:brief profile|contact details)\b", paragraph_text, flags=re.I):
                    break
                for span in paragraph.find_all("span", recursive=False):
                    value = clean_text(span.get_text(" ", strip=True))
                    if value.strip(" :").lower() == "research areas" or not value or len(value) > 160:
                        continue
                    if value not in areas:
                        areas.append(value)
                paragraph = paragraph.find_next_sibling("p")
            person.research_areas = areas

    person.name = name
    person.department = _source_unit(config, source_url) or "Department of Electrical Engineering"
    person.source_type = "official_profile"
    person.extraction_method = "cityu_ee_legacy_profile"
    person.evidence_text = clean_text(
        " ".join(
            filter(
                None,
                [
                    person.name,
                    person.title,
                    person.department,
                    *person.emails,
                    *person.research_areas,
                ],
            )
        )
    )[:1000]
    person.confidence = 0.95 if person.title and person.emails else 0.85
    return person


def parse_cityu_person_profile(
    html_text: str,
    source_url: str,
    config: dict,
) -> ParsedPerson | None:
    """Parse source-specific CityU profiles without treating site chrome as a person.

    Several current MNE, Chemistry and Law profiles render the person's name
    in a Drupal ``field--name-title`` div rather than a semantic heading.  The
    generic parser intentionally ignores arbitrary page-title fields, while
    the directory parser is too broad for a single profile and can mistake
    navigation cards for people.  This adapter activates only when that
    source-specific person field is present and validated as a name.
    """

    parsed_url = urlparse(source_url)
    host = (parsed_url.hostname or "").lower()
    if not (host == "cityu.edu.hk" or host.endswith(".cityu.edu.hk")):
        return None

    ee_legacy_person = _parse_cityu_ee_legacy_profile(html_text, source_url, config)
    if ee_legacy_person is not None:
        return ee_legacy_person

    soup = BeautifulSoup(html_text or "", "html.parser")
    name_node = soup.select_one(".field--name-title")
    if name_node is None:
        return None
    raw_name = clean_text(name_node.get_text(" ", strip=True))
    raw_name = re.sub(r"^Vice-Rector\s+", "", raw_name, flags=re.I)
    name = _clean_name(raw_name)
    if not name or not _valid_name(name):
        return None

    person = parse_profile_page(
        html_text,
        source_url,
        extra_name_candidates=[name],
    )
    if person is None:
        return None

    title_values: list[str] = []
    for node in soup.select(
        ".field--name-field-full-position, "
        ".field--name-field-position-text, "
        ".field--name-field-position-tag, "
        ".block-field-blocknodemne-staffbody .field--name-body"
    ):
        value = clean_text(node.get_text(" ", strip=True))
        value = re.sub(r"^(?:Position(?: Tag| Text)?|Appointment)\s*", "", value, flags=re.I)
        if value and len(value) <= 260 and ACADEMIC_ROLE_RE.search(value):
            title_values.append(value)

    person.name = name
    person.title = "; ".join(dict.fromkeys(title_values)) or person.title
    person.department = _source_unit(config, source_url)
    person.research_areas = [
        value
        for value in person.research_areas
        if not re.search(r"\b(?:cookies?|privacy|website experience)\b", value, flags=re.I)
    ]
    person.source_type = "official_profile"
    person.extraction_method = "cityu_drupal_person_profile"
    person.evidence_text = clean_text(
        " ".join(
            filter(
                None,
                [
                    name,
                    person.title,
                    person.department,
                    *person.emails,
                    *person.research_areas,
                ],
            )
        )
    )[:1000]
    person.confidence = 0.95 if person.title and person.emails else 0.85
    return person


def parse_cityu_federated_directory(html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
    stripped = (html_text or "").lstrip()
    if stripped.startswith("Title:") and "Markdown Content:" in stripped:
        reader_payload = _reader_json_payload(stripped)
        if reader_payload is not None and "profiles" in reader_payload:
            return _parse_profile_api(reader_payload, source_url, config, via_reader=True)
        return _parse_reader_markdown(stripped, source_url, config)
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and "Staffs" in payload:
            return _parse_mgt_api(payload, source_url, config)
        if isinstance(payload, dict) and "profiles" in payload:
            return _parse_profile_api(payload, source_url, config)

    central_people = parse_cityu_academic_directory(html_text, source_url, config)
    if central_people:
        return central_people

    soup = BeautifulSoup(html_text or "", "html.parser")
    modal_people = _parse_affiliated_modal_cards(soup, source_url, config)
    cards = []
    seen_cards: set[int] = set()
    for selector in FEDERATED_CARD_SELECTORS:
        for card in soup.select(selector):
            if id(card) not in seen_cards:
                cards.append(card)
                seen_cards.add(id(card))
    people = [
        *modal_people,
        *(person for card in cards if (person := _person_from_card(card, source_url, config))),
    ]
    structured_names = {_punctuation_insensitive_name_key(person.name) for person in people}
    structured_emails = {email.lower() for person in people for email in person.emails}
    structured_profiles = {(person.profile_url or "").lower() for person in people if person.profile_url}
    people.extend(
        person
        for person in _filtered_generic_people(html_text, source_url, config)
        if _punctuation_insensitive_name_key(person.name) not in structured_names
        and not structured_emails.intersection(email.lower() for email in person.emails)
        and not (person.profile_url and person.profile_url.lower() in structured_profiles)
    )
    # Some newly added faculty appear only in the site's generic section, so
    # enrich after both structured and generic records have been assembled.
    _enrich_people_from_directory_rows(people, soup, source_url)

    deduped: dict[str, ParsedPerson] = {}
    for person in people:
        email_key = ",".join(sorted(email.lower() for email in person.emails))
        key = email_key or (person.profile_url or "").lower() or person.name.lower()
        current = deduped.get(key)
        if current is None or person.confidence > current.confidence:
            deduped[key] = person
    return list(deduped.values())


def parse_cityu_multi_person_profile(
    html_text: str,
    source_url: str,
    config: dict,
) -> list[ParsedPerson]:
    """Parse profile links that resolve to an official multi-person card page.

    Most CityU profile links are single-person pages and should stay on the
    lightweight profile adapter chain.  A small number, notably the English
    Department adjunct page, resolve to repeated ``views-row`` person cards.
    Gate the federated parser on that explicit structure so every card is kept
    without running the full directory parser across every individual page.
    """

    soup = BeautifulSoup(html_text or "", "html.parser")
    if not soup.select_one(".faculty-list.people-list .views-row"):
        return []
    return parse_cityu_federated_directory(html_text, source_url, config)


def cityu_multi_person_profile_candidate_count(html_text: str) -> int:
    return len(
        BeautifulSoup(html_text or "", "html.parser").select(
            ".faculty-list.people-list .views-row"
        )
    )


def cityu_federated_candidate_count(html_text: str) -> int:
    stripped = (html_text or "").lstrip()
    if stripped.startswith("Title:") and "Markdown Content:" in stripped:
        payload = _reader_json_payload(stripped)
        if payload is not None and isinstance(payload.get("profiles"), list):
            return len(payload["profiles"])
        return len(re.findall(r"^###\s+\[", stripped, flags=re.M))
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload.get("profiles"), list):
            return len(payload["profiles"])
        return sum(
            len(group.get("data") or [])
            for group in payload.get("Staffs") or []
            if isinstance(group, dict)
        )
    soup = BeautifulSoup(html_text or "", "html.parser")
    central = len(soup.select(CARD_SELECTOR))
    if central:
        return central
    affiliated_articles = soup.select("article.affiliated-faculty.card")
    if affiliated_articles:
        affiliated_name_keys = {
            _punctuation_insensitive_name_key(name)
            for article in affiliated_articles
            if (name := _name_from_card(article))
        }
        other_candidates = 0
        for person in parse_faculty_directory(
            html_text,
            "https://www.cityu.edu.hk/",
            [],
        ):
            name = _clean_name(person.name)
            if not _valid_name(name):
                continue
            if _punctuation_insensitive_name_key(name) in affiliated_name_keys:
                continue
            other_candidates += 1
        return len(affiliated_articles) + other_candidates
    seen: set[int] = set()
    for selector in FEDERATED_CARD_SELECTORS:
        seen.update(id(card) for card in soup.select(selector))
    return len(seen) or len(parse_faculty_directory(html_text, "https://www.cityu.edu.hk/", []))
