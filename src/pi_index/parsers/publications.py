from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
PUBLICATION_LABELS = {
    "publication",
    "publications",
    "selected publication",
    "selected publications",
    "notable publication",
    "notable publications",
    "research output",
    "research outputs",
    "representative works",
}
ARTICLE_TYPES = {"article", "scholarlyarticle"}


@dataclass(frozen=True)
class PublicationSnapshot:
    """Publication inventory parsed from one official page.

    ``authoritative`` is intentionally narrow: it only means the page exposed a
    recognisable publication inventory whose absence can be compared with the
    previous inventory for *that same source*.  It never means that the page is
    a complete bibliography, nor that a disappeared item was retracted.
    """

    fingerprints: list[dict[str, Any]]
    authoritative: bool
    truncated: bool
    inventory_signals: tuple[str, ...]

# Pure profile pages place output-type facets and neighbouring entity cards inside
# containers whose class names also contain ``research-output``.  Those controls
# look enough like publication entries to the generic parser that labels such as
# "1 Book Chapter" used to become official publication fingerprints.
GENERIC_PUBLICATION_LABELS = {
    *PUBLICATION_LABELS,
    "all publications",
    "all research output",
    "all research outputs",
    "research output per year",
    "research outputs per year",
    "publications per year",
    "publication overview",
    "research output overview",
    "conference presentations",
    "no publication",
    "no publications",
    "journal publications and reviews",
}

GENERIC_PUBLICATION_NAV_RE = re.compile(
    r"^(?:view|show|load|see|browse)\s+(?:all\s+)?(?:more\s+)?"
    r"(?:publications?|research outputs?|results?)$",
    re.I,
)
PURE_RESEARCH_OUTPUT_LABEL_RE = re.compile(r"^research outputs?\s*:", re.I)
PURE_NON_PUBLICATION_LABEL_RE = re.compile(
    r"^(?:"
    r"prize\s*:.*"
    r"|activity\s*:.*"
    r"|press\s*/\s*media\s*:.*"
    r"|project\s*:\s*(?:knowledge transfer|research project)"
    r")$",
    re.I,
)
DATE_RANGE_LABEL_RE = re.compile(
    r"^\d{1,2}/\d{1,2}/\d{2,4}\s*(?:→|->|–|—|-)\s*"
    r"\d{1,2}/\d{1,2}/\d{2,4}$"
)
COUNT_PREFIX_RE = re.compile(r"^\s*\d[\d,]*(?:\.\d+)?\s+(?P<label>.+?)\s*$", re.I)
PURE_FACET_URL_RE = re.compile(r"/publications?/?\?(?:[^#]*&)?type=", re.I)
PURE_PERSON_PROFILE_URL_RE = re.compile(r"/persons?/[^/?#]+/?(?:[?#][^#]*)?$", re.I)

