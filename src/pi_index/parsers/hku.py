from __future__ import annotations

import codecs
from dataclasses import dataclass, field
import json
import re
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

from ..models import ParsedPerson
from .generic_html import clean_text, external_ids_from_links, likely_name, parse_profile_page
from .mailto import extract_emails_from_html, split_person_and_ambiguous_emails


@dataclass
class _Candidate:
    raw_name: str
    title: str | None
    card: Tag
    profile_url: str | None = None
    department: str | None = None
    research_areas: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    curated_academic: bool = False


def _matches_any(value: str | None, patterns: list[str]) -> bool:
    lower = (value or "").lower()
    return any(pattern.lower() in lower for pattern in patterns if pattern)


ACADEMIC_ROLE_RE = re.compile(
    r"\b(?:professor|lecturer|reader|principal investigator|group leader|lab director|"
    r"research (?:assistant|associate|fellow|officer)|post[- ]?doctoral|academic staff|faculty member|"
    r"chairperson|dean|director|head|programme coordinator|program coordinator)\b",
    flags=re.I,
)
def _host(value: str) -> str:
    return urlparse(value).netloc.lower().removeprefix("www.")


def _path(value: str) -> str:
    return urlparse(value).path.rstrip("/").lower()


def _canonical_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def _individual_profile_url(value: str | None) -> str | None:
    """Return only a person-specific current profile URL.

    A few HKU Arts cards point several people at the same staff collection,
    which must remain directory evidence rather than becoming one person's
    profile.  SMLC also currently publishes one corrected Wix slug while an
    older directory still links the stale form.
    """

    if not value:
        return None
    host = _host(value)
    path = _path(value)
    if host == "linguistics.hku.hk" and path in {"/people/faculty", "/people/affiliates"}:
        return None
    if host == "music.hku.hk" and path == "/honorary-parttime-affiliated.html":
        return None
    if host == "genderstudies.hku.hk" and path == "/elizabeth-lacouture":
        # The Arts directory still supplies the current person record, while
        # this former individual page now returns 404 and has no verified
        # replacement on the current official Gender Studies site.
        return None
    if host == "web.smlc.hku.hk":
        if path == "/teachingstaff/cha-paul-s.k.":
            # The legacy Arts link is now 404 and Paul Cha is not present on
            # SMLC's current official teaching-staff list.  Keep the directory
            # record, but do not invent a replacement profile URL.
            return None
        if path == "/teachingstaff/guerillot-benoit-gilles":
            return "https://www.web.smlc.hku.hk/teachingstaff/guerillot-benoit-gilles-"
    return value


def _source_unit(config: dict, source_url: str) -> str | None:
    source_host = _host(source_url)
    source_path = _path(source_url)
    best: tuple[int, str] | None = None
    for unit in (config.get("pool_scope") or {}).get("units") or []:
        for seed in unit.get("seed_urls") or []:
            seed_host = _host(seed)
            seed_path = _path(seed)
            if source_host != seed_host:
                continue
            if source_path == seed_path or source_path.startswith(seed_path + "/"):
                candidate = (len(seed_path), clean_text(str(unit.get("name") or "")))
                if candidate[1] and (best is None or candidate[0] > best[0]):
                    best = candidate
    return best[1] if best else None


def _medicine_department(source_url: str) -> str | None:
    host = _host(source_url)
    departments = {
        "anaesthesia.hku.hk": "Department of Anaesthesiology",
        "oncology.med.hku.hk": "Department of Clinical Oncology",
        "radiology.med.hku.hk": "Department of Diagnostic Radiology",
        "emed.med.hku.hk": "Department of Emergency Medicine",
        "fmpc.hku.hk": "Department of Family Medicine and Primary Care",
        "medic.hku.hk": "Department of Medicine",
        "microbiology.hku.hk": "Department of Microbiology",
        "hkumicro.hku.hk": "Department of Microbiology",
        "obsgyn.med.hku.hk": "Department of Obstetrics and Gynaecology",
        "ophthalmology.hku.hk": "Department of Ophthalmology",
        "ortho.hku.hk": "Department of Orthopaedics and Traumatology",
        "paed.hku.hk": "Department of Paediatrics and Adolescent Medicine",
        "patho.hku.hk": "Department of Pathology",
        "psychiatry.hku.hk": "Department of Psychiatry",
        "surgery.hku.hk": "Department of Surgery",
        "hkuccmu.hku.hk": "Critical Care Medicine Unit",
        "mehu.hku.hk": "Medical Ethics and Humanities Unit",
        "sbms.hku.hk": "School of Biomedical Sciences",
        "nursing.hku.hk": "School of Nursing",
        "sph.hku.hk": "School of Public Health",
        "pharma.hku.hk": "Department of Pharmacology and Pharmacy",
        "scm.hku.hk": "School of Chinese Medicine",
    }
    return departments.get(host)


def _clean_name(value: str) -> str:
    value = clean_text(value)
    value = re.sub(
        r"^(?:(?:The Honourable|Hon\.?|Ar\.?|Ir\.?)\s+)?"
        r"(?:Professor|Prof\.?|Doctor|Dr\.?|Mr\.?|Ms\.?|Mrs\.?)\s+",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"(?:\u6559\u6388|\u535a\u58eb)$", "", value).strip()
    value = re.sub(
        r"\s*\((?:(?:undergraduate|postgraduate|seminar)\s+coordinator|until\s+\d{4})\)\s*$",
        "",
        value,
        flags=re.I,
    )
    return clean_text(value.strip(" |-"))


def _law_structured_name(given_name: str, family_name: str) -> str:
    """Normalize the Law directory's honorific and post-nominal subfields."""

    given_name = re.sub(
        r"^(?:(?:Prof(?:essor)?|Dr|The Hon(?:ourable)?(?: Mr| Mrs| Madam)? Justice|"
        r"Mr Justice|Mrs Justice)\.?\s+)+",
        "",
        clean_text(given_name),
        flags=re.I,
    )
    family_name = clean_text(family_name).split(",", 1)[0]
    return clean_text(" ".join(filter(None, [given_name, family_name])))


def _law_profile_emails(
    soup: BeautifulSoup,
    name_node: Tag,
) -> tuple[list[str], list[str]]:
    """Decode Law's person-local ROT13 email attributes.

    The current Law template ROT13-encodes the address itself while leaving the
    literal ``[at]`` separator unchanged.  Replace that separator before ROT13
    decoding; decoding first would turn it into ``[ng]`` and make the address
    unrecoverable.
    """

    # The name and appointment live in the left column, while the email lives
    # in its right sibling.  Their nearest shared ancestor is the person-local
    # content wrapper; limiting extraction to that wrapper also excludes the
    # Faculty-wide footer/contact address.
    scope = name_node.find_parent(class_="int_content_wrapper")
    scope = scope or name_node.parent or soup
    emails = set(extract_emails_from_html(str(scope)))
    for node in scope.select("[data-enc-email]"):
        encoded = clean_text(str(node.get("data-enc-email") or ""))
        if not encoded:
            continue
        encoded = re.sub(
            r"\s*(?:\[at\]|\(at\)|\s+at\s+)\s*",
            "@",
            encoded,
            flags=re.I,
        )
        decoded = codecs.decode(encoded, "rot_13")
        emails.update(extract_emails_from_html(decoded))

    # A small number of pages use a second WordPress mail-protection shape:
    # ``decodeURIComponent("%27%6a...%27")``.  Decode only literal percent-
    # encoded constants; never evaluate the surrounding JavaScript.
    for match in re.finditer(
        r"decodeURIComponent\(\s*(['\"])(?P<value>(?:%[0-9a-f]{2})+)\1\s*\)",
        str(scope),
        flags=re.I,
    ):
        emails.update(extract_emails_from_html(unquote(match.group("value"))))
    return split_person_and_ambiguous_emails(sorted(emails))


def _first_string(node: Tag | None) -> str:
    if node is None:
        return ""
    return clean_text(next(node.stripped_strings, ""))


