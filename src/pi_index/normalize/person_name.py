from __future__ import annotations

import re


_NON_PERSON_PAGE_TITLES = {
    "404 not found",
    "academic staff",
    "academic rankings",
    "adjunct professors",
    "awards and honors",
    "awards and honours",
    "cityuhk scholars",
    "course taught",
    "courses taught",
    "current research",
    "dedicated to",
    "distinguished visiting professors",
    "dive into details",
    "faculty",
    "faculty & staff",
    "faculty and staff",
    "faculty member",
    "faculty members",
    "faculty staff",
    "honorary professor",
    "honorary professors",
    "honourary professor",
    "honourary professors",
    "google scholar",
    "not found",
    "orcid id",
    "our people",
    "page not found",
    "people",
    "personal profile",
    "political development",
    "posted in",
    "posted on",
    "research professors",
    "researchgate",
    "reset filters",
    "scholarship winners",
    "selected publications",
    "scopus author id",
    "staff",
    "staff profile",
    "students and alumni success stories",
    "work experience",
}

_LEGACY_HOMEPAGE_CHROME_RE = re.compile(
    r"^(?:welcome\s+to\s+)?\S.+?(?:(?:['\u2019]s)\s+|\s+)home\s*page$",
    flags=re.I,
)
_BIOGRAPHY_FRAGMENT_RE = re.compile(
    r"^.*\b[A-Za-z][A-Za-z'.-]*[A-Za-z]\.\s+(?:he|she|they)$",
    flags=re.I,
)
_AFFILIATION_NAME_RE = re.compile(
    r"^(?:dept\.?|department|school|faculty|college|institute|centre|center)\s+of\b",
    flags=re.I,
)

_COLLECTIVE_ROLE_WORDS = {
    "academic",
    "academics",
    "adjunct",
    "affiliate",
    "and",
    "assistant",
    "associate",
    "chair",
    "clinical",
    "distinguished",
    "emeritus",
    "faculty",
    "fellow",
    "fellows",
    "global",
    "guest",
    "honorary",
    "honourary",
    "lecturer",
    "lecturers",
    "member",
    "members",
    "part",
    "professor",
    "professors",
    "reader",
    "readers",
    "research",
    "scholar",
    "scholars",
    "staff",
    "teaching",
    "time",
    "visiting",
}
_COLLECTIVE_ROLE_NOUNS = {
    "academic",
    "academics",
    "faculty",
    "fellow",
    "fellows",
    "lecturer",
    "lecturers",
    "member",
    "members",
    "professor",
    "professors",
    "reader",
    "readers",
    "scholar",
    "scholars",
    "staff",
}
_SENTENCE_NAME_WORDS = {
    "a",
    "and",
    "for",
    "in",
    "of",
    "on",
    "that",
    "the",
    "then",
    "to",
    "with",
}

_SECTION_OR_CATEGORY_HEADING_PATTERNS = (
    re.compile(r"^courses?\s+taught$", flags=re.I),
    re.compile(r"^dive\s+into\s+details$", flags=re.I),
    re.compile(
        r"^(?:[\w.'-]+(?:['\u2019]s)?\s+)?scholarships?\s*"
        r"(?:(?:&|and)\s+awards?)?$",
        flags=re.I,
    ),
    re.compile(r"^(?:departmental\s+)?awards?$", flags=re.I),
    re.compile(r"^scholarship\s+winners?$", flags=re.I),
    re.compile(
        r"^select(?:ed)?\s+publications?(?:\s*\([^)]*\))?$",
        flags=re.I,
    ),
    re.compile(r"^(?:(?:personal|staff)\s+)?profiles?$", flags=re.I),
    re.compile(
        r"^(?:link|view)\s+to\s+(?:the\s+)?profiles?$",
        flags=re.I,
    ),
)

_NEWS_HEADING_PATTERNS = (
    re.compile(r"^congratulations?\b", flags=re.I),
    re.compile(r"^another\b.*\b(?:graduate|position)\b", flags=re.I),
    re.compile(r"^(?:guests?|visitors?)\s+visit\b", flags=re.I),
    re.compile(r"^promotion\s+of\b", flags=re.I),
    re.compile(r"^welcome\s+(?:prof(?:essor)?|dr)\.?\b", flags=re.I),
    re.compile(r"\bpaper\s+accepted\b", flags=re.I),
    re.compile(
        r"\bwon\b.*\b(?:award|poster|presentation|symposium)\b",
        flags=re.I,
    ),
)


def _looks_like_collective_role_heading(value: str) -> bool:
    words = re.findall(r"[a-z]+", value.casefold())
    return bool(
        words
        and set(words).issubset(_COLLECTIVE_ROLE_WORDS)
        and set(words).intersection(_COLLECTIVE_ROLE_NOUNS)
    )


def _looks_like_sentence_or_news_heading(value: str) -> bool:
    words = re.findall(r"[a-z]+", value.casefold())
    if any(pattern.search(value.strip()) for pattern in _NEWS_HEADING_PATTERNS):
        return True
    return bool(
        len(words) >= 7
        and sum(word in _SENTENCE_NAME_WORDS for word in words) >= 3
    )


def _looks_like_section_or_category_heading(value: str) -> bool:
    normalized = " ".join(value.split()).strip(" -:|")
    return any(
        pattern.fullmatch(normalized)
        for pattern in _SECTION_OR_CATEGORY_HEADING_PATTERNS
    )


def is_non_person_name(value: str | None) -> bool:
    """Return whether a would-be identity is page chrome rather than a name."""

    name = " ".join((value or "").split()).strip()
    normalized = name.strip(" -:|").casefold()
    return bool(
        not name
        or normalized in _NON_PERSON_PAGE_TITLES
        or _LEGACY_HOMEPAGE_CHROME_RE.fullmatch(name)
        or _BIOGRAPHY_FRAGMENT_RE.fullmatch(name)
        or _AFFILIATION_NAME_RE.search(name)
        or _looks_like_collective_role_heading(name)
        or _looks_like_sentence_or_news_heading(name)
        or _looks_like_section_or_category_heading(name)
    )


def is_title_contaminated_name(value: str | None) -> bool:
    """Reject role/office text that an old card boundary appended to a name."""

    normalized = " ".join((value or "").split()).casefold()
    return bool(
        re.search(
            r"\b(?:affiliate prof\.?|assistant professor|associate professor|"
            r"emeritus professor|honorary professor|prof\.?|professor|lecturer|"
            r"reader|dean|faculty of law)\b",
            normalized,
        )
    )


def split_name(display_name: str) -> tuple[str | None, str | None, str]:
    name = re.sub(r"\s+", " ", display_name or "").strip()
    if "," in name:
        family, given = [part.strip() for part in name.split(",", 1)]
        normalized = f"{given} {family}".strip()
        return given or None, family or None, normalized
    parts = name.split()
    if not parts:
        return None, None, ""
    if len(parts) == 1:
        return None, parts[0], parts[0]
    return " ".join(parts[:-1]), parts[-1], name