# This deliberately describes complete Pure output-type labels rather than merely
# looking for a noun such as "article".  A real title like "3 Articles that
# Changed Competition Law" therefore remains valid even without a DOI/year.
PURE_OUTPUT_TYPE_LABEL_RE = re.compile(
    r"^(?:"
    r"abstracts?"
    r"|(?:article|articles)(?:\s*\([^)]*\))?"
    r"|(?:journal|other|review|newspaper|conference)\s+articles?(?:\s*\([^)]*\))?"
    r"|article\s*\(translation\)"
    r"|journal article\s*\(in progress\)"
    r"|books?(?:\s*\((?:author|editor|translator)\))?"
    r"|books?\s+or\s+reports?"
    r"|book chapters?(?:\s*\(translator\))?"
    r"|chapters?"
    r"|chapter in (?:an\s+)?edited book\s*\(author\)"
    r"|conference\s+(?:papers?|proceedings?|articles?|abstracts?|posters?)"
    r"(?:\s*\([^)]*\))?"
    r"|(?:invited|other)\s+conference\s+papers?"
    r"(?:\s+published in conference proceedings)?"
    r"|meeting abstract published in a journal"
    r"|conference paper published in conference proceedings"
    r"|invited conference paper published in conference proceedings"
    r"|publication in (?:a\s+)?(?:refereed|policy or professional) journal"
    r"|publication in (?:a\s+)?(?:news outlet|magazine)"
    r"|papers?"
    r"|(?:working|discussion) paper(?:\s+series)?"
    r"|policy or profession paper"
    r"|preprints?"
    r"|letters?"
    r"|editorials?(?:\s*/\s*preface)?(?:\s*\(journal\))?"
    r"|comment\s*/\s*debate(?:\s*/\s*erratum)?"
    r"|erratum"
    r"|research book or monograph\s*\(author\)"
    r"|edited book\s*\(editor\)"
    r"|textbook\s*\(author\)"
    r"|authored play, poem, novel, story\s*\((?:book|book chapter or short passage)\)"
    r"|translation of other's work\s*\((?:article|book|chapter)\)"
    r"|reference entry"
    r"|entry for encyclopedia/dictionary"
    r"|foreword\s*/\s*postscript"
    r"|review of books or software"
    r"|(?:doctoral\s+)?theses?\s*/?\s*dissertations?"
    r"|consulting or contract research reports?"
    r"|other (?:outputs?|contributions?)"
    r"|presentations?"
    r"|posters?"
    r"|patents?(?:\s*\((?:non-)?lu\))?"
    r"|exhibitions?"
    r"|performances?(?: and participation in exhibits)?"
    r"|digital, visual or audio products?"
    r"|films?(?:\s*/\s*video| or video)?"
    r"|computer software or system"
    r"|software(?: or system)?"
    r"|engineering, architectural, graphic designs"
    r"|design"
    r"|sound recording"
    r"|artefacts?"
    r"|compositions?(?:\s*/\s*music score)?"
    r"|music score\s*/\s*composition"
    r"|painting, sculpture, drawing, photograph"
    r"|literary works(?:\s*\(translator\))?"
    r"|creative and literary works, consulting reports and case studies"
    r"|written teaching case study or extensive note"
    r"|teaching case"
    r"|teaching development grants\s*\(tdg\)"
    r"|honou?rs projects\s*\(hp\)"
    r"|preregistration"
    r"|protocols?"
    r"|items? of media coverage"
    r")$",
    re.I,
)


def _clean_text(value: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", value or "").strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", cleaned)


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    match = DOI_RE.search(value)
    if not match:
        return None
    return match.group(0).rstrip(".,;)]}").lower()


def _normalized_label(value: str | None) -> str:
    return _clean_text(value).strip(" .,:;\u00a0").casefold()


def _is_pure_count_facet(title: str, publication_url: str | None) -> bool:
    match = COUNT_PREFIX_RE.fullmatch(title)
    if not match:
        return False
    if publication_url and PURE_FACET_URL_RE.search(publication_url):
        return True

    label = _clean_text(match.group("label")).strip(" .,:;")
    # Pure sometimes prepends another count to a collapsed "More" facet, for
    # example "1 More 2 Books".  Peel those UI tokens before classifying it.
    label = re.sub(r"^more\s+", "", label, flags=re.I)
    nested_count = COUNT_PREFIX_RE.fullmatch(label)
    if nested_count:
        label = _clean_text(nested_count.group("label")).strip(" .,:;")
    return PURE_OUTPUT_TYPE_LABEL_RE.fullmatch(label) is not None