def _text(node: Tag | None) -> str:
    return clean_text(node.get_text(" ", strip=True) if node else "")


def _texts(node: Tag, selector: str) -> list[str]:
    return list(dict.fromkeys(_text(item) for item in node.select(selector) if _text(item)))


def _first_profile_link(card: Tag, source_url: str, patterns: tuple[str, ...] = ()) -> str | None:
    candidates: list[Tag] = []
    if card.name == "a" and card.get("href"):
        candidates.append(card)
    candidates.extend(card.select("a[href]"))
    for link in candidates:
        href = clean_text(link.get("href") or "")
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        absolute = urljoin(source_url, href)
        if patterns and not any(pattern.lower() in absolute.lower() for pattern in patterns):
            continue
        return absolute
    return None


def _role_title(values: list[str], positive: list[str]) -> str | None:
    roles: list[str] = []
    for value in values:
        value = clean_text(value).strip(" ,;")
        if not value or len(value) > 240:
            continue
        if _matches_any(value, positive) or ACADEMIC_ROLE_RE.search(value):
            if value not in roles:
                roles.append(value)
    return "; ".join(roles) or None


def _obfuscated_emails(value: str) -> list[str]:
    emails: set[str] = set()
    pattern = re.compile(
        r"\b([A-Z0-9._%+-]+)\s*(?:\(at\)|\[at\]|\s+at\s+)\s*"
        r"([A-Z0-9.-]+\.[A-Z]{2,})\b",
        flags=re.I,
    )
    for match in pattern.finditer(value or ""):
        emails.add(f"{match.group(1)}@{match.group(2)}".lower())
    return sorted(emails)


def _candidate_nodes(soup: BeautifulSoup, source_url: str) -> list[Tag]:
    host = _host(source_url)
    path = _path(source_url)
    if host == "hub.hku.hk" and path == "/simple-search":
        return [
            row
            for row in soup.select("table.crisrp tbody tr")
            if row.select_one('td[headers="t1"] a[href*="/cris/rp/"]')
        ]
    if host == "arts.hku.hk" and path == "/about-us/find-an-expert":
        items: list[Tag] = []
        seen: set[tuple[str, str, str]] = set()
        for item in soup.select(".hkuarts_researcher_listing__item[data-name]"):
            key = (
                clean_text(item.get("data-name") or "").lower(),
                clean_text(item.get("data-department") or "").lower(),
                clean_text(item.get("data-url") or "").lower(),
            )
            if key not in seen:
                seen.add(key)
                items.append(item)
        return items
    if host == "arch.hku.hk" and "/people/" in path:
        return [
            item
            for item in soup.select("a.peopleItem[href]")
            if item.select_one(".name")
            and {"filter_academic-staff", "filter_honorary-staff"}.intersection(item.get("class") or [])
            and "filter_non-academic-staff" not in (item.get("class") or [])
        ]
    if host == "philosophy.hku.hk" and path == "/faculty-and-staff":
        return soup.select(".staff_item")
    if host == "hkubs.hku.hk" and path.startswith("/people"):
        return soup.select(".people-item")
    if host == "facdent.hku.hk" and "professoriate-staff" in path:
        return soup.select(".team-style-03")
    if host == "web.edu.hku.hk" and path.startswith("/faculty-academics"):
        return soup.select("a.flex.flex--circle-blk")
    if host == "law.hku.hk" and "academic-staff" in path:
        return soup.select(".staff")
    if host == "scifac.hku.hk" and path.startswith("/people/a-z/"):
        return soup.select(".staff__item")
    if host == "web.socsc.hku.hk" and path == "/people":
        return soup.select(".people-item")
    if host == "civil.hku.hk" and "acstaff" in path:
        return [row for row in soup.select("tr") if row.select_one(".hoverImageWrapper")]
    if host == "dase.hku.hk" and "academic-staff" in path:
        return soup.select(".card-blk__itm")
    if host == "cds.hku.hk" and "academic-staff" in path:
        return soup.select(".search-filter-results > .grid > div")
    if host in {"ece.hku.hk", "eee.hku.hk"} and path.startswith("/people"):
        return soup.select(".et_pb_blurb")
    if host == "i-school.hku.hk" and path.startswith("/people"):
        return soup.select("article.people.member_category-academic")
    if host == "mech.hku.hk" and "academic-staff" in path:
        return soup.select(".e-loop-item")
    if host == "web.chinese.hku.hk" and "academic_staff" in path:
        return soup.select('a.image[href*="/people/staff/"]')
    if host == "med.hku.hk" and "our-professional" in path:
        return soup.select(".staff-box")
    if host == "sbms.hku.hk" and path == "/faculty":
        return soup.select(".views-view-grid__item")
    if host == "nursing.hku.hk" and "/people/" in path:
        return soup.select(".ppl-blk__item")
    if host == "sph.hku.hk" and "academic-staff" in path:
        return soup.select(".staff-item")
    if host == "pharma.hku.hk" and "professoriate-staff" in path:
        return soup.select(".staff-card")
    if host == "scm.hku.hk" and path.endswith(("professoriatestaff.html", "researchstaff.html")):
        # One list can contain several people.  Treating the whole ``ul`` as
        # one card silently retained only its first person.
        return soup.select(".staffList > li")
    if host == "ppa.hku.hk" and path.startswith("/people"):
        return soup.select('.right-wrap[data-people-type="faculty-members"] .member-item')
    if host == "jmsc.hku.hk" and path.startswith("/people"):
        return soup.select("article.people, .post-item.people")
    if host == "web.socialwork.hku.hk" and path == "/faculty-members":
        return soup.select(
            '#faculty-members-list-container a[href*="/faculty-members/"]'
        )
    if host == "psychology.hku.hk" and "faculty-members" in path:
        return [
            row
            for row in soup.select('tr[class*="ninja_table_row"]')
            if row.select_one('a[href*="/people/"]')
        ]
    if host == "hkums.hku.hk" and path.startswith("/staff-page"):
        return soup.select(".team.team_vertical")
    if host == "geog.hku.hk" and path == "/full-time-academic-staff":
        return soup.select(".wixui-repeater__item")
    if host == "anaesthesia.hku.hk" and path.endswith("/our-team/academic"):
        return soup.select(".item-card")
    if host == "obsgyn.med.hku.hk" and path.endswith("/our-team/university-staff"):
        return [row for row in soup.select("table.StaffTable tbody tr") if row.select_one(".NameText")]
    if host == "ortho.hku.hk" and "/staff/hku-" in path:
        return soup.select(".staff-item")
    if host == "ophthalmology.hku.hk" and path == "/academic-and-clinical-staff":
        return soup.select(".wixui-repeater__item")
    if host == "psychiatry.hku.hk" and path == "/academic-staff":
        return soup.select(".wixui-repeater__item")
    if host == "hkuccmu.hku.hk" and path == "/academic-staff":
        return soup.select(".wixui-repeater__item")
    if host == "mehu.hku.hk" and path == "/academic-staff":
        return soup.select(".vc_tta-panel[id]")
    if host == "hkumicro.hku.hk" and path == "/university-staff":
        return [item for item in soup.select(".box.has-hover") if item.select_one(".box-text-inner p a[href]")]
    if soup.select_one(".staff-card .staff-card-name"):
        return soup.select(".staff-card")
    return []


def _standard_candidates(
    soup: BeautifulSoup,
    source_url: str,
    config: dict,
) -> list[_Candidate]:
    host = _host(source_url)
    path = _path(source_url)
    positive: list[str] = []
    department = _source_unit(config, source_url)
    candidates: list[_Candidate] = []

    for card in _candidate_nodes(soup, source_url):
        raw_name = ""
        title: str | None = None
        profile_url: str | None = None
        card_department = department
        research_areas: list[str] = []
        curated = False

        if host == "hub.hku.hk" and path == "/simple-search":
            name_link = card.select_one('td[headers="t1"] a[href*="/cris/rp/"]')
            raw_name = _text(name_link)
            listed_department = _text(card.select_one('td[headers="t3"]'))
            card_department = listed_department or department
            interest_text = _text(card.select_one('td[headers="t4"]'))
            research_areas = [
                clean_text(value)
                for value in re.split(r";|\n", interest_text)
                if clean_text(value)
            ][:20]
            profile_url = (
                urljoin(source_url, name_link.get("href"))
                if name_link and name_link.get("href")
                else None
            )
            curated = True
        elif host == "arts.hku.hk":
            raw_name = clean_text(card.get("data-name") or "")
            title = "Faculty Researcher"
            listed_department = clean_text(card.get("data-department") or "")
            card_department = f"Faculty of Arts: {listed_department}" if listed_department else department
            area = clean_text(card.get("data-area") or "")
            research_areas = [clean_text(value) for value in area.split(";") if clean_text(value)]
            listed_url = clean_text(card.get("data-url") or "")
            if listed_url and (_host(listed_url) == "hku.hk" or _host(listed_url).endswith(".hku.hk")):
                profile_url = listed_url
            curated = True
        elif host == "arch.hku.hk":
            raw_name = _first_string(card.select_one(".name"))
            if path == "/people/honorary-professors":
                group_heading = card.find_previous("h2")
                title = _text(group_heading) or "Honorary Professor"
            else:
                title = _text(card.select_one(".title")) or None
            classes = set(card.get("class") or [])
            architecture_units = {
                "filter_arch": "Department of Architecture",
                "filter_dla": "Division of Landscape Architecture",
                "filter_rec": "Department of Real Estate and Construction",
                "filter_upad": "Department of Urban Planning and Design",
            }
            card_department = next(
                (unit for marker, unit in architecture_units.items() if marker in classes),
                department,
            )
            profile_url = _first_profile_link(card, source_url)
            curated = True
        elif host == "philosophy.hku.hk":
            name_link = card.select_one(".staff_content h4 a[href]")
            raw_name = _text(name_link) or _text(card.select_one(".staff_content h4"))
            title = _text(card.select_one(".staff_content .title")) or None
            research_text = _text(card.select_one(".staff_content .field"))
            research_areas = [
                clean_text(value)
                for value in re.split(r"[,;]", research_text)
                if clean_text(value) and len(clean_text(value)) <= 160
            ][:20]
            profile_url = (
                urljoin(source_url, name_link.get("href"))
                if name_link
                and name_link.get("href")
                and (_host(urljoin(source_url, name_link.get("href"))) == "hku.hk"
                     or _host(urljoin(source_url, name_link.get("href"))).endswith(".hku.hk"))
                else None
            )
            curated = True
        elif host == "hkubs.hku.hk":
            raw_name = _text(card)
            title = "Professoriate Faculty"
            profile_url = _first_profile_link(card, source_url, ("/people/",))
            curated = True
        elif host == "facdent.hku.hk":
            lines = [clean_text(value) for value in card.stripped_strings]
            raw_name = lines[0] if lines else ""
            title = _role_title(lines[1:], positive)
            profile_url = _first_profile_link(card, source_url, ("/profile/", "profile/"))
        elif host == "web.edu.hku.hk":
            raw_name = _text(card.select_one(".pp-info__name"))
            title = "; ".join(_texts(card, ".pp-info__title")) or None
            research_areas = _texts(card, ".pp-info__area")
            profile_url = _first_profile_link(card, source_url, ("/faculty-academics/",))
        elif host == "law.hku.hk":
            given_name = _text(card.select_one("#given"))
            family_name = _text(card.select_one("#family"))
            raw_name = _law_structured_name(given_name, family_name)
            if not raw_name:
                raw_name = _text(card.select_one("h2"))
            profession = card.select_one("#profession")
            title_lines = [
                clean_text(value).strip(" ,;")
                for value in (profession.stripped_strings if profession else [])
                if clean_text(value).strip(" ,;")
            ]
            title = "; ".join(dict.fromkeys(title_lines)) or None
            profile_url = _first_profile_link(card, source_url, ("/academic_staff/",))
        elif host == "scifac.hku.hk":
            raw_name = _text(card.select_one(".staff__name"))
            title = _text(card.select_one(".staff__title")) or None
            profile_url = _first_profile_link(card, source_url, ("/people/",))
        elif host == "web.socsc.hku.hk":
            raw_name = _text(card.select_one(".people-name"))
            title = _text(card.select_one(".people-position")) or None
            profile_url = _first_profile_link(card, source_url)
        elif host == "civil.hku.hk":
            cells = card.select("td")
            raw_name = _text(cells[0]) if cells else ""
            title = _text(cells[1]) if len(cells) > 1 else None
            card_department = _text(cells[2]) if len(cells) > 2 else department
            profile_url = _first_profile_link(cells[0] if cells else card, source_url)
        elif host == "dase.hku.hk":
            raw_name = _text(card.select_one(".stf-info__name"))
            title = "; ".join(_texts(card, ".stf-info__title-itm")) or _text(card.select_one(".stf-info__title")) or None
            profile_url = _first_profile_link(card, source_url, ("/people/",))
        elif host == "cds.hku.hk":
            raw_name = _text(card.select_one("h6.heading6"))
            values = [clean_text(value) for value in card.stripped_strings]
            title = _role_title(values[1:], positive)
            profile_url = _first_profile_link(card, source_url)
        elif host in {"ece.hku.hk", "eee.hku.hk"}:
            raw_name = _text(card.select_one(".et_pb_module_header"))
            values = [clean_text(value) for value in card.stripped_strings]
            title = _role_title(values[1:], positive)
            profile_url = _first_profile_link(card, source_url)
        elif host == "i-school.hku.hk":
            headings = _texts(card, ".elementor-heading-title")
            raw_name = headings[0] if headings else _text(card)
            title = "; ".join(headings[1:]) or "Academic Staff"
            profile_url = _first_profile_link(card, source_url, ("/people/",))
            curated = True
        elif host == "mech.hku.hk":
            link = next(
                (
                    item
                    for item in card.select('a[href*="/academic-staff/"]')
                    if _text(item)
                ),
                None,
            )
            raw_name = _text(link)
            values = [clean_text(value) for value in card.stripped_strings]
            title = _role_title(values[1:], positive)
            profile_url = urljoin(source_url, link.get("href")) if link and link.get("href") else None
        elif host == "web.chinese.hku.hk":
            raw_name = _text(card.select_one(".name1"))
            title = _text(card.select_one(".position")) or None
            research_areas = _texts(card, ".research_interest")
            profile_url = _first_profile_link(card, source_url, ("/people/staff/",))
        elif host == "med.hku.hk":
            raw_name = _text(card.select_one(".staff-name")) or _text(card.select_one(".staff-box-btn"))
            values = [clean_text(value) for value in card.stripped_strings]
            title = _role_title(values[1:], positive)
            row = card.find_parent(class_="staff-row")
            card_department = _text(row.select_one("h4")) if row else department
            profile_url = _first_profile_link(card, source_url, ("professional",))
            curated = True
        elif host == "sbms.hku.hk":
            raw_name = _text(card.select_one(".staff-card__name"))
            title = _text(card.select_one(".staff-card__position")) or None
            if not title:
                trigger = card.select_one("[data-target]")
                target_id = clean_text(trigger.get("data-target") or "") if trigger else ""
                lightbox = soup.find(id=target_id) if target_id else None
                title = _text(lightbox.select_one(".staff-meta p")) if lightbox else None
            if not title:
                grid = card.find_parent(class_="views-view-grid")
                title = _text(grid.find_previous("h4")) if grid else None
            card_department = _medicine_department(source_url) or department
            profile_url = _first_profile_link(card, source_url, ("/staff/",))
        elif host == "nursing.hku.hk":
            raw_name = _first_string(card.select_one(".ppl-blk__item-name"))
            values = [clean_text(value) for value in card.stripped_strings]
            title = _role_title(values[1:], positive)
            card_department = _medicine_department(source_url) or department
            profile_url = _first_profile_link(card, source_url, ("/people/",))
        elif host == "sph.hku.hk":
            raw_name = _text(card.select_one(".staff-card-name"))
            title_values = [clean_text(value) for value in (card.select_one(".staff-title") or card).stripped_strings]
            title = _role_title(title_values, positive) or "; ".join(title_values) or None
            unit = _text(card.select_one(".staff-division"))
            card_department = unit or _medicine_department(source_url) or department
            profile_url = _first_profile_link(card, source_url, ("/biography/",))
        elif host == "pharma.hku.hk":
            raw_name = _first_string(card.select_one(".staff-card-name"))
            title_values = _texts(card, ".staff-card-title li")
            title = _role_title(title_values, positive) or "; ".join(title_values) or None
            card_department = _medicine_department(source_url) or department
            profile_url = _first_profile_link(card, source_url, ("/our-people/",))
        elif host == "scm.hku.hk":
            raw_name = _text(card.select_one("b")) or _first_string(card)
            group_heading = card.find_previous("h5")
            title = _text(group_heading) or (
                "Professoriate Staff" if path.endswith("professoriatestaff.html") else "Research Staff"
            )
            card_department = _medicine_department(source_url) or department
            profile_url = _first_profile_link(card, source_url)
        elif host == "ppa.hku.hk":
            info = card.select_one(".info-box") or card
            raw_name = _first_string(info.select_one("a[href]")) or _first_string(info)
            values = [clean_text(value) for value in card.stripped_strings]
            title = _role_title(values[1:], positive)
            profile_url = _first_profile_link(card, source_url, ("/people/",))
        elif host == "jmsc.hku.hk":
            link = card.select_one('a[href*="/people/"]')
            raw_name = _text(link)
            values = [clean_text(value) for value in card.stripped_strings]
            title = _role_title(values[1:], positive) or "Academic Staff"
            profile_url = urljoin(source_url, link.get("href")) if link and link.get("href") else None
            curated = True
        elif host == "web.socialwork.hku.hk":
            raw_name = _text(card.select_one(".h6"))
            title = _text(card.select_one(".description")) or None
            card_department = "Department of Social Work and Social Administration"
            profile_url = _first_profile_link(card, source_url, ("/faculty-members/",))
        elif host == "psychology.hku.hk":
            link = card.select_one('a[href*="/people/"]')
            raw_name = _text(link)
            values = [clean_text(value) for value in card.stripped_strings]
            title = _role_title(values[1:], positive)
            cells = card.select("td")
            research_areas = []
            for cell in cells:
                # The live NinjaTable puts the portrait, identity/contact
                # block and research area in separate cells.  Do not copy the
                # full identity/contact cell into research_areas merely because
                # it begins with the professor's name rather than "Office:".
                if cell.select_one('a[href*="/people/"]'):
                    continue
                value = _text(cell)
                if not value or extract_emails_from_html(str(cell)):
                    continue
                if re.search(r"\b(?:office|tel(?:ephone)?|e-?mail)\s*:", value, flags=re.I):
                    continue
                research_areas.append(value)
            research_areas = research_areas[:10]
            profile_url = urljoin(source_url, link.get("href")) if link and link.get("href") else None
        elif host == "hkums.hku.hk":
            name_node = card.select_one(".desc_wrappper_title")
            link = card.select_one('a[href*="/people/"]')
            raw_name = _text(name_node) or _text(link)
            title = _text(card.select_one(".subtitle")) or None
            profile_url = urljoin(source_url, link.get("href")) if link and link.get("href") else None
        elif host == "geog.hku.hk":
            values = [clean_text(value) for value in card.stripped_strings]
            raw_name = values[0] if values else ""
            title = _role_title(values[1:], positive)
            profile_url = _first_profile_link(card, source_url)
        elif host == "anaesthesia.hku.hk":
            raw_name = _text(card.select_one(".item-card-name"))
            values = [clean_text(value) for value in card.stripped_strings]
            title = _role_title(values[1:], positive)
            card_department = _medicine_department(source_url) or department
            profile_url = _first_profile_link(card, source_url)
        elif host == "obsgyn.med.hku.hk":
            link = card.select_one(".NameText a[href]")
            raw_name = _text(link) or _text(card.select_one(".NameText"))
            cells = card.select("td")
            title = _text(cells[1]) if len(cells) > 1 else None
            card_department = _medicine_department(source_url) or department
            profile_url = urljoin(source_url, link.get("href")) if link and link.get("href") else None
        elif host == "ortho.hku.hk":
            raw_name = _text(card.select_one(".staff-name"))
            title = _text(card.select_one(".staff-title")) or None
            card_department = _medicine_department(source_url) or department
            profile_url = _first_profile_link(card, source_url)
        elif host in {"ophthalmology.hku.hk", "psychiatry.hku.hk", "hkuccmu.hku.hk"}:
            values = [clean_text(value) for value in card.stripped_strings]
            raw_name = values[0] if values else ""
            title = _role_title(values[1:], positive)
            card_department = _medicine_department(source_url) or department
            profile_url = _first_profile_link(card, source_url)
        elif host == "mehu.hku.hk":
            raw_name = _text(card.select_one(".vc_tta-title-text"))
            title_node = card.select_one("p.vc_custom_heading")
            title_values = [clean_text(value) for value in title_node.stripped_strings] if title_node else []
            title = _role_title(title_values, positive)
            card_department = _medicine_department(source_url) or department
            panel_id = clean_text(card.get("id") or "")
            profile_url = f"{source_url.rstrip('/')}#{panel_id}" if panel_id else source_url
        elif host == "hkumicro.hku.hk":
            link = card.select_one(".box-text-inner p a[href]")
            raw_name = _text(link)
            row = card.find_parent(class_="row")
            group_title = _text(row.find_previous(["h2", "h3"])) if row else ""
            subrole = _text(card.select_one(".box-text-inner h4"))
            title = "; ".join(dict.fromkeys(filter(None, [group_title, subrole]))) or None
            card_department = _medicine_department(source_url) or department
            profile_url = urljoin(source_url, link.get("href")) if link and link.get("href") else None
        elif host == "patho.hku.hk" and "staff-card" in (card.get("class") or []):
            raw_name = _text(card.select_one(".staff-card-name"))
            # The first item is the appointment.  Later list items are degrees
            # and contact details and must not be concatenated into ``title``.
            title = (
                _text(card.select_one(".staff-card-title li b, .staff-card-title li strong"))
                or _text(card.select_one(".staff-card-title li"))
                or None
            )
            card_department = _medicine_department(source_url) or department
            profile_url = _first_profile_link(card, source_url)
        elif "staff-card" in (card.get("class") or []):
            raw_name = _text(card.select_one(".staff-card-name"))
            title_values = _texts(card, ".staff-card-title li")
            title = _role_title(title_values, positive) or "; ".join(title_values) or None
            if raw_name.lower().startswith(("professor ", "prof. ")) and not ACADEMIC_ROLE_RE.search(title or ""):
                title = "; ".join(filter(None, ["Professor", title]))
            card_department = _medicine_department(source_url) or department
            profile_url = _first_profile_link(card, source_url)

        candidates.append(
            _Candidate(
                raw_name=raw_name,
                title=title,
                card=card,
                profile_url=profile_url,
                department=card_department,
                research_areas=research_areas,
                curated_academic=curated,
            )
        )
    return candidates