def is_meaningful_publication_fingerprint(
    fingerprint: dict[str, Any] | str | None = None,
    *,
    title: str | None = None,
    citation_text: str | None = None,
    publication_year: int | str | None = None,
    doi: str | None = None,
    publication_url: str | None = None,
) -> bool:
    """Return whether a parsed item represents an individual scholarly work.

    ``fingerprint`` may be either the parser's record dictionary or a title
    string.  Explicit keyword arguments make the predicate useful to importers
    that have not yet assembled a record.
    """

    if isinstance(fingerprint, dict):
        title = title if title is not None else fingerprint.get("title")
        citation_text = citation_text if citation_text is not None else fingerprint.get("citation_text")
        publication_year = (
            publication_year if publication_year is not None else fingerprint.get("publication_year")
        )
        doi = doi if doi is not None else fingerprint.get("doi")
        publication_url = publication_url if publication_url is not None else fingerprint.get("publication_url")
    elif isinstance(fingerprint, str) and title is None:
        title = fingerprint

    cleaned_title = _clean_text(title)
    if len(cleaned_title) < 8 or not any(character.isalpha() for character in cleaned_title):
        return False

    normalized_title = _normalized_label(cleaned_title)
    if normalized_title in GENERIC_PUBLICATION_LABELS:
        return False
    if GENERIC_PUBLICATION_NAV_RE.fullmatch(cleaned_title):
        return False
    if PURE_RESEARCH_OUTPUT_LABEL_RE.match(cleaned_title):
        return False
    if PURE_NON_PUBLICATION_LABEL_RE.fullmatch(cleaned_title):
        return False
    if DATE_RANGE_LABEL_RE.fullmatch(cleaned_title):
        return False
    if re.match(r"^\(?\s*[#*]\s+denotes\b", cleaned_title, re.I):
        return False
    title_is_url = re.match(r"^https?://", cleaned_title, re.I) is not None
    title_is_bare_domain = re.fullmatch(
        r"(?:www\.)?[a-z0-9-]+(?:\.[a-z]{2,})+(?:/\S*)?",
        cleaned_title,
        flags=re.I,
    ) is not None
    if publication_url and PURE_PERSON_PROFILE_URL_RE.search(publication_url):
        return False

    evidence_text = " ".join(part for part in [cleaned_title, _clean_text(citation_text)] if part)
    has_doi = bool(normalize_doi(doi) or normalize_doi(evidence_text) or normalize_doi(publication_url))
    has_year = publication_year is not None or bool(YEAR_RE.search(evidence_text))
    if has_doi or has_year:
        return True
    if title_is_url or title_is_bare_domain:
        return False

    return not _is_pure_count_facet(cleaned_title, publication_url)