def _english_ajax_candidates(html_text: str, source_url: str, config: dict) -> list[_Candidate]:
    if _host(source_url) != "english.hku.hk" or "data_people.php" not in _path(source_url):
        return []
    try:
        payload = json.loads(html_text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, list):
        return []
    department = _source_unit(config, source_url)
    candidates: list[_Candidate] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = clean_text(str(item.get("name") or ""))
        title = clean_text(str(item.get("desc") or "")) or None
        if not name:
            continue
        card_soup = BeautifulSoup("<article></article>", "html.parser")
        card = card_soup.article
        card.append(clean_text(" | ".join(filter(None, [name, title, str(item.get('email') or '')]))))
        research_areas = [
            clean_text(str(value.get("research_area_name") or ""))
            for value in item.get("research") or []
            if isinstance(value, dict) and value.get("research_area_name")
        ]
        candidates.append(
            _Candidate(
                raw_name=name,
                title=title,
                card=card,
                profile_url=clean_text(str(item.get("ppl_url") or "")) or None,
                department=department,
                research_areas=research_areas,
                emails=[clean_text(str(item.get("email") or ""))],
                curated_academic=True,
            )
        )
    return candidates


def _wix_candidates(soup: BeautifulSoup, source_url: str, config: dict) -> list[_Candidate]:
    host = _host(source_url)
    path = _path(source_url)
    path_prefixes: dict[str, tuple[str, ...]] = {
        "web.smlc.hku.hk": ("/teachingstaff/",),
        "geog.hku.hk": ("/p-", "/full-time-academic-staff/", "/staff/", "/people/"),
        "sociology.hku.hk": ("/people/",),
        "sbme.hku.hk": ("/people/",),
    }
    prefixes = path_prefixes.get(host)
    if not prefixes:
        return []
    positive: list[str] = []
    department = _source_unit(config, source_url)
    candidates: list[_Candidate] = []
    seen_urls: set[str] = set()

    for link in soup.select("a[href]"):
        absolute = urljoin(source_url, link.get("href") or "")
        if _host(absolute) != host or _path(absolute) == path:
            continue
        if not any(_path(absolute).startswith(prefix.rstrip("/")) for prefix in prefixes):
            continue
        canonical = _canonical_url(absolute)
        if not canonical or canonical in seen_urls:
            continue
        container: Tag | None = None
        for parent in list(link.parents)[:5]:
            if not isinstance(parent, Tag):
                continue
            value = _text(parent)
            if not value or len(value) > 1000:
                continue
            values = [clean_text(item) for item in parent.stripped_strings]
            if likely_name(_text(link)) or any(likely_name(item) for item in values[:12]):
                container = parent
                break
        if container is None:
            continue
        values = [clean_text(value) for value in container.stripped_strings]
        link_name = _text(link)
        honorific_name = next(
            (
                value
                for value in values
                if len(value) <= 120
                and re.match(r"^(?:Professor|Prof\.?|Doctor|Dr\.?)\s+(?!\()", value, flags=re.I)
            ),
            "",
        )
        title = _role_title([value for value in values if value != honorific_name], positive)
        raw_name = honorific_name or (link_name if link_name and len(link_name) <= 120 else "")
        if not raw_name:
            for value in values:
                if value == title or _matches_any(value, positive) or ACADEMIC_ROLE_RE.search(value):
                    continue
                if len(value) <= 120:
                    raw_name = value
                    break
        if raw_name:
            candidates.append(
                _Candidate(
                    raw_name=raw_name,
                    title=title,
                    card=container,
                    profile_url=absolute,
                    department=department,
                )
            )
            seen_urls.add(canonical)
    return candidates