def _iter_json_nodes(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _iter_json_nodes(item)
    elif isinstance(value, dict):
        yield value
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from _iter_json_nodes(item)


def _article_type(node: dict[str, Any]) -> bool:
    value = node.get("@type")
    values = value if isinstance(value, list) else [value]
    return any(str(item).lower().rstrip("/").rsplit("/", 1)[-1] in ARTICLE_TYPES for item in values)


def _publication_section_items(soup: BeautifulSoup) -> list[Tag]:
    items: list[Tag] = []
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        label = _clean_text(heading.get_text(" ", strip=True)).lower().rstrip(":")
        if label not in PUBLICATION_LABELS and "publication" not in label and "research output" not in label:
            continue
        heading_level = int(heading.name[1])
        sibling = heading.find_next_sibling()
        while sibling is not None:
            if isinstance(sibling, Tag) and re.fullmatch(r"h[1-6]", sibling.name or ""):
                if int(sibling.name[1]) <= heading_level:
                    break
            if isinstance(sibling, Tag):
                if sibling.name in {"li", "p", "article", "tr"}:
                    items.append(sibling)
                else:
                    nested = sibling.find_all(["li", "p", "article", "tr"])
                    items.extend(nested)
            sibling = sibling.find_next_sibling()

    # HKU Business School (and other WordPress/WGL sites) renders visual
    # headings as nested ``span.dbl__title`` elements instead of semantic hN
    # tags.  The publication list is the next sibling of the surrounding
    # ``.wgl-double_heading`` block.  Keep the traversal bounded by the next
    # visual heading so sections such as service and teaching cannot leak into
    # the official publication inventory.
    for title in soup.select(".dbl__title"):
        label = _normalized_label(title.get_text(" ", strip=True))
        if label not in PUBLICATION_LABELS:
            continue
        heading = title.find_parent(class_="wgl-double_heading") or title.parent
        sibling = heading.find_next_sibling() if isinstance(heading, Tag) else None
        while sibling is not None:
            if isinstance(sibling, Tag) and sibling.select_one(".dbl__title"):
                break
            if isinstance(sibling, Tag):
                if sibling.name in {"li", "p", "article", "tr"}:
                    items.append(sibling)
                else:
                    items.extend(sibling.find_all(["li", "p", "article", "tr"]))
            sibling = sibling.find_next_sibling()
    return items


def _publication_inventory_signals(soup: BeautifulSoup) -> tuple[str, ...]:
    signals: list[str] = []
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        label = _normalized_label(heading.get_text(" ", strip=True))
        if label in PUBLICATION_LABELS or "publication" in label or "research output" in label:
            signals.append("labelled_section")
            break
    if any(
        _normalized_label(title.get_text(" ", strip=True)) in PUBLICATION_LABELS
        for title in soup.select(".dbl__title")
    ):
        signals.append("labelled_section")
    if soup.select_one(
        ".publication-list, .publications-list, [class*='publication-list'], "
        ".research-output-list, [class*='research-output-list'], "
        "[data-section='publications'], [data-section='research-outputs']"
    ):
        signals.append("structured_inventory")
    # Pure person profiles expose the result list independently of the output
    # type facets.  Requiring both a person-profile URL shape and a result item
    # prevents a facet card elsewhere on the site becoming a removal baseline.
    if soup.select_one(".results-container .research-output, .result-container .research-output"):
        signals.append("pure_results_inventory")
    return tuple(dict.fromkeys(signals))


def _citation_title(node: Tag, citation_text: str) -> str | None:
    quoted = re.search(r"[\u201c\"]\s*(.{8,}?)\s*[\u201d\"]", citation_text)
    if quoted:
        return _clean_text(quoted.group(1)).strip(" ,.;:")[:1000]

    journal = node.find(["em", "i"])
    journal_text = (
        _clean_text(journal.get_text(" ", strip=True)) if journal is not None else ""
    )

    # APA/author-year bibliographies often contain no semantic markup around
    # the work title.  Remove the author prefix and stop before the marked-up
    # journal (when present) or the next citation sentence.
    author_year = re.match(
        r"^.+?(?:\(\s*(?:19|20)\d{2}[a-z]?\s*\)|,\s*(?:19|20)\d{2}"
        r"|\.\s*(?:19|20)\d{2}[a-z]?\s*\.)"
        r"\s*[.:]?\s*(?P<title>.+)$",
        citation_text,
        flags=re.I,
    )
    if author_year:
        candidate = author_year.group("title")
        if journal_text and candidate.startswith(journal_text):
            candidate = journal_text
        elif journal_text and journal_text in candidate:
            candidate = candidate.partition(journal_text)[0]
        else:
            candidate = re.split(r"\.\s+(?=[A-Z])", candidate, maxsplit=1)[0]
        candidate = re.split(r"\.\s+In\s+", candidate, maxsplit=1, flags=re.I)[0]
        candidate = _clean_text(candidate).strip(" \u00a0\u201c\u201d\"',.;:")
        if len(candidate) >= 8 and len(candidate.split()) >= 2:
            return candidate[:1000]

    # Some HKUBS lists omit the year between the author list and title, while
    # ending the authors with an inverted ``Surname, I.`` form.  Greedily
    # consume through that final author, then apply the same journal boundary.
    inverted_author_prefix = re.match(
        r"^.+\b[A-Z][A-Za-z'\-]+,\s*(?:[A-Z]\.)+(?:,)?\s+"
        r"(?P<title>[A-Z].+)$",
        citation_text,
    )
    if inverted_author_prefix:
        candidate = inverted_author_prefix.group("title")
        if journal_text and journal_text in candidate:
            candidate = candidate.partition(journal_text)[0]
        else:
            candidate = re.split(r"\.\s+(?=[A-Z])", candidate, maxsplit=1)[0]
        candidate = _clean_text(candidate).strip(" \u00a0\u201c\u201d\"',.;:")
        if len(candidate) >= 8 and len(candidate.split()) >= 2:
            return candidate[:1000]

    if journal is None:
        return None
    if not journal_text:
        return None
    prefix, separator, _ = citation_text.partition(journal_text)
    if not separator:
        return None

    # Conventional bibliography lists put co-authors after the title.
    prefix = re.split(r"\s*\(\s*with\b", prefix, maxsplit=1, flags=re.I)[0]

    candidate = _clean_text(prefix).strip(" \u00a0\u201c\u201d\"',.;:")
    if len(candidate) < 8 or len(candidate.split()) < 2:
        return None
    return candidate[:1000]


def _best_title(node: Tag, citation_text: str) -> str:
    if candidate := _citation_title(node, citation_text):
        return candidate
    for selector in ["cite", "strong", "em", "i"]:
        candidate = node.select_one(selector)
        if candidate:
            value = _clean_text(candidate.get_text(" ", strip=True))
            if len(value) >= 8:
                return value[:1000]
    for link in node.find_all("a", href=True):
        value = _clean_text(link.get_text(" ", strip=True))
        if len(value) >= 8 and not normalize_doi(value) and value.lower() not in {"doi", "link", "pdf", "view"}:
            return value[:1000]
    return citation_text[:1000]


def _from_tag(node: Tag, source_url: str) -> dict[str, Any] | None:
    citation_text = _clean_text(node.get_text(" ", strip=True))
    if len(citation_text) < 8:
        return None
    links = [urljoin(source_url, link.get("href") or "") for link in node.find_all("a", href=True)]
    doi = normalize_doi(" ".join([citation_text, *links]))
    year_match = YEAR_RE.search(citation_text)
    publication_url = None
    if doi:
        publication_url = f"https://doi.org/{doi}"
    elif links:
        publication_url = links[0]
    return {
        "title": _best_title(node, citation_text),
        "citation_text": citation_text[:2000],
        "publication_year": int(year_match.group(0)) if year_match else None,
        "doi": doi,
        "publication_url": publication_url,
        "confidence": 0.9 if doi else 0.7,
    }


def _from_jsonld(soup: BeautifulSoup, source_url: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": lambda value: value and "ld+json" in value}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _iter_json_nodes(data):
            if not _article_type(node):
                continue
            title = _clean_text(str(node.get("name") or node.get("headline") or ""))
            if len(title) < 8:
                continue
            identifier = node.get("doi") or node.get("identifier") or ""
            if isinstance(identifier, dict):
                identifier = identifier.get("value") or identifier.get("@value") or ""
            url = node.get("url") or node.get("@id")
            date = str(node.get("datePublished") or node.get("dateCreated") or "")
            year_match = YEAR_RE.search(date)
            doi = normalize_doi(f"{identifier} {url or ''}")
            records.append(
                {
                    "title": title[:1000],
                    "citation_text": title[:2000],
                    "publication_year": int(year_match.group(0)) if year_match else None,
                    "doi": doi,
                    "publication_url": f"https://doi.org/{doi}" if doi else (urljoin(source_url, str(url)) if url else None),
                    "confidence": 0.95 if doi else 0.85,
                }
            )
    return records


def extract_publication_snapshot(html_text: str, source_url: str) -> PublicationSnapshot:
    soup = BeautifulSoup(html_text or "", "html.parser")
    inventory_signals = _publication_inventory_signals(soup)
    records = _from_jsonld(soup, source_url)
    nodes = _publication_section_items(soup)

    for container in soup.select(
        ".publication-item, .publication-entry, .research-output, "
        "[class*='publication-item'], [class*='research-output']"
    ):
        nested = container.find_all(["li", "p", "article", "tr"])
        nodes.extend(nested or [container])

    # DOI links outside a labelled section are still strong publication evidence.
    for link in soup.find_all("a", href=True):
        href = urljoin(source_url, link.get("href") or "")
        if not normalize_doi(href):
            continue
        parent = link.find_parent(["li", "p", "article"]) or link
        nodes.append(parent)

    for node in nodes:
        record = _from_tag(node, source_url)
        if record:
            records.append(record)

    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        if not is_meaningful_publication_fingerprint(record):
            continue
        title_key = re.sub(r"\W+", " ", record["title"].lower()).strip()
        key = record.get("doi") or f"{title_key}|{record.get('publication_year') or ''}"
        existing = deduped.get(key)
        if existing is None or float(record.get("confidence", 0)) > float(existing.get("confidence", 0)):
            deduped[key] = record
    fingerprints = list(deduped.values())
    truncated = len(fingerprints) > 500
    return PublicationSnapshot(
        fingerprints=fingerprints[:500],
        authoritative=bool(inventory_signals) and not truncated,
        truncated=truncated,
        inventory_signals=inventory_signals,
    )


def extract_publication_fingerprints(html_text: str, source_url: str) -> list[dict[str, Any]]:
    """Backward-compatible list API used by the institution adapters."""

    return extract_publication_snapshot(html_text, source_url).fingerprints