def parse_hku_directory(html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
    ajax_candidates = _english_ajax_candidates(html_text, source_url, config)
    soup = BeautifulSoup(html_text or "", "html.parser")
    candidates = ajax_candidates or _standard_candidates(soup, source_url, config)
    if not candidates:
        candidates = _wix_candidates(soup, source_url, config)

    people: dict[tuple[str, str], ParsedPerson] = {}
    for candidate in candidates:
        name = _clean_name(candidate.raw_name)
        if not name or len(name) > 160 or name.lower() in {"people", "academic staff", "faculty"}:
            continue
        profile_url = _individual_profile_url(candidate.profile_url)
        card_html = str(candidate.card)
        emails = set(extract_emails_from_html(card_html))
        emails.update(value for value in candidate.emails if value)
        emails.update(_obfuscated_emails(_text(candidate.card)))
        person_emails, ambiguous_emails = split_person_and_ambiguous_emails(sorted(emails))
        evidence_text = _text(candidate.card)
        person = ParsedPerson(
            name=name,
            title=clean_text(candidate.title or "") or None,
            department=candidate.department,
            profile_url=profile_url,
            emails=person_emails,
            ambiguous_emails=ambiguous_emails,
            research_areas=list(dict.fromkeys(candidate.research_areas))[:20],
            source_url=source_url,
            source_type=(
                "official_research_directory"
                if _host(source_url) == "hub.hku.hk"
                else "official_directory"
            ),
            extraction_method="hku_multi_template_directory",
            evidence_text=evidence_text[:1000],
            confidence=0.95 if candidate.title and profile_url else 0.85,
            email_association="person_local" if person_emails else "none",
        )
        key = (name.lower(), _canonical_url(profile_url) or source_url)
        existing = people.get(key)
        if existing is None:
            people[key] = person
            continue

        # Some current directories repeat one person under leadership and
        # academic-rank sections.  The higher-confidence observation supplies
        # the base fields, while repeated cards contribute complementary data.
        primary, secondary = (
            (person, existing)
            if person.confidence > existing.confidence
            else (existing, person)
        )
        titles = [value for value in (primary.title, secondary.title) if value]
        primary.title = "; ".join(dict.fromkeys(titles)) or None
        primary.emails = sorted(set(primary.emails + secondary.emails))
        primary.ambiguous_emails = sorted(
            set(primary.ambiguous_emails + secondary.ambiguous_emails)
        )
        primary.research_areas = list(
            dict.fromkeys(primary.research_areas + secondary.research_areas)
        )[:20]
        primary.profile_url = primary.profile_url or secondary.profile_url
        primary.lab_url = primary.lab_url or secondary.lab_url
        primary.department = primary.department or secondary.department
        primary.external_ids = {**secondary.external_ids, **primary.external_ids}
        primary.publication_fingerprints = list(
                {
                    json.dumps(item, ensure_ascii=False, sort_keys=True): item
                    for item in (
                        primary.publication_fingerprints
                        + secondary.publication_fingerprints
                    )
                }.values()
        )
        primary.email_association = (
            "person_local" if primary.emails else primary.email_association
        )
        people[key] = primary
    return list(people.values())


def _first_profile_email_after_name(soup: BeautifulSoup, name_node: Tag) -> tuple[list[str], list[str]]:
    all_emails = extract_emails_from_html(str(soup))
    general_contacts = {"chinmed", "complit", "dentistry", "globalba", "jmsc", "mech", "sbms", "smlc"}
    eligible_emails = [
        email
        for email in all_emails
        if email.endswith("@hku.hk") and email.partition("@")[0] not in general_contacts
    ]
    if len(eligible_emails) == 1:
        person_email = eligible_emails[0]
        return [person_email], sorted(set(all_emails) - {person_email})
    person_email: str | None = None
    for node in name_node.find_all_next():
        if isinstance(node, Tag) and node.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            if _text(node).casefold() == "contact us":
                break
        if not isinstance(node, Tag) or node.name != "a" or not node.get("href"):
            continue
        for email in extract_emails_from_html(str(node)):
            local_part = email.partition("@")[0]
            if email.endswith("@hku.hk") and local_part not in general_contacts:
                person_email = email
                break
        if person_email:
            break
    if not person_email:
        return [], all_emails
    return [person_email], sorted(set(all_emails) - {person_email})


def _smlc_research_areas(soup: BeautifulSoup) -> list[str]:
    for value in soup.find_all(string=True):
        if clean_text(str(value)).casefold() != "research area":
            continue
        label = value.find_parent("div")
        area_node = label.find_next_sibling("div") if label else None
        area_text = _text(area_node)
        if not area_text:
            continue
        areas = []
        for item in re.split(r"[,;]", area_text):
            area = clean_text(item).strip("\u200b\u200c\u200d\ufeff")
            if area and any(character.isalnum() for character in area) and len(area) <= 200:
                areas.append(area)
        return areas[:20]
    return []


def _comparative_literature_research_areas(soup: BeautifulSoup) -> list[str]:
    for paragraph in soup.select("p"):
        text = _text(paragraph)
        match = re.search(r"\bspeciali[sz]es in (.+?)(?:\.\s|\.$|$)", text, flags=re.I)
        if not match:
            continue
        return [
            area
            for area in (
                clean_text(item).removeprefix("and ")
                for item in match.group(1).split(",")
            )
            if area and len(area) <= 200
        ][:20]
    return []


def _likely_source_specific_name(value: str) -> bool:
    return bool(
        value
        and len(value) <= 160
        and len(value.split()) >= 2
        and any(character.isalpha() for character in value)
        and "@" not in value
        and not re.search(r"\d", value)
        and not ACADEMIC_ROLE_RE.search(value)
    )


def _iter_json_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _iter_json_nodes(item)
    elif isinstance(value, dict):
        yield value
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from _iter_json_nodes(item)


def _facdent_profile_data(soup: BeautifulSoup) -> dict | None:
    """Read the Faculty of Dentistry's authoritative Person JSON-LD block."""

    people: list[dict] = []
    for script in soup.find_all("script", attrs={"type": lambda value: value and "ld+json" in value}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _iter_json_nodes(data):
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if any(str(value).casefold() == "person" for value in types):
                people.append(node)
    # A real profile page has exactly one Person.  Refuse ambiguous aggregate
    # JSON-LD rather than arbitrarily attaching one person's fields to another.
    unique_people = {
        clean_text(str(person.get("name") or "")): person
        for person in people
        if clean_text(str(person.get("name") or ""))
    }
    if len(unique_people) != 1:
        return None
    person = next(iter(unique_people.values()))
    name = _clean_name(clean_text(str(person.get("name") or "")))
    if not _likely_source_specific_name(name):
        return None
    raw_titles = person.get("jobTitle") or []
    if isinstance(raw_titles, str):
        raw_titles = [raw_titles]
    titles = list(dict.fromkeys(clean_text(str(value)) for value in raw_titles if clean_text(str(value))))
    raw_areas = person.get("knowsAbout") or []
    if isinstance(raw_areas, str):
        raw_areas = [raw_areas]
    areas = [
        clean_text(str(value))
        for value in raw_areas
        if clean_text(str(value)) and len(clean_text(str(value))) <= 240
    ][:20]
    email_values = person.get("email") or []
    if isinstance(email_values, str):
        email_values = [email_values]
    emails, ambiguous = split_person_and_ambiguous_emails(
        extract_emails_from_html(" ".join(str(value) for value in email_values))
    )
    return {
        "name": name,
        "title": "; ".join(titles) or None,
        "department": "Faculty of Dentistry",
        "research_areas": areas,
        "emails": emails,
        "ambiguous_emails": ambiguous,
    }


_CIVIL_ROLE_RE = re.compile(
    r"\b(?:Research Assistant Professor|Chair Professor|Clinical Professor|"
    r"Associate Professor|Assistant Professor|Senior Lecturer|Lecturer|Professor)\b",
    flags=re.I,
)


def _civil_profile_identity(raw_heading: str) -> tuple[str, str | None]:
    value = re.sub(
        r"^(?:(?:Ir\.?\s+)?Professor|(?:Ir\.?\s+)?Dr\.?)\s+",
        "",
        clean_text(raw_heading),
        flags=re.I,
    )
    role_match = _CIVIL_ROLE_RE.search(value)
    if not role_match:
        return "", None
    raw_name = clean_text(value[: role_match.start()]).strip(" ,")
    # Several endowed-title headings put the chair name between the actual
    # surname and the appointment.  In those pages an initial-led personal name
    # has a clearly capitalised surname; stop there instead of absorbing the
    # endowed title (for example, ``H. YE Leung Cheuk Tong ...``).
    tokens = raw_name.split()
    if tokens and any("." in token for token in tokens[:-1]):
        for index, token in enumerate(tokens[1:], start=1):
            surname = token.strip(".,")
            if len(surname) >= 2 and surname.isupper() and any(character.isalpha() for character in surname):
                raw_name = " ".join(tokens[: index + 1]).strip(" ,")
                break
    raw_name = re.sub(r",\s*(?:MH|BBS|JP)(?:\s*,\s*(?:MH|BBS|JP))*\s*$", "", raw_name, flags=re.I)
    roles = list(dict.fromkeys(clean_text(match.group(0)) for match in _CIVIL_ROLE_RE.finditer(value)))
    name = _clean_name(raw_name)
    return (name if _likely_source_specific_name(name) else ""), "; ".join(roles) or None


def _strip_trailing_chinese_name(value: str) -> str:
    return clean_text(re.sub(r"\s+[\u3400-\u9fff]+\s*$", "", value))


def _role_lines(node: Tag | None, *, limit: int = 4) -> list[str]:
    if node is None:
        return []
    roles: list[str] = []
    for value in node.stripped_strings:
        line = clean_text(str(value)).strip(" ,;")
        lower = line.casefold()
        if not line or len(line) > 240:
            continue
        if re.match(r"^(?:bsc|ba|beng|msc|ma|meng|ph\.?d|dmedsc|credentials?)\b", lower, flags=re.I):
            continue
        if ACADEMIC_ROLE_RE.search(line) and lower not in {
            "academic staff",
            "teaching staff",
            "technical staff",
            "administrative staff",
            "supporting staff",
        }:
            roles.append(line)
        if len(roles) >= limit:
            break
    return list(dict.fromkeys(roles))


def _civil_research_areas(soup: BeautifulSoup) -> list[str]:
    labels = [
        node
        for node in soup.find_all(["strong", "b", "h2", "h3", "h4", "h5", "h6"])
        if _text(node).casefold() in {"research interests", "research areas"}
    ]
    if not labels:
        return []
    listing = labels[-1].find_next("ul")
    if listing is None:
        return []
    return [
        area
        for area in (clean_text(_text(item)) for item in listing.find_all("li", recursive=False))
        if area and len(area) <= 240
    ][:20]


def _elementor_research_areas(soup: BeautifulSoup, labels: set[str]) -> list[str]:
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if _text(heading).casefold() not in labels:
            continue
        widget = heading.find_parent(class_=lambda value: value and "elementor-element" in value)
        value_widget = widget.find_next_sibling(class_=lambda value: value and "elementor-element" in value) if widget else None
        value = _text(value_widget)
        if not value or len(value) > 4000:
            continue
        areas = [
            clean_text(item)
            for item in re.split(r"[;\n]", value)
            if clean_text(item) and len(clean_text(item)) <= 240
        ]
        if areas:
            return areas[:20]
    return []


_BIOMEDICAL_PROFILE_ROLE_RE = re.compile(
    r"\b(?:Emeritus Professor|Chair Professor|Research Assistant Professor|"
    r"Honorary Clinical Associate Professor|Honorary Clinical Assistant Professor|"
    r"Honorary Clinical Professor|Clinical Associate Professor|Clinical Assistant Professor|"
    r"Clinical Professor|Honorary Associate Professor|Honorary Assistant Professor|"
    r"Honorary Professor|Associate Professor|Assistant Professor|Adjunct Professor|"
    r"Visiting Professor|Professor|Senior Lecturer|Clinical Lecturer|Lecturer|Reader)\b",
    flags=re.I,
)


def _biomedical_profile_identity(card: Tag) -> tuple[str, str | None, Tag | None]:
    """Read one HKUMed shared-template staff card without site-navigation bleed."""

    name_node = card.select_one(".staff-card-name")
    raw_name = _strip_trailing_chinese_name(_clean_name(_text(name_node)))
    raw_name = re.sub(
        r",\s*(?:(?:MH|BBS|JP|GBM|GBS|CBE|SBS)\s*,?\s*)+$",
        "",
        raw_name,
        flags=re.I,
    )
    name = clean_text(raw_name)
    if not _likely_source_specific_name(name):
        return "", None, name_node

    title_text = _text(card.select_one(".staff-card-title"))
    roles = list(
        dict.fromkeys(clean_text(match.group(0)) for match in _BIOMEDICAL_PROFILE_ROLE_RE.finditer(title_text))
    )
    return name, "; ".join(roles) or None, name_node


def _biomedical_research_areas(soup: BeautifulSoup) -> list[str]:
    labels = {"research interests", "key research areas", "research profile"}
    for heading in soup.find_all(["h3", "h4", "h5", "h6"]):
        if _text(heading).casefold() not in labels:
            continue
        content: Tag | None = None
        if heading.find_next_sibling() is not None:
            content = heading.find_next_sibling()
        if content is None and isinstance(heading.parent, Tag):
            content = heading.parent.find_next_sibling()
        if content is None:
            continue
        items = [
            clean_text(_text(item))
            for item in content.select("li")
            if clean_text(_text(item)) and len(clean_text(_text(item))) <= 240
        ]
        if items:
            return list(dict.fromkeys(items))[:20]
        values = [
            clean_text(_text(item))
            for item in content.find_all(["p", "div"], recursive=False)
            if clean_text(_text(item)) and len(clean_text(_text(item))) <= 240
        ]
        if values:
            return list(dict.fromkeys(values))[:20]
    return []


def parse_hku_profile(html_text: str, source_url: str, config: dict | None = None) -> ParsedPerson | None:
    """Parse HKU's federated person pages, then fall back to the generic profile parser."""

    soup = BeautifulSoup(html_text or "", "html.parser")
    host = _host(source_url)
    path = _path(source_url)
    name = ""
    title: str | None = None
    department: str | None = None
    name_node: Tag | None = None
    source_specific = False
    structured_research_areas: list[str] = []
    structured_emails: list[str] | None = None
    structured_ambiguous_emails: list[str] | None = None
    structured_external_ids: dict[str, str] | None = None

    # This SMLC URL is a shared collection page, not an individual profile.
    # Let the official directory retain its four honorary professors without
    # manufacturing a fifth person from the page heading.
    if host == "web.smlc.hku.hk" and path == "/honorary-professors":
        return None

    biomedical_cards = soup.select(".profile-card.staff-card")
    if len(biomedical_cards) == 1:
        card = biomedical_cards[0]
        name, title, name_node = _biomedical_profile_identity(card)
        if name:
            department = _medicine_department(source_url)
            card_emails = set(extract_emails_from_html(str(card)))
            card_emails.update(_obfuscated_emails(_text(card)))
            structured_emails, structured_ambiguous_emails = split_person_and_ambiguous_emails(
                sorted(card_emails)
            )
            structured_research_areas = _biomedical_research_areas(soup)
            source_specific = True
    elif (
        host == "oncology.med.hku.hk"
        and path.endswith("/dr-lanqi-gong/dr-lanqi-gong-profit1")
    ):
        # This official URL is a published stub: it has no staff card or
        # biography, only a person breadcrumb plus the department-wide contact
        # address.  Preserve the breadcrumb as the deterministic join key for
        # the complete directory record, but never promote site navigation or
        # oncology@hku.hk to person fields.
        breadcrumb = soup.select_one(".breadcrumb li:last-child, .breadcrumb span:last-child")
        candidate_name = _clean_name(
            re.sub(r"\s+Profit1\s*$", "", _text(breadcrumb), flags=re.I)
        )
        if _likely_source_specific_name(candidate_name):
            name = candidate_name
            name_node = breadcrumb
            department = "Department of Clinical Oncology"
            structured_emails = []
            structured_ambiguous_emails = extract_emails_from_html(str(soup))
            source_specific = True
    elif host == "facdent.hku.hk" and path.startswith("/people/professoriate-staff/profile/"):
        data = _facdent_profile_data(soup)
        if data:
            name = data["name"]
            title = data["title"]
            department = data["department"]
            structured_research_areas = data["research_areas"]
            structured_emails = data["emails"]
            structured_ambiguous_emails = data["ambiguous_emails"]
            source_specific = True
    elif host == "law.hku.hk" and path.startswith("/academic_staff/"):
        name_node = soup.select_one("h2.staff_name")
        given_name = _text(name_node.select_one("#given")) if name_node else ""
        family_name = _text(name_node.select_one("#family")) if name_node else ""
        candidate_name = _law_structured_name(given_name, family_name)
        if name_node and _likely_source_specific_name(candidate_name):
            name = candidate_name
            appointment = name_node.find_next_sibling("p")
            title_lines = [
                clean_text(str(value)).strip(" ,;")
                for value in (appointment.stripped_strings if appointment else [])
                if clean_text(str(value)).strip(" ,;")
                and len(clean_text(str(value)).strip(" ,;")) <= 240
            ]
            title = "; ".join(dict.fromkeys(title_lines)) or None
            department = "Faculty of Law"
            structured_emails, structured_ambiguous_emails = _law_profile_emails(
                soup,
                name_node,
            )
            source_specific = True
    elif host == "civil.hku.hk" and re.match(r"/(?:pp-|rap-).+\.html$", path, flags=re.I):
        name_node = soup.select_one("h4[style*='green']") or soup.select_one("h4")
        name, title = _civil_profile_identity(_text(name_node))
        if name:
            department = "Department of Civil Engineering"
            structured_research_areas = _civil_research_areas(soup)
            source_specific = True
    elif host == "i-school.hku.hk" and path.startswith("/people/"):
        page_title = _text(soup.title)
        candidate_name = _clean_name(re.sub(r"\s+-\s+HKU I-School\s*$", "", page_title, flags=re.I))
        if _likely_source_specific_name(candidate_name):
            name = candidate_name
            for node in soup.select(".elementor-widget-text-editor .elementor-widget-container"):
                if _clean_name(_text(node)).casefold() == name.casefold():
                    name_node = node
                    break
            role_scope = name_node.find_parent(class_="e-con-inner") if name_node else None
            roles = _role_lines(role_scope)
            title = "; ".join(roles) or None
            department = "HKU I-School"
            structured_research_areas = _elementor_research_areas(soup, {"expertise", "research interests"})
            source_specific = True
    elif host == "mech.hku.hk" and path.startswith("/academic-staff/"):
        name_node = soup.select_one(".elementor-image-box-title")
        candidate_name = _strip_trailing_chinese_name(_clean_name(_text(name_node)))
        if _likely_source_specific_name(candidate_name):
            name = candidate_name
            description = name_node.find_next_sibling() if name_node else None
            title = _text(description) or None
            department = "Department of Mechanical Engineering"
            structured_research_areas = _elementor_research_areas(soup, {"research areas", "research interests"})
            source_specific = True
    elif host == "sbms.hku.hk" and path.startswith("/staff/"):
        name_node = soup.select_one(".field--name-field-name") or soup.select_one(".staff-title")
        candidate_name = _clean_name(_text(name_node))
        if _likely_source_specific_name(candidate_name):
            name = candidate_name
            roles = _role_lines(soup.select_one(".field--name-field-role"))
            title = "; ".join(roles) or None
            department = "School of Biomedical Sciences"
            source_specific = True
    elif host == "sbme.hku.hk" and path == "/people/wangfeifei":
        # The current official page identifies Feifei Wang in its heading and
        # biography, but it also contains a copied Wei-Ning Lee contact block
        # (CB506, wnlee@..., ECE profile, and Google Scholar).  None of that
        # block is evidence about Wang.
        name = "Feifei Wang"
        name_node = soup.select_one("h1")
        title = "Assistant Professor"
        department = "School of Biomedical Engineering"
        structured_emails = []
        structured_ambiguous_emails = [
            email
            for email in extract_emails_from_html(str(soup))
            if email == "sbme@hku.hk"
        ]
        structured_external_ids = {}
        source_specific = True
    elif host == "philosophy.hku.hk" and path.startswith("/staff/"):
        # Profile pages repeat the navigation heading "Faculty and staff"
        # before the actual person name in ``h4.staff_name``.
        name_node = soup.select_one("h4.staff_name, .staff_name")
        candidate_name = _clean_name(_text(name_node))
        if _likely_source_specific_name(candidate_name):
            name = candidate_name
            department = "Department of Philosophy"
            source_specific = True
    elif host == "scifac.hku.hk" and path.startswith("/people/"):
        # Science profiles contain many two-word section headings (including
        # "Current research") that satisfy a generic name shape.  The h1 is
        # the authoritative identity field.
        name_node = soup.select_one("h1.profile__name")
        candidate_name = _clean_name(_text(name_node))
        if "," in candidate_name:
            family, given = (clean_text(value) for value in candidate_name.split(",", 1))
            candidate_name = clean_text(f"{given} {family}")
        if _likely_source_specific_name(candidate_name):
            name = candidate_name
            appointment = _text(soup.select_one("h2.profile__from"))
            title = appointment if appointment and len(appointment) <= 400 else None
            department = "Faculty of Science"
            structured_research_areas = _biomedical_research_areas(soup)
            structured_emails = []
            structured_ambiguous_emails = extract_emails_from_html(str(soup))
            source_specific = True
    elif host == "scm.hku.hk" and "/views/people/" in path:
        name_node = soup.select_one(".cv-name")
        candidate_name = _clean_name(_text(name_node))
        if candidate_name:
            name = candidate_name
            title_lines = list(dict.fromkeys(clean_text(str(value)) for value in (soup.select_one(".cv-title") or []).stripped_strings)) if soup.select_one(".cv-title") else []
            title = "; ".join(title_lines[:4]) or None
            department = "School of Chinese Medicine"
            research_node = soup.select_one("#researchint")
            if research_node:
                structured_research_areas = [
                    clean_text(_text(item).lstrip("•·- "))
                    for item in research_node.select("li")
                    if clean_text(_text(item).lstrip("•·- "))
                ][:20]
            source_specific = True
    elif host == "hkubs.hku.hk" and path.startswith("/people/"):
        name_node = soup.select_one(".team-title")
        candidate_name = _clean_name(_text(name_node))
        if _likely_source_specific_name(candidate_name):
            name = candidate_name
            roles = _role_lines(name_node.find_parent(class_="team-info_wrapper") if name_node else None)
            title = "; ".join(roles) or None
            department = "HKU Business School"
            source_specific = True
    elif host == "history.hku.hk" and path == "/staff-gq-xu":
        # The page's first biography sentence begins with "Professor Xu ...";
        # the generic nearby-title heuristic must not promote that sentence to
        # an appointment.  The official heading immediately above it is the
        # current endowed professorship.  Use Western canonical order to join
        # the Arts directory's explicit "Xu, Guoqi" record.
        name = "Guoqi Xu"
        name_node = next(
            (
                heading
                for heading in soup.select("h1.wp-block-heading")
                if _text(heading).casefold() == "xu guoqi"
            ),
            None,
        )
        appointment = next(
            (
                clean_text(_text(heading))
                for heading in soup.select("h1.wp-block-heading")
                if "professor" in _text(heading).casefold()
                and "2017-2022" not in _text(heading)
                and len(_text(heading)) <= 160
            ),
            "",
        )
        if name_node and appointment:
            title = appointment
            department = "Department of History"
            page_emails = extract_emails_from_html(str(soup))
            structured_emails = [email for email in page_emails if email == "xuguoqi@hku.hk"]
            structured_ambiguous_emails = [email for email in page_emails if email not in structured_emails]
            source_specific = True
    elif host == "music.hku.hk" and path.endswith(".html"):
        name_block = soup.select_one(".staff-adress .text-block-8")
        raw_name = _first_string(name_block)
        candidate_name = _clean_name(raw_name)
        if name_block and _likely_source_specific_name(candidate_name):
            name = candidate_name
            name_node = name_block
            values = [clean_text(value) for value in list(name_block.stripped_strings)[1:]]
            secondary = soup.select_one(".staff-adress .text-block-23")
            if secondary:
                values.extend(clean_text(value) for value in secondary.stripped_strings)
            title = _role_title(values, [])
            if not title and re.match(r"^Professor\b", raw_name, flags=re.I):
                title = "Professor"
            department = "Department of Music"
            source_specific = True
    elif host == "web.smlc.hku.hk" and path.startswith("/teachingstaff/"):
        ignored_headings = {"profile", "key publications", "contact us", "courses offered in"}
        for heading in soup.select("h4"):
            raw_name = _text(heading)
            candidate_name = _clean_name(raw_name)
            if raw_name.casefold() not in ignored_headings and _likely_source_specific_name(candidate_name):
                name = candidate_name
                name_node = heading
                break
        if name_node:
            headings = soup.select("h1, h2, h3, h4, h5, h6")
            name_index = headings.index(name_node)
            title = _role_title(
                [_text(heading) for heading in headings[max(0, name_index - 12) : name_index]],
                [],
            )
            department = "School of Modern Languages and Cultures"
            source_specific = True
    elif host == "complit.hku.hk" and "/faculty/" in path:
        heading = soup.select_one("h1")
        raw_heading = _text(heading)
        title_match = re.search(
            r"\(([^)]*(?:Professor|Lecturer|Reader|Research Fellow)[^)]*)\)\s*$",
            raw_heading,
            flags=re.I,
        )
        raw_name = raw_heading[: title_match.start()] if title_match else raw_heading
        candidate_name = _clean_name(raw_name)
        if heading and _likely_source_specific_name(candidate_name):
            name = candidate_name
            name_node = heading
            title = clean_text(title_match.group(1)) if title_match else None
            department = "Department of Comparative Literature"
            source_specific = True

    person = parse_profile_page(
        html_text,
        source_url,
        extra_name_candidates=[name] if name else None,
    )
    if person is None:
        return None
    if name:
        person.name = name
    if title:
        person.title = title
    if department:
        person.department = department
    if structured_emails is not None:
        person.emails = structured_emails
        person.ambiguous_emails = structured_ambiguous_emails or []
        person.email_association = (
            "person_local"
            if person.emails
            else ("ambiguous_email" if person.ambiguous_emails else "none")
        )
    elif source_specific and name_node and host in {
        "civil.hku.hk",
        "i-school.hku.hk",
        "mech.hku.hk",
        "sbms.hku.hk",
        "scm.hku.hk",
        "hkubs.hku.hk",
    }:
        person.emails, person.ambiguous_emails = _first_profile_email_after_name(soup, name_node)
        person.email_association = "person_local" if person.emails else "none"
    if structured_research_areas:
        person.research_areas = structured_research_areas
    if structured_external_ids is not None:
        person.external_ids = structured_external_ids
    elif biomedical_cards and source_specific:
        person.external_ids = external_ids_from_links(
            BeautifulSoup(str(biomedical_cards[0]), "html.parser"),
            source_url,
        )
    if host == "music.hku.hk" and name_node:
        person.emails, person.ambiguous_emails = _first_profile_email_after_name(soup, name_node)
        person.email_association = "person_local" if person.emails else "none"
    elif host == "web.smlc.hku.hk" and name_node:
        person.emails, person.ambiguous_emails = _first_profile_email_after_name(soup, name_node)
        person.email_association = "person_local" if person.emails else "none"
        areas = _smlc_research_areas(soup)
        if areas:
            person.research_areas = areas
    elif host == "complit.hku.hk":
        if name_node:
            person.emails, person.ambiguous_emails = _first_profile_email_after_name(soup, name_node)
            person.email_association = "person_local" if person.emails else "none"
        areas = _comparative_literature_research_areas(soup)
        if areas:
            person.research_areas = areas
    if source_specific:
        person.extraction_method = "hku_federated_profile"
    return person


def hku_directory_candidate_count(html_text: str, source_url: str, config: dict) -> int:
    ajax_candidates = _english_ajax_candidates(html_text, source_url, config)
    if ajax_candidates:
        return len(ajax_candidates)
    soup = BeautifulSoup(html_text or "", "html.parser")
    nodes = _candidate_nodes(soup, source_url)
    if nodes:
        return len(nodes)
    return len(_wix_candidates(soup, source_url, config))
