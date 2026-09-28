from __future__ import annotations

import csv
from contextlib import nullcontext
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlsplit
import uuid

from .models import (
    CanonicalPIRecord,
    EmailEvidence,
    InstitutionRecord,
    OfficialPublicationFingerprint,
    PIContactVerdict,
    PersonEvidence,
    RawSourceRecord,
    stable_id,
    utc_now_iso,
)
from .normalize.person_name import is_non_person_name, is_title_contaminated_name
from .parsers.publications import is_meaningful_publication_fingerprint


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _validated_sparse_vector(
    value: dict[str, Any],
    feature_limit: int,
) -> dict[str, float]:
    """Validate an already-built sparse unit vector before persistence."""

    limit = int(feature_limit)
    if limit < 1:
        raise ValueError("feature_limit must be at least 1")
    if not isinstance(value, dict):
        raise TypeError("vector must be a dictionary")
    vector: dict[str, float] = {}
    for raw_term, raw_weight in value.items():
        term = str(raw_term).strip()
        weight = float(raw_weight)
        if not term or not weight:
            raise ValueError("vector terms and weights must be non-empty and non-zero")
        if not math.isfinite(weight):
            raise ValueError("vector weights must be finite")
        if term in vector:
            raise ValueError(f"duplicate normalized vector term: {term}")
        vector[term] = weight
    if len(vector) > limit:
        raise ValueError("vector contains more entries than feature_limit")
    norm = math.sqrt(sum(weight * weight for weight in vector.values()))
    if vector and not math.isclose(norm, 1.0, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError("non-empty sparse vectors must be L2-normalized")
    return vector


def _uppercase_percent_escapes(value: str) -> str:
    """Normalize only URL percent-escape hex case, preserving path case."""

    return re.sub(r"%[0-9a-fA-F]{2}", lambda match: match.group(0).upper(), value)


_LEGACY_SUPERVISION_KEYS = {
    "supervision_signals",
    "pi_supervisor_confidence",
    "likely_supervisor_candidate",
    "supervisor_validity_score",
    "supervision_score",
}
_DEPRECATED_EXPORT_FILENAMES = {
    "verified_supervisor_candidates.csv",
    "plausible_supervisor_review_queue.csv",
    "contactable_non_supervisor.csv",
    "supervisor_candidates.csv",
}


def _model_payload(model_type: type, value: str | dict[str, Any]) -> dict[str, Any]:
    """Read current models from JSON produced before neutral pool semantics.

    Old SQLite files may retain both legacy physical columns and legacy keys in
    ``record_json``.  The physical columns are harmless; model construction and
    all new writes intentionally ignore unknown/retired fields.
    """
    data = json.loads(value) if isinstance(value, str) else dict(value)
    allowed = {item.name for item in fields(model_type)}
    return {key: item for key, item in data.items() if key in allowed}


def _pi_from_json(value: str | dict[str, Any]) -> CanonicalPIRecord:
    return CanonicalPIRecord(**_model_payload(CanonicalPIRecord, value))


def _verdict_from_json(value: str | dict[str, Any]) -> PIContactVerdict:
    return PIContactVerdict(**_model_payload(PIContactVerdict, value))


def _normalize_key_part(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


_BARE_ACADEMIC_TITLE_RE = re.compile(
    r"^(?:(?:adjunct|assistant|associate|chair|clinical|distinguished|emeritus|"
    r"honorary|principal|professorial|research|senior|teaching|visiting)\s+)*"
    r"(?:fellow|instructor|lecturer|professor|reader)(?:\s+of\s+practice)?$",
    re.IGNORECASE,
)


def _title_segments(value: str | None) -> list[str]:
    """Return clean, internally unique appointment segments in source order."""

    result: list[str] = []
    seen: set[str] = set()
    for raw_segment in re.split(r"\s*(?:;|\r?\n)\s*", value or ""):
        segment = " ".join(raw_segment.split())
        normalized = segment.casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(segment)

    # A source can itself emit both an endowed/specialized appointment and the
    # bare rank it contains (for example, "Stelux Professor in Finance;
    # Professor").  The bare rank adds no information in that case.
    normalized_segments = [segment.casefold() for segment in result]
    return [
        segment
        for index, segment in enumerate(result)
        if not (
            _BARE_ACADEMIC_TITLE_RE.fullmatch(segment)
            and any(
                index != other_index
                and len(other) > len(normalized_segments[index])
                and re.search(
                    rf"(?<!\w){re.escape(normalized_segments[index])}(?!\w)",
                    other,
                )
                for other_index, other in enumerate(normalized_segments)
            )
        )
    ]


def _merge_parallel_titles(
    canonical_title: str | None,
    duplicate_title: str | None,
) -> str | None:
    """Union reviewed identities' appointments without losing courtesy roles.

    Canonical segments retain their order and spelling.  Duplicate segments are
    appended unless they are equivalent after whitespace/case normalization or
    are merely a bare academic rank already expressed by a more specific
    canonical appointment.
    """

    canonical_segments = _title_segments(canonical_title)
    result = list(canonical_segments)
    seen = {segment.casefold() for segment in result}
    canonical_normalized = [segment.casefold() for segment in canonical_segments]
    for segment in _title_segments(duplicate_title):
        normalized = segment.casefold()
        if normalized in seen:
            continue
        if _BARE_ACADEMIC_TITLE_RE.fullmatch(segment) and any(
            len(existing) > len(normalized)
            and re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", existing)
            for existing in canonical_normalized
        ):
            continue
        seen.add(normalized)
        result.append(segment)
    return "; ".join(result) or None


def _sqlite_publication_is_meaningful(
    title: str | None,
    citation_text: str | None,
    publication_year: int | str | None,
    doi: str | None,
    publication_url: str | None,
) -> int:
    """SQLite bridge for applying the parser's quality gate to legacy rows."""
    try:
        return int(
            is_meaningful_publication_fingerprint(
                title=title,
                citation_text=citation_text,
                publication_year=publication_year,
                doi=doi,
                publication_url=publication_url,
            )
        )
    except (TypeError, ValueError):
        return 0


_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}
_GENERIC_PROFILE_SLUGS = {
    "academic-staff",
    "academic-and-clinical-staff",
    "academic_staff",
    "affiliates",
    "directory",
    "faculty",
    "faculty-academics",
    "faculty-and-staff",
    "faculty-members",
    "find-an-expert",
    "honorary-professors",
    "our-people",
    "our-team",
    "people",
    "staff",
    "team",
    "teaching-staff",
    "university-staff",
}
_GENERIC_PROFILE_FRAGMENTS = {
    "about",
    "bio",
    "biography",
    "contact",
    "education",
    "overview",
    "publications",
    "research",
    "top",
}
_ROLE_EMAIL_MARKERS = {
    "admin",
    "admission",
    "contact",
    "dean",
    "department",
    "enquir",
    "faculty",
    "general",
    "graduate",
    "help",
    "hr",
    "info",
    "office",
    "reception",
    "recruit",
    "school",
    "secretar",
    "support",
    "webmaster",
}
_NAME_STOPWORDS = {
    "dr",
    "emeritus",
    "miss",
    "mr",
    "mrs",
    "ms",
    "prof",
    "professor",
}
_OFFICIAL_EXTERNAL_ID_KEYS = {
    "employee_id",
    "institution_person_id",
    "official_person_id",
    "official_profile_id",
    "person_id",
    "pure_person_id",
    "researcher_id",
    "scholars_person_id",
    "staff_id",
}

_IDENTITY_INDEX_SENTINEL = ("__indexed__", "1")
_IDENTITY_INDEX_QUERY_CHUNK = 300
_IDENTITY_INDEX_SCHEMA_VERSION = "5-registrable-domain-profile-slug-v1"
_PUBLICATION_REFRESH_SCHEMA_VERSION = "1-source-claims-v1"
_OPENALEX_SYNC_SCHEMA_VERSION = "1-author-works-v1"
_CURRENT_PUBLICATION_CLAIM_STATUSES = ("active", "no_longer_observed")
_CURRENT_OPENALEX_PERSON_WORK_STATUSES = ("active", "missing")
_EXTERNAL_RESEARCH_IDENTITY_HOSTS = {
    "orcid.org",
    "researchgate.net",
    "scholar.google.com",
    "scopus.com",
}


def _normalize_openalex_id(value: str | None, prefix: str) -> str:
    """Normalize an OpenAlex URL or short identifier to its canonical ID."""

    match = re.search(rf"(?:openalex\.org/)?({re.escape(prefix)}\d+)\b", value or "", re.I)
    return match.group(1).upper() if match else ""


def _openalex_abstract_text(work: dict[str, Any]) -> str:
    direct = work.get("abstract_text") or work.get("abstract")
    if isinstance(direct, str):
        return " ".join(direct.split())
    inverted = work.get("abstract_inverted_index")
    if not isinstance(inverted, dict):
        return ""
    positioned: list[tuple[int, str]] = []
    for token, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            try:
                positioned.append((int(position), str(token)))
            except (TypeError, ValueError):
                continue
    return " ".join(token for _position, token in sorted(positioned))


def _openalex_named_values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else ([value] if value else [])
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, dict):
            text = item.get("display_name") or item.get("name") or item.get("keyword")
        else:
            text = item
        cleaned = " ".join(str(text or "").split())
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def _openalex_work_vector_text(work: dict[str, Any]) -> tuple[str, str, list[str]]:
    """Build stable paper-vector input from normalized OpenAlex metadata."""

    title = " ".join(str(work.get("title") or work.get("display_name") or "").split())
    abstract = _openalex_abstract_text(work)
    primary = work.get("primary_topic")
    primary_values = _openalex_named_values(primary)
    topical_values = [
        *_openalex_named_values(work.get("topics")),
        *_openalex_named_values(work.get("keywords")),
        *_openalex_named_values(work.get("concepts")),
    ]
    topics = sorted(
        dict.fromkeys([*primary_values, *topical_values]),
        key=str.casefold,
    )
    sections = []
    if title:
        sections.append(f"Title: {title}")
    if abstract:
        sections.append(f"Abstract: {abstract}")
    if topics:
        sections.append(f"Topics: {'; '.join(topics)}")
    return "\n".join(sections), abstract, topics


def _iter_identity_values(value: Any) -> Iterable[str]:
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_identity_values(item)
    elif value not in (None, ""):
        yield str(value).strip()


def normalize_profile_url(value: str | None) -> str:
    """Return a comparison-only canonical form without tracking noise.

    Section fragments such as ``#bio`` are presentation-only and are removed.
    Some official directories, however, use a fragment as the actual person
    identifier (for example ``/academic-staff#AlexGearin``).  Keeping those
    identity-bearing fragments prevents every person on the shared page from
    collapsing to the same normalized URL.
    """
    if not value:
        return ""
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parsed.port
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", unquote(parsed.path or "/")).rstrip("/") or "/"
    if path.lower().endswith("/index.html"):
        path = path[:-11] or "/"
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=False):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in _TRACKING_QUERY_KEYS:
            continue
        query.append((lowered, item.strip()))
    query_text = urlencode(sorted(query), doseq=True)
    normalized = f"{host}{path}" if host else path
    if query_text:
        normalized = f"{normalized}?{query_text}"
    fragment = unquote(parsed.fragment or "").strip()
    normalized_fragment = re.sub(r"[^a-z0-9]+", "", fragment.casefold())
    if (
        normalized_fragment
        and normalized_fragment not in _GENERIC_PROFILE_FRAGMENTS
    ):
        normalized = f"{normalized}#{normalized_fragment}"
    return normalized.casefold()


def is_external_research_identity_url(value: str | None) -> bool:
    """Return whether a link is an external identifier, not an official profile."""

    host = (urlparse(value or "").hostname or "").casefold().removeprefix("www.")
    return any(
        host == candidate or host.endswith(f".{candidate}")
        for candidate in _EXTERNAL_RESEARCH_IDENTITY_HOSTS
    )


def is_unusable_profile_url(value: str | None) -> bool:
    """Return whether a link cannot represent a usable person/profile page."""

    if not value:
        return False
    parsed = urlparse(value.strip())
    # Canonical profile links must be navigable web documents.  This also
    # catches malformed email anchors seen in official directories (for
    # example ``mailt:person@example.edu``), which must never become identity
    # keys merely because the typo is not the literal ``mailto:`` scheme.
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return True
    if is_external_research_identity_url(value):
        return True
    path = unquote(parsed.path or "").casefold().rstrip("/")
    if path == "/error/404" or path.endswith("/error/404"):
        return True
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    # A research-portal landing page is aggregate infrastructure, not a
    # person's profile.  Keep arbitrary personal-domain roots valid (for
    # example a professor's external lab/homepage) and constrain this rule to
    # the conventional institutional ``scholars.*`` portal host.
    return bool(
        host.startswith("scholars.")
        and path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _is_official_exact_profile_candidate(
    value: str | None,
    record: CanonicalPIRecord,
) -> bool:
    """Whether an exact URL is safe to use as one of two identity anchors.

    Exact URL equality is never sufficient by itself.  This predicate only
    admits non-aggregate URLs sourced from first-party evidence; the caller
    must additionally require compatible names or the same person-local
    non-role email.
    """

    if not value:
        return False
    source = (record.field_sources or {}).get("profile_url", "")
    if source and not source.casefold().startswith("official"):
        return False
    parsed = urlparse(value)
    if not parsed.hostname:
        return False
    segments = [
        segment.casefold()
        for segment in unquote(parsed.path).strip("/").split("/")
        if segment
    ]
    fragment = re.sub(r"[^a-z0-9]+", "", unquote(parsed.fragment or "").casefold())
    if fragment and fragment not in _GENERIC_PROFILE_FRAGMENTS:
        return True
    if not segments:
        return False
    slug = segments[-1].removesuffix(".html").removesuffix(".htm")
    if slug in _GENERIC_PROFILE_SLUGS:
        return False
    if re.search(r"\.(?:jpe?g|png|gif|webp|svg|pdf)$", segments[-1], re.I):
        return False
    return True


_COMMON_COMPOUND_PUBLIC_SUFFIX_LABELS = {
    "ac",
    "co",
    "com",
    "edu",
    "gov",
    "net",
    "org",
    "sch",
}


def _registrable_profile_domain(host: str) -> str:
    """Return a conservative parent-domain scope for profile slug identity.

    Profile hosts commonly move between a university apex and a dedicated
    subdomain (for example ``example.edu`` and ``profiles.example.edu``).  The
    final two labels cover ordinary domains; common country-code second-level
    namespaces such as ``ac.uk`` and ``edu.hk`` need one additional label so we
    never turn the public namespace itself into an identity scope.

    This is intentionally only a candidate-generation key.  The canonical
    records are still compared for independent profile, email, and department
    conflicts before a weak slug match can merge them.
    """

    normalized = host.casefold().strip(".").removeprefix("www.")
    labels = [label for label in normalized.split(".") if label]
    if len(labels) <= 2:
        return normalized
    if len(labels[-1]) == 2 and labels[-2] in _COMMON_COMPOUND_PUBLIC_SUFFIX_LABELS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _profile_slug(value: str | None) -> str:
    if not value:
        return ""
    # Keep identity-bearing query parameters in the fallback profile key.
    # Legacy researcher systems commonly serve every person through the same
    # script path (for example ``profile.php?id=123``); using only the final
    # path segment would collapse distinct people who happen to share a name.
    normalized = normalize_profile_url(value)
    if "?" in normalized:
        return _normalize_key_part(normalized)
    host = (urlparse(value).hostname or "").casefold().removeprefix("www.")
    slug = _normalize_key_part(normalized.rsplit("/", 1)[-1] if normalized else "")
    # A human-readable slug is only locally useful within the institution's
    # registrable domain.  This scope bridges ordinary apex/subdomain URL moves;
    # the separating-identity guard later rejects same-name/same-slug academics
    # (such as the two HKU Yang Liu records) when at least two official identity
    # families disagree.  Exact profiles and strong external IDs remain higher
    # priority evidence.
    domain = _registrable_profile_domain(host)
    return f"{domain}|{slug}" if domain and slug else ""


def _profile_mentions_name(value: str | None, name: str) -> bool:
    if not value:
        return False
    profile_text = unquote(urlparse(value).path).lower()
    tokens = [token for token in re.findall(r"[a-z]+", name.lower()) if len(token) >= 3]
    return any(token in profile_text for token in tokens)


def _profile_mentions_record_name(value: str | None, record: CanonicalPIRecord) -> bool:
    return any(
        _profile_mentions_name(value, name)
        for name in [record.display_name, *(record.aliases or [])]
        if name
    )


def _record_profile_urls(record: CanonicalPIRecord) -> list[str]:
    values = [record.profile_url, *(getattr(record, "profile_urls", None) or [])]
    return list(
        dict.fromkeys(
            value
            for value in values
            if value and not is_unusable_profile_url(value)
        )
    )


def _is_person_specific_profile_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    segments = [segment.casefold() for segment in unquote(parsed.path).strip("/").split("/") if segment]
    if not segments or segments[-1].removesuffix(".html") in _GENERIC_PROFILE_SLUGS:
        return False
    query_keys = {key.casefold() for key, _value in parse_qsl(parsed.query)}
    if query_keys.intersection({"id", "person", "personid", "pid", "profile", "rp", "staff", "uid"}):
        return True
    if len(segments) < 2:
        return False
    return any(
        marker in segments[:-1]
        for marker in {"faculty", "people", "person", "persons", "profile", "profiles", "rp", "staff"}
    ) or len(segments) >= 3


def _official_profile_identities(record: CanonicalPIRecord) -> set[str]:
    identities: set[str] = set()
    for value in _record_profile_urls(record):
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        decoded = unquote(parsed.path)
        for match in re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", decoded, re.I):
            identities.add(f"{host}:uuid:{match.lower()}")
        rp_match = re.search(r"/(?:cris/)?rp/(rp\d+)\b", decoded, re.I)
        if rp_match:
            identities.add(f"{host}:rp:{rp_match.group(1).lower()}")
        for key, item in parse_qsl(parsed.query):
            if key.casefold() in {"id", "person", "personid", "pid", "profile", "rp", "staff", "uid"} and item.strip():
                identities.add(f"{host}:{key.casefold()}:{item.strip().casefold()}")
    return identities


def _normalize_external_identity(kind: str, value: str) -> str | None:
    text = unquote(value).strip()
    if not text:
        return None
    if kind == "orcid":
        match = re.search(r"\d{4}-\d{4}-\d{4}-[\dXx]{4}", text)
        return match.group(0).upper() if match else None
    if kind == "openalex_author_id":
        match = re.search(r"(?:openalex\.org/)?(A\d+)\b", text, re.I)
        return match.group(1).upper() if match else None
    if kind == "scopus_author_id":
        match = re.search(r"(?:authorId=)?(\d{6,})\b", text, re.I)
        return match.group(1) if match else None
    if kind == "google_scholar_id":
        parsed = urlparse(text)
        if parsed.hostname and "scholar.google." in parsed.hostname.lower():
            values = dict(parse_qsl(parsed.query))
            text = values.get("user") or ""
        return text.casefold() or None
    return _normalize_key_part(text) or None


def _strong_external_identities(record: CanonicalPIRecord) -> dict[str, set[str]]:
    identities: dict[str, set[str]] = {
        "orcid": set(),
        "openalex_author_id": set(),
        "official_person_id": set(),
        "scopus_author_id": set(),
        "google_scholar_id": set(),
    }
    for raw_key, raw_value in (record.external_ids or {}).items():
        key = str(raw_key).casefold()
        if key in {"orcid", "orcid_id", "orcid_url"}:
            kind = "orcid"
        elif key in {"openalex", "openalex_author_id", "openalex_id", "openalex_url"}:
            kind = "openalex_author_id"
        elif key in {"scopus_author_id", "scopus_id", "scopus_url"}:
            kind = "scopus_author_id"
        elif key in {"google_scholar_id", "google_scholar_url"}:
            kind = "google_scholar_id"
        elif key in _OFFICIAL_EXTERNAL_ID_KEYS:
            kind = "official_person_id"
        else:
            continue
        for value in _iter_identity_values(raw_value):
            normalized = _normalize_external_identity(kind, value)
            if normalized:
                identities[kind].add(normalized)
    identities["official_person_id"].update(_official_profile_identities(record))
    return identities


def _name_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return {
        token
        for token in re.findall(r"[^\W\d_]+", normalized, flags=re.UNICODE)
        if len(token) > 1 and token not in _NAME_STOPWORDS
    }


def _names_compatible(left: CanonicalPIRecord, right: CanonicalPIRecord) -> bool:
    left_names = [left.display_name, *(left.aliases or [])]
    right_names = [right.display_name, *(right.aliases or [])]
    for left_name in left_names:
        for right_name in right_names:
            if _normalize_key_part(left_name) == _normalize_key_part(right_name):
                return True
            left_tokens = _name_tokens(left_name)
            right_tokens = _name_tokens(right_name)
            if len(left_tokens.intersection(right_tokens)) >= 2:
                return True
    return False


_EXACT_PROFILE_NAME_STOPWORDS = _NAME_STOPWORDS.union({"gbs", "jp"})


def _ordered_subsequence(shorter: list[str], longer: list[str]) -> bool:
    if not shorter:
        return False
    offset = 0
    for value in longer:
        if value == shorter[offset]:
            offset += 1
            if offset == len(shorter):
                return True
    return False


def _exact_profile_name_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return [
        token
        for token in re.findall(r"[^\W\d_]+", normalized, flags=re.UNICODE)
        if token not in _EXACT_PROFILE_NAME_STOPWORDS
    ]


def _exact_profile_explicit_initial_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return {
        token.casefold()
        for token in re.findall(r"[^\W\d_]+", normalized, flags=re.UNICODE)
        if (
            len(token) == 1
            or (1 < len(token) <= 4 and token.isupper())
        )
        and token.casefold() not in _EXACT_PROFILE_NAME_STOPWORDS
    }


def _ordered_name_initials(tokens: list[str], explicit_tokens: set[str]) -> list[str]:
    initials: list[str] = []
    for token in tokens:
        if token in explicit_tokens and len(token) > 1:
            initials.extend(token)
        else:
            initials.append(token[0])
    return initials


def _names_compatible_on_exact_profile(
    left: CanonicalPIRecord,
    right: CanonicalPIRecord,
) -> bool:
    """Allow conservative initial/full-name aliases behind one exact profile.

    This deliberately is *not* the general name matcher.  Callers must first
    establish the same non-aggregate official profile URL.  Compact equality
    covers punctuation, spacing and CJK spacing variants.  Initial/full-name
    matching additionally requires a shared complete token (normally the
    surname), then compares the remaining initials in order.
    """

    if _names_compatible(left, right):
        return True
    left_names = [left.display_name, *(left.aliases or [])]
    right_names = [right.display_name, *(right.aliases or [])]
    for left_name in left_names:
        for right_name in right_names:
            left_tokens = _exact_profile_name_tokens(left_name)
            right_tokens = _exact_profile_name_tokens(right_name)
            left_explicit_initials = _exact_profile_explicit_initial_tokens(left_name)
            right_explicit_initials = _exact_profile_explicit_initial_tokens(right_name)
            if not left_tokens or not right_tokens:
                continue
            if "".join(left_tokens) == "".join(right_tokens):
                return True

            shared_complete = {
                token for token in left_tokens if len(token) > 1
            }.intersection(token for token in right_tokens if len(token) > 1)
            if not shared_complete:
                continue
            left_remaining = list(left_tokens)
            right_remaining = list(right_tokens)
            for token in shared_complete:
                if token in left_remaining:
                    left_remaining.remove(token)
                if token in right_remaining:
                    right_remaining.remove(token)
            if not left_remaining or not right_remaining:
                continue

            left_initials = _ordered_name_initials(left_remaining, left_explicit_initials)
            right_initials = _ordered_name_initials(right_remaining, right_explicit_initials)
            shorter, longer = sorted(
                (left_initials, right_initials),
                key=lambda values: (len(values), values),
            )
            has_explicit_initial = any(
                token in left_explicit_initials or token in right_explicit_initials
                for token in [*left_remaining, *right_remaining]
            )
            if has_explicit_initial and _ordered_subsequence(shorter, longer):
                return True

            # A documented short form such as Jeff/Jeffrey is acceptable only
            # behind the same exact profile and a shared full surname.
            for left_token in left_remaining:
                for right_token in right_remaining:
                    shorter_token, longer_token = sorted(
                        (left_token, right_token), key=lambda token: (len(token), token)
                    )
                    if len(shorter_token) >= 3 and longer_token.startswith(shorter_token):
                        return True
    return False


def _safe_email_identities(record: CanonicalPIRecord) -> set[str]:
    # Only an explicitly person-local association is strong enough for entity
    # resolution.  Ambiguous/shared-block addresses may still be retained as
    # evidence, but must never nominate a merge candidate.
    if record.email_association != "person_local":
        return set()
    safe: set[str] = set()
    for email in record.emails:
        normalized = email.strip().casefold()
        if "@" not in normalized:
            continue
        local = normalized.split("@", 1)[0]
        if any(marker in local for marker in _ROLE_EMAIL_MARKERS):
            continue
        safe.add(normalized)
    return safe


def _department_identities(record: CanonicalPIRecord) -> set[str]:
    values = [record.department, *(getattr(record, "departments", None) or [])]
    identities: set[str] = set()
    for value in values:
        for item in str(value or "").split(";"):
            normalized = _normalize_key_part(item)
            if normalized:
                identities.add(normalized)
    return identities


def _person_profile_identities(record: CanonicalPIRecord) -> set[str]:
    return {
        normalized
        for value in _record_profile_urls(record)
        if _is_person_specific_profile_url(value)
        for normalized in [normalize_profile_url(value)]
        if normalized
    }


def _has_separating_official_identity_conflict(
    left: CanonicalPIRecord,
    right: CanonicalPIRecord,
) -> bool:
    """Reject weak merges contradicted by at least two official anchors.

    Department moves, additional profile pages, and email changes each occur for
    real people, so no single difference is decisive.  Two disjoint, populated
    identity families are sufficient to require review instead of automatically
    combining same-name records.  Strong shared external identifiers are tested
    before this guard and can still establish identity directly.
    """

    left_emails = _safe_email_identities(left)
    right_emails = _safe_email_identities(right)
    left_profiles = _person_profile_identities(left)
    right_profiles = _person_profile_identities(right)
    left_departments = _department_identities(left)
    right_departments = _department_identities(right)
    conflicts = (
        bool(left_emails and right_emails and left_emails.isdisjoint(right_emails)),
        bool(
            left_profiles
            and right_profiles
            and left_profiles.isdisjoint(right_profiles)
        ),
        bool(
            left_departments
            and right_departments
            and left_departments.isdisjoint(right_departments)
        ),
    )
    return sum(conflicts) >= 2


def _identity_index_entries(record: CanonicalPIRecord) -> set[tuple[str, str]]:
    """Materialize only identities that can safely nominate a dedupe candidate.

    The sentinel makes an identity-free record distinguishable from a row copied
    into ``canonical_pi_records`` without going through :meth:`upsert_pi_record`.
    Candidate matches are still rechecked against the canonical JSON record, so
    this index narrows work without weakening any identity rule.
    """
    entries = {_IDENTITY_INDEX_SENTINEL}
    for kind, values in _strong_external_identities(record).items():
        entries.update((f"external:{kind}", value) for value in values)
    entries.update(("email", value) for value in _safe_email_identities(record))
    for value in _record_profile_urls(record):
        if _is_person_specific_profile_url(value) and _profile_mentions_record_name(value, record):
            normalized = normalize_profile_url(value)
            if normalized:
                entries.add(("profile", normalized))
        if _is_official_exact_profile_candidate(value, record):
            normalized = normalize_profile_url(value)
            if normalized:
                entries.add(("profile_exact", normalized))
        slug = _profile_slug(value)
        if slug:
            entries.add(("profile_slug", slug))
    return entries


def dedupe_key_for_record(record: CanonicalPIRecord) -> str:
    emails = ",".join(sorted(e.lower() for e in record.emails))
    external = json.dumps(record.external_ids or {}, sort_keys=True)
    return "|".join(
        [
            _normalize_key_part(record.institution_id),
            _normalize_key_part(record.display_name),
            _normalize_key_part(record.profile_url),
            _normalize_key_part(emails),
            _normalize_key_part(external),
        ]
    )


class PIIndexStorage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # One storage instance is one default queue worker.  Keeping tokens in
        # memory preserves the original ``finish(queue_id, ...)`` call shape
        # while still preventing a different process/connection from
        # completing a lease it does not own.
        self._vector_claim_owner = f"storage-{uuid.uuid4().hex}"
        self._vector_claim_tokens: dict[int, str] = {}
        # Long institutional runs may be observed by read-only audit tooling.
        # Wait for short-lived readers instead of aborting a multi-hour crawl.
        self.conn = sqlite3.connect(self.db_path, timeout=30.0)
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.row_factory = sqlite3.Row
        self._identity_index_checked_institutions: set[str] = set()
        self.conn.create_function(
            "publication_is_meaningful",
            5,
            _sqlite_publication_is_meaningful,
            deterministic=True,
        )
        self.init_db()

    def close(self) -> None:
        self.conn.close()

    def init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS institutions (
                institution_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                aliases_json TEXT NOT NULL,
                country TEXT,
                region TEXT,
                ror_id TEXT,
                homepage_url TEXT,
                official_domains_json TEXT NOT NULL,
                qs_rank INTEGER,
                qs_year INTEGER,
                source TEXT,
                status TEXT,
                record_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS raw_sources (
                source_url TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                source_type TEXT,
                http_status INTEGER,
                content_hash TEXT,
                parser_used TEXT,
                crawl_method TEXT,
                error_reason TEXT,
                record_json TEXT NOT NULL,
                PRIMARY KEY (source_url, institution_id, fetched_at)
            );

            CREATE TABLE IF NOT EXISTS person_evidence (
                evidence_id TEXT PRIMARY KEY,
                person_temp_id TEXT,
                institution_id TEXT,
                field_name TEXT,
                field_value TEXT,
                source_url TEXT,
                source_type TEXT,
                extraction_method TEXT,
                extracted_at TEXT,
                confidence REAL,
                evidence_text TEXT,
                content_hash TEXT,
                record_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS canonical_pi_records (
                person_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                institution_name TEXT NOT NULL,
                title TEXT,
                department TEXT,
                profile_url TEXT,
                emails_json TEXT NOT NULL,
                research_areas_json TEXT NOT NULL,
                contact_confidence TEXT,
                topic_match_confidence TEXT,
                current_affiliation_confidence TEXT,
                dedupe_key TEXT,
                record_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS email_evidence (
                email TEXT NOT NULL,
                source_url TEXT NOT NULL,
                person_id TEXT,
                source_type TEXT,
                domain_aligned INTEGER,
                official_source INTEGER,
                extracted_at TEXT,
                confidence REAL,
                verdict TEXT,
                association TEXT,
                record_json TEXT NOT NULL,
                PRIMARY KEY (email, source_url, person_id)
            );

            CREATE TABLE IF NOT EXISTS contact_verdicts (
                person_id TEXT PRIMARY KEY,
                verdict TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                recommended_action TEXT,
                last_live_checked_at TEXT,
                contact_confidence TEXT,
                topic_match_confidence TEXT,
                current_affiliation_confidence TEXT,
                record_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crawl_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                institution_id TEXT,
                source_url TEXT,
                stage TEXT,
                reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS match_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                applicant_source TEXT,
                person_id TEXT,
                display_name TEXT,
                institution_name TEXT,
                match_score REAL,
                institution_fit_score REAL,
                research_fit_score REAL,
                topic_score REAL,
                contact_score REAL,
                institution_score REAL,
                total_score REAL,
                topic_overlap TEXT,
                contact_verdict TEXT,
                explanation TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ingestion_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                institution_id TEXT,
                institution_name TEXT,
                config_name TEXT,
                pages_attempted INTEGER,
                pages_successfully_fetched INTEGER,
                pages_failed INTEGER,
                people_extracted INTEGER,
                emails_extracted INTEGER,
                status TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS duplicates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                institution_id TEXT,
                group_key TEXT,
                kept_person_id TEXT,
                duplicate_person_id TEXT,
                reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS parse_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                institution_id TEXT,
                source_url TEXT,
                parser_name TEXT,
                candidate_blocks INTEGER,
                people_extracted INTEGER,
                filtered_blocks INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pi_observations (
                observation_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                record_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS official_publication_fingerprints (
                fingerprint_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                title TEXT NOT NULL,
                citation_text TEXT NOT NULL,
                publication_year INTEGER,
                doi TEXT,
                publication_url TEXT,
                source_url TEXT NOT NULL,
                confidence REAL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_seen_run_id TEXT NOT NULL,
                record_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS publication_refresh_runs (
                run_id TEXT PRIMARY KEY,
                institution_id TEXT,
                source_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                error_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS official_publication_refresh_state (
                person_id TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                final_url TEXT,
                etag TEXT,
                last_modified TEXT,
                body_sha256 TEXT,
                checked_at TEXT NOT NULL,
                changed_at TEXT,
                last_success_at TEXT,
                parser_name TEXT,
                parser_version TEXT,
                config_hash TEXT,
                parse_status TEXT NOT NULL,
                parse_complete INTEGER NOT NULL DEFAULT 0,
                publication_count INTEGER NOT NULL DEFAULT 0,
                last_run_id TEXT,
                error_reason TEXT,
                record_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (person_id, source_kind, source_url)
            );

            CREATE TABLE IF NOT EXISTS official_publication_source_claims (
                fingerprint_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                claim_status TEXT NOT NULL DEFAULT 'active',
                missing_streak INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_seen_run_id TEXT NOT NULL,
                last_checked_at TEXT NOT NULL,
                tombstoned_at TEXT,
                record_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (fingerprint_id, source_kind, source_url)
            );

            CREATE TABLE IF NOT EXISTS vector_dirty_queue (
                queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_kind TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                person_id TEXT,
                fingerprint_id TEXT,
                reason TEXT NOT NULL,
                run_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                claim_token TEXT,
                claim_owner TEXT,
                lease_expires_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                processed_at TEXT,
                last_error TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS openalex_sync_runs (
                run_id TEXT PRIMARY KEY,
                institution_id TEXT,
                sync_mode TEXT NOT NULL,
                full_snapshot INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                error_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS openalex_author_links (
                person_id TEXT PRIMARY KEY,
                institution_id TEXT NOT NULL,
                openalex_author_id TEXT,
                link_status TEXT NOT NULL DEFAULT 'confirmed',
                confidence REAL,
                match_method TEXT,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                first_linked_at TEXT NOT NULL,
                last_verified_at TEXT NOT NULL,
                last_successful_sync_at TEXT,
                last_full_sync_at TEXT,
                works_updated_through TEXT,
                last_sync_run_id TEXT,
                record_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS openalex_author_link_archives (
                archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                openalex_author_id TEXT NOT NULL,
                link_status TEXT NOT NULL,
                match_method TEXT,
                archived_at TEXT NOT NULL,
                archived_run_id TEXT NOT NULL,
                archive_reason TEXT NOT NULL,
                replacement_manifest_sha256 TEXT NOT NULL,
                original_link_sha256 TEXT NOT NULL,
                original_link_json TEXT NOT NULL,
                replacement_evidence_json TEXT NOT NULL,
                UNIQUE(person_id, archived_run_id)
            );

            CREATE TABLE IF NOT EXISTS openalex_identity_probe_cache (
                probe_key TEXT PRIMARY KEY,
                probe_version TEXT NOT NULL,
                evidence_kind TEXT NOT NULL,
                evidence_value TEXT NOT NULL,
                result_status TEXT NOT NULL,
                works_json TEXT NOT NULL DEFAULT '[]',
                works_sha256 TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_run_id TEXT,
                record_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS openalex_works (
                openalex_work_id TEXT PRIMARY KEY,
                doi TEXT,
                title TEXT NOT NULL DEFAULT '',
                abstract_text TEXT NOT NULL DEFAULT '',
                publication_year INTEGER,
                publication_date TEXT,
                updated_date TEXT,
                work_type TEXT,
                language TEXT,
                topics_json TEXT NOT NULL DEFAULT '[]',
                vector_text TEXT NOT NULL DEFAULT '',
                vector_text_hash TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_seen_run_id TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS openalex_person_works (
                person_id TEXT NOT NULL,
                openalex_work_id TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                openalex_author_id TEXT NOT NULL,
                relationship_status TEXT NOT NULL DEFAULT 'active',
                missing_streak INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_seen_run_id TEXT NOT NULL,
                last_checked_at TEXT NOT NULL,
                tombstoned_at TEXT,
                record_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (person_id, openalex_work_id)
            );

            CREATE TABLE IF NOT EXISTS openalex_work_vectors (
                openalex_work_id TEXT NOT NULL,
                representation TEXT NOT NULL,
                encoder_id TEXT NOT NULL,
                feature_limit INTEGER NOT NULL,
                vector_json TEXT NOT NULL,
                feature_count INTEGER NOT NULL,
                vector_hash TEXT NOT NULL,
                source_text_hash TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                record_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (openalex_work_id, representation)
            );

            CREATE TABLE IF NOT EXISTS pi_career_vectors (
                person_id TEXT NOT NULL,
                representation TEXT NOT NULL,
                encoder_id TEXT NOT NULL,
                feature_limit INTEGER NOT NULL,
                vector_json TEXT NOT NULL,
                feature_count INTEGER NOT NULL,
                vector_hash TEXT NOT NULL,
                dependency_hash TEXT NOT NULL,
                work_count INTEGER NOT NULL,
                nonempty_work_count INTEGER NOT NULL,
                generated_at TEXT NOT NULL,
                record_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (person_id, representation)
            );

            CREATE TABLE IF NOT EXISTS pi_identity_aliases (
                alias_person_id TEXT PRIMARY KEY,
                canonical_person_id TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_seen_run_id TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pi_identity_keys (
                institution_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                identity_kind TEXT NOT NULL,
                identity_value TEXT NOT NULL,
                PRIMARY KEY (institution_id, person_id, identity_kind, identity_value)
            );

            CREATE INDEX IF NOT EXISTS idx_raw_sources_cache
                ON raw_sources(institution_id, source_url, fetched_at DESC);
            CREATE INDEX IF NOT EXISTS idx_pi_observations_run
                ON pi_observations(institution_id, run_id);
            CREATE INDEX IF NOT EXISTS idx_publication_fingerprints_person
                ON official_publication_fingerprints(person_id);
            CREATE INDEX IF NOT EXISTS idx_publication_refresh_runs_institution
                ON publication_refresh_runs(institution_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_publication_refresh_state_due
                ON official_publication_refresh_state(institution_id, checked_at, parse_status);
            CREATE INDEX IF NOT EXISTS idx_publication_source_claims_person
                ON official_publication_source_claims(person_id, claim_status);
            CREATE INDEX IF NOT EXISTS idx_publication_source_claims_source
                ON official_publication_source_claims(person_id, source_kind, source_url, claim_status);
            CREATE INDEX IF NOT EXISTS idx_vector_dirty_queue_pending
                ON vector_dirty_queue(status, created_at, queue_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_vector_dirty_queue_unique_pending
                ON vector_dirty_queue(entity_kind, entity_id)
                WHERE status='pending';
            CREATE INDEX IF NOT EXISTS idx_openalex_sync_runs_institution
                ON openalex_sync_runs(institution_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_openalex_author_links_author
                ON openalex_author_links(openalex_author_id, link_status);
            CREATE INDEX IF NOT EXISTS idx_openalex_author_link_archives_person
                ON openalex_author_link_archives(person_id, archived_at DESC);
            CREATE INDEX IF NOT EXISTS idx_openalex_identity_probe_expiry
                ON openalex_identity_probe_cache(expires_at);
            CREATE INDEX IF NOT EXISTS idx_openalex_works_updated
                ON openalex_works(updated_date, openalex_work_id);
            CREATE INDEX IF NOT EXISTS idx_openalex_person_works_current
                ON openalex_person_works(person_id, relationship_status, openalex_work_id);
            CREATE INDEX IF NOT EXISTS idx_openalex_person_works_work
                ON openalex_person_works(openalex_work_id, relationship_status, person_id);
            CREATE INDEX IF NOT EXISTS idx_openalex_work_vectors_encoder
                ON openalex_work_vectors(representation, encoder_id, openalex_work_id);
            CREATE INDEX IF NOT EXISTS idx_pi_career_vectors_encoder
                ON pi_career_vectors(representation, encoder_id, person_id);
            CREATE INDEX IF NOT EXISTS idx_pi_identity_aliases_canonical
                ON pi_identity_aliases(canonical_person_id);
            CREATE INDEX IF NOT EXISTS idx_pi_identity_keys_lookup
                ON pi_identity_keys(institution_id, identity_kind, identity_value, person_id);
            CREATE INDEX IF NOT EXISTS idx_pi_identity_keys_person
                ON pi_identity_keys(person_id);
            """
        )
        self._ensure_schema_columns()
        self._migrate_canonical_records_v2()
        self._migrate_publication_refresh_v1()
        self._migrate_openalex_sync_v1()
        self._migrate_openalex_person_works_nullable_author()
        identity_index_version = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='pi_identity_index_schema'"
        ).fetchone()
        rebuild_identity_index = (
            identity_index_version is None
            or identity_index_version["value"] != _IDENTITY_INDEX_SCHEMA_VERSION
        )
        self.sync_identity_index(rebuild=rebuild_identity_index)
        if rebuild_identity_index:
            self.conn.execute(
                """
                INSERT INTO schema_meta (key, value)
                VALUES ('pi_identity_index_schema', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (_IDENTITY_INDEX_SCHEMA_VERSION,),
            )
        self.conn.commit()

    def _migrate_openalex_person_works_nullable_author(self) -> None:
        columns = {
            row["name"]: row
            for row in self.conn.execute("PRAGMA table_info(openalex_person_works)")
        }
        author_column = columns.get("openalex_author_id")
        if author_column is None or not int(author_column["notnull"] or 0):
            return
        with self.conn:
            self.conn.execute("ALTER TABLE openalex_person_works RENAME TO openalex_person_works_v1")
            self.conn.execute(
                """
                CREATE TABLE openalex_person_works (
                    person_id TEXT NOT NULL,
                    openalex_work_id TEXT NOT NULL,
                    institution_id TEXT NOT NULL,
                    openalex_author_id TEXT,
                    relationship_status TEXT NOT NULL DEFAULT 'active',
                    missing_streak INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_seen_run_id TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL,
                    tombstoned_at TEXT,
                    record_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (person_id, openalex_work_id)
                )
                """
            )
            self.conn.execute(
                """
                INSERT INTO openalex_person_works
                SELECT person_id, openalex_work_id, institution_id,
                       NULLIF(openalex_author_id, ''), relationship_status,
                       missing_streak, first_seen_at, last_seen_at,
                       last_seen_run_id, last_checked_at, tombstoned_at,
                       record_json
                FROM openalex_person_works_v1
                """
            )
            self.conn.execute("DROP TABLE openalex_person_works_v1")
            self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_openalex_person_works_current
                ON openalex_person_works(person_id, relationship_status, openalex_work_id)
                """
            )
            self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_openalex_person_works_work
                ON openalex_person_works(openalex_work_id, relationship_status, person_id)
                """
            )

    def _ensure_schema_columns(self) -> None:
        columns: dict[str, list[tuple[str, str]]] = {
            "canonical_pi_records": [
                ("contact_confidence", "TEXT"),
                ("topic_match_confidence", "TEXT"),
                ("current_affiliation_confidence", "TEXT"),
                ("dedupe_key", "TEXT"),
                ("first_seen_at", "TEXT"),
                ("last_seen_at", "TEXT"),
                ("last_seen_run_id", "TEXT"),
                ("membership_status", "TEXT DEFAULT 'active'"),
                ("missing_streak", "INTEGER DEFAULT 0"),
                ("pool_scope", "TEXT"),
                ("schema_version", "INTEGER DEFAULT 2"),
            ],
            "raw_sources": [
                ("run_id", "TEXT"),
                ("final_url", "TEXT"),
                ("content_type", "TEXT"),
                ("encoding", "TEXT"),
                ("etag", "TEXT"),
                ("last_modified", "TEXT"),
                ("archive_key", "TEXT"),
                ("body_sha256", "TEXT"),
                ("uncompressed_bytes", "INTEGER DEFAULT 0"),
                ("compressed_bytes", "INTEGER DEFAULT 0"),
                ("network_bytes", "INTEGER DEFAULT 0"),
                ("not_modified", "INTEGER DEFAULT 0"),
            ],
            "person_evidence": [
                ("run_id", "TEXT"),
            ],
            "email_evidence": [
                ("person_id", "TEXT"),
                ("association", "TEXT"),
                ("run_id", "TEXT"),
            ],
            "contact_verdicts": [
                ("contact_confidence", "TEXT"),
                ("topic_match_confidence", "TEXT"),
                ("current_affiliation_confidence", "TEXT"),
                ("run_id", "TEXT"),
            ],
            "crawl_errors": [("run_id", "TEXT")],
            "duplicates": [("run_id", "TEXT")],
            "parse_metrics": [("run_id", "TEXT")],
            "ingestion_runs": [
                ("run_id", "TEXT"),
                ("config_sha256", "TEXT"),
                ("pool_scope", "TEXT"),
                ("metrics_json", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("crawl_complete", "INTEGER DEFAULT 0"),
            ],
            "match_results": [
                ("institution_fit_score", "REAL"),
                ("research_fit_score", "REAL"),
                ("topic_score", "REAL"),
                ("contact_score", "REAL"),
                ("institution_score", "REAL"),
                ("total_score", "REAL"),
            ],
            "openalex_sync_runs": [
                ("institution_id", "TEXT"),
                ("sync_mode", "TEXT DEFAULT 'delta'"),
                ("full_snapshot", "INTEGER DEFAULT 0"),
                ("status", "TEXT DEFAULT 'running'"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("metrics_json", "TEXT DEFAULT '{}'"),
                ("error_reason", "TEXT"),
            ],
            "openalex_author_links": [
                ("institution_id", "TEXT"),
                ("openalex_author_id", "TEXT"),
                ("link_status", "TEXT DEFAULT 'confirmed'"),
                ("confidence", "REAL"),
                ("match_method", "TEXT"),
                ("evidence_json", "TEXT DEFAULT '{}'"),
                ("first_linked_at", "TEXT"),
                ("last_verified_at", "TEXT"),
                ("last_successful_sync_at", "TEXT"),
                ("last_full_sync_at", "TEXT"),
                ("works_updated_through", "TEXT"),
                ("last_sync_run_id", "TEXT"),
                ("record_json", "TEXT DEFAULT '{}'"),
            ],
            "openalex_works": [
                ("doi", "TEXT"),
                ("title", "TEXT DEFAULT ''"),
                ("abstract_text", "TEXT DEFAULT ''"),
                ("publication_year", "INTEGER"),
                ("publication_date", "TEXT"),
                ("updated_date", "TEXT"),
                ("work_type", "TEXT"),
                ("language", "TEXT"),
                ("topics_json", "TEXT DEFAULT '[]'"),
                ("vector_text", "TEXT DEFAULT ''"),
                ("vector_text_hash", "TEXT DEFAULT ''"),
                ("raw_json", "TEXT DEFAULT '{}'"),
                ("first_seen_at", "TEXT"),
                ("last_seen_at", "TEXT"),
                ("last_seen_run_id", "TEXT"),
            ],
            "openalex_person_works": [
                ("institution_id", "TEXT"),
                ("openalex_author_id", "TEXT"),
                ("relationship_status", "TEXT DEFAULT 'active'"),
                ("missing_streak", "INTEGER DEFAULT 0"),
                ("first_seen_at", "TEXT"),
                ("last_seen_at", "TEXT"),
                ("last_seen_run_id", "TEXT"),
                ("last_checked_at", "TEXT"),
                ("tombstoned_at", "TEXT"),
                ("record_json", "TEXT DEFAULT '{}'"),
            ],
            "vector_dirty_queue": [
                ("claim_token", "TEXT"),
                ("claim_owner", "TEXT"),
                ("lease_expires_at", "TEXT"),
            ],
        }
        for table, desired in columns.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, type_name in desired:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {type_name}")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ingestion_runs_run_id ON ingestion_runs(run_id) WHERE run_id IS NOT NULL"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pi_records_membership ON canonical_pi_records(institution_id, membership_status)"
        )
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_vector_dirty_queue_claim_token
            ON vector_dirty_queue(claim_token)
            WHERE claim_token IS NOT NULL
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vector_dirty_queue_lease
            ON vector_dirty_queue(status, lease_expires_at, created_at, queue_id)
            """
        )

    def _migrate_canonical_records_v2(self) -> None:
        marker = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='canonical_pi_record_schema'"
        ).fetchone()
        if marker and marker["value"] == "2-neutral-v1":
            return
        rows = self.conn.execute(
            """
            SELECT person_id, first_seen_at, last_seen_at, last_seen_run_id,
                   membership_status, missing_streak, pool_scope, record_json
            FROM canonical_pi_records
            """
        ).fetchall()
        for row in rows:
            data = json.loads(row["record_json"])
            for key in _LEGACY_SUPERVISION_KEYS:
                data.pop(key, None)
            first_seen_at = data.get("first_seen_at") or row["first_seen_at"] or data.get("last_checked_at")
            last_seen_at = data.get("last_seen_at") or row["last_seen_at"] or data.get("last_checked_at")
            last_seen_run_id = data.get("last_seen_run_id") or row["last_seen_run_id"]
            membership_status = row["membership_status"] or data.get("membership_status") or "active"
            missing_streak = int(row["missing_streak"] or data.get("missing_streak") or 0)
            pool_scope = data.get("pool_scope") or row["pool_scope"]
            data.update(
                {
                    "first_seen_at": first_seen_at,
                    "last_seen_at": last_seen_at,
                    "last_seen_run_id": last_seen_run_id,
                    "membership_status": membership_status,
                    "missing_streak": missing_streak,
                    "pool_scope": pool_scope,
                    "schema_version": 2,
                }
            )
            self.conn.execute(
                """
                UPDATE canonical_pi_records
                SET first_seen_at=?, last_seen_at=?, last_seen_run_id=?,
                    membership_status=?, missing_streak=?, pool_scope=?,
                    schema_version=2, record_json=?
                WHERE person_id=?
                """,
                (
                    first_seen_at,
                    last_seen_at,
                    last_seen_run_id,
                    membership_status,
                    missing_streak,
                    pool_scope,
                    _json(data),
                    row["person_id"],
                ),
            )
        verdict_rows = self.conn.execute(
            "SELECT person_id, record_json FROM contact_verdicts"
        ).fetchall()
        for row in verdict_rows:
            data = json.loads(row["record_json"])
            for key in _LEGACY_SUPERVISION_KEYS:
                data.pop(key, None)
            self.conn.execute(
                "UPDATE contact_verdicts SET record_json=? WHERE person_id=?",
                (_json(data), row["person_id"]),
            )
        self.conn.execute(
            """
            INSERT INTO schema_meta (key, value) VALUES ('canonical_pi_record_schema', '2-neutral-v1')
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """
        )

    def _migrate_publication_refresh_v1(self) -> None:
        """Add source-claim lifecycle state without hiding legacy publications.

        Historical fingerprints are backfilled as active claims, but their
        source state is deliberately marked ``legacy_imported`` and incomplete.
        A later refresh therefore has provenance to compare while still needing
        a complete, successful parse before absence can advance a tombstone.
        The schema marker makes the potentially large backfill idempotent.
        """

        marker = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='publication_refresh_schema'"
        ).fetchone()
        if marker and marker["value"] == _PUBLICATION_REFRESH_SCHEMA_VERSION:
            return

        self.conn.execute(
            """
            INSERT OR IGNORE INTO official_publication_source_claims
            (fingerprint_id, person_id, institution_id, source_url, source_kind,
             claim_status, missing_streak, first_seen_at, last_seen_at,
             last_seen_run_id, last_checked_at, tombstoned_at, record_json)
            SELECT fingerprint_id, person_id, institution_id, source_url,
                   'official_profile', 'active', 0, first_seen_at, last_seen_at,
                   last_seen_run_id, last_seen_at, NULL, '{}'
            FROM official_publication_fingerprints
            """
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO official_publication_refresh_state
            (person_id, institution_id, source_url, source_kind, final_url,
             etag, last_modified, body_sha256, checked_at, changed_at,
             last_success_at, parser_name, parser_version, config_hash,
             parse_status, parse_complete, publication_count, last_run_id,
             error_reason, record_json)
            SELECT person_id, institution_id, source_url, 'official_profile',
                   source_url, NULL, NULL, NULL, MAX(last_seen_at), NULL,
                   MAX(last_seen_at), NULL, NULL, NULL, 'legacy_imported', 0,
                   COUNT(*), MAX(last_seen_run_id), NULL, '{}'
            FROM official_publication_fingerprints
            GROUP BY person_id, institution_id, source_url
            """
        )
        self.conn.execute(
            """
            INSERT INTO schema_meta (key, value)
            VALUES ('publication_refresh_schema', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (_PUBLICATION_REFRESH_SCHEMA_VERSION,),
        )

    def _migrate_openalex_sync_v1(self) -> None:
        """Backfill derived OpenAlex state and mark the additive schema applied."""

        marker = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='openalex_sync_schema'"
        ).fetchone()
        if marker and marker["value"] == _OPENALEX_SYNC_SCHEMA_VERSION:
            return

        now = utc_now_iso()
        author_rows = self.conn.execute(
            "SELECT person_id, evidence_json, record_json FROM openalex_author_links"
        ).fetchall()
        for row in author_rows:
            evidence = _loads(row["evidence_json"], {})
            payload = _loads(row["record_json"], {})
            payload.update({"person_id": row["person_id"], "evidence": evidence})
            self.conn.execute(
                """
                UPDATE openalex_author_links
                SET link_status=COALESCE(NULLIF(link_status, ''), 'confirmed'),
                    evidence_json=COALESCE(NULLIF(evidence_json, ''), '{}'),
                    first_linked_at=COALESCE(first_linked_at, ?),
                    last_verified_at=COALESCE(last_verified_at, first_linked_at, ?),
                    record_json=?
                WHERE person_id=?
                """,
                (now, now, _json(payload), row["person_id"]),
            )

        work_rows = self.conn.execute(
            "SELECT * FROM openalex_works"
        ).fetchall()
        for row in work_rows:
            raw = _loads(row["raw_json"], {})
            if not raw:
                raw = {
                    "id": row["openalex_work_id"],
                    "doi": row["doi"],
                    "title": row["title"],
                    "abstract_text": row["abstract_text"],
                    "publication_year": row["publication_year"],
                    "publication_date": row["publication_date"],
                    "updated_date": row["updated_date"],
                    "type": row["work_type"],
                    "language": row["language"],
                    "topics": _loads(row["topics_json"], []),
                }
            vector_text, abstract_text, topics = _openalex_work_vector_text(raw)
            vector_hash = hashlib.sha256(vector_text.encode("utf-8")).hexdigest()
            self.conn.execute(
                """
                UPDATE openalex_works
                SET title=COALESCE(title, ''),
                    abstract_text=COALESCE(NULLIF(abstract_text, ''), ?),
                    topics_json=CASE WHEN topics_json IS NULL OR topics_json=''
                                     THEN ? ELSE topics_json END,
                    vector_text=CASE WHEN vector_text IS NULL OR vector_text=''
                                     THEN ? ELSE vector_text END,
                    vector_text_hash=CASE WHEN vector_text_hash IS NULL OR vector_text_hash=''
                                          THEN ? ELSE vector_text_hash END,
                    raw_json=?,
                    first_seen_at=COALESCE(first_seen_at, last_seen_at, ?),
                    last_seen_at=COALESCE(last_seen_at, first_seen_at, ?),
                    last_seen_run_id=COALESCE(last_seen_run_id, 'legacy_openalex_import')
                WHERE openalex_work_id=?
                """,
                (
                    abstract_text,
                    _json(topics),
                    vector_text,
                    vector_hash,
                    _json(raw),
                    now,
                    now,
                    row["openalex_work_id"],
                ),
            )

        self.conn.execute(
            """
            UPDATE openalex_person_works
            SET relationship_status=COALESCE(NULLIF(relationship_status, ''), 'active'),
                missing_streak=COALESCE(missing_streak, 0),
                first_seen_at=COALESCE(first_seen_at, last_seen_at, ?),
                last_seen_at=COALESCE(last_seen_at, first_seen_at, ?),
                last_seen_run_id=COALESCE(last_seen_run_id, 'legacy_openalex_import'),
                last_checked_at=COALESCE(last_checked_at, last_seen_at, first_seen_at, ?),
                record_json=COALESCE(NULLIF(record_json, ''), '{}')
            """,
            (now, now, now),
        )
        self.conn.execute(
            """
            INSERT INTO schema_meta (key, value)
            VALUES ('openalex_sync_schema', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (_OPENALEX_SYNC_SCHEMA_VERSION,),
        )

    @staticmethod
    def _identity_record_from_row(row: sqlite3.Row) -> CanonicalPIRecord | None:
        """Read a canonical row, tolerating sparse legacy/test JSON payloads."""
        try:
            return _pi_from_json(row["record_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            try:
                data = _loads(row["record_json"], {})
                emails = _loads(row["emails_json"], [])
                research_areas = _loads(row["research_areas_json"], [])
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            data.update(
                {
                    "person_id": data.get("person_id") or row["person_id"],
                    "display_name": data.get("display_name") or row["display_name"],
                    "institution_id": data.get("institution_id") or row["institution_id"],
                    "institution_name": data.get("institution_name") or row["institution_name"],
                    "profile_url": data.get("profile_url") or row["profile_url"],
                    "emails": data.get("emails") or emails,
                    "research_areas": data.get("research_areas") or research_areas,
                }
            )
            defaults: dict[str, Any] = {
                "given_name": None,
                "family_name": None,
                "aliases": [],
                "ror_id": None,
                "department": row["department"],
                "title": row["title"],
                "lab_url": None,
                "publications_summary": {},
                "external_ids": {},
                "source_evidence_ids": [],
                "last_checked_at": row["updated_at"] or utc_now_iso(),
                "profile_urls": [],
            }
            for key, value in defaults.items():
                data.setdefault(key, value)
            try:
                return _pi_from_json(data)
            except (TypeError, ValueError):
                return None

    def _replace_identity_keys(
        self,
        institution_id: str,
        person_id: str,
        entries: set[tuple[str, str]],
    ) -> None:
        self.conn.execute("DELETE FROM pi_identity_keys WHERE person_id=?", (person_id,))
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO pi_identity_keys
            (institution_id, person_id, identity_kind, identity_value)
            VALUES (?, ?, ?, ?)
            """,
            [
                (institution_id, person_id, identity_kind, identity_value)
                for identity_kind, identity_value in sorted(entries)
            ],
        )

    def sync_identity_index(
        self,
        institution_id: str | None = None,
        *,
        rebuild: bool = False,
    ) -> int:
        """Incrementally index canonical rows that bypassed normal upserts.

        Shard merging deliberately copies canonical rows with SQL.  The sentinel
        anti-join below makes that path safe even when the target database was
        initialized while empty; records with no usable identity still receive a
        sentinel and therefore are not repeatedly deserialized.
        """
        if rebuild:
            if institution_id is None:
                self.conn.execute("DELETE FROM pi_identity_keys")
                self._identity_index_checked_institutions.clear()
            else:
                self.conn.execute(
                    "DELETE FROM pi_identity_keys WHERE institution_id=?",
                    (institution_id,),
                )
                self._identity_index_checked_institutions.discard(institution_id)
        params: list[str] = [_IDENTITY_INDEX_SENTINEL[0], _IDENTITY_INDEX_SENTINEL[1]]
        institution_filter = ""
        if institution_id is not None:
            institution_filter = " AND c.institution_id=?"
            params.append(institution_id)
        rows = self.conn.execute(
            f"""
            SELECT c.person_id, c.display_name, c.institution_id, c.institution_name,
                   c.title, c.department, c.profile_url, c.emails_json,
                   c.research_areas_json, c.record_json, c.updated_at
            FROM canonical_pi_records AS c
            WHERE NOT EXISTS (
                SELECT 1
                FROM pi_identity_keys AS k
                WHERE k.person_id=c.person_id
                  AND k.identity_kind=? AND k.identity_value=?
            )
            {institution_filter}
            """,
            params,
        ).fetchall()
        with self.conn:
            for row in rows:
                record = self._identity_record_from_row(row)
                entries = (
                    _identity_index_entries(record)
                    if record is not None
                    else {_IDENTITY_INDEX_SENTINEL}
                )
                self._replace_identity_keys(
                    str(row["institution_id"]),
                    str(row["person_id"]),
                    entries,
                )
        if institution_id is not None:
            self._identity_index_checked_institutions.add(institution_id)
        return len(rows)

    def upsert_institution(self, record: InstitutionRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO institutions
            (institution_id, name, aliases_json, country, region, ror_id, homepage_url,
             official_domains_json, qs_rank, qs_year, source, status, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(institution_id) DO UPDATE SET
                name=excluded.name,
                aliases_json=excluded.aliases_json,
                country=excluded.country,
                region=excluded.region,
                ror_id=excluded.ror_id,
                homepage_url=excluded.homepage_url,
                official_domains_json=excluded.official_domains_json,
                qs_rank=excluded.qs_rank,
                qs_year=excluded.qs_year,
                source=excluded.source,
                status=excluded.status,
                record_json=excluded.record_json
            """,
            (
                record.institution_id,
                record.name,
                _json(record.aliases),
                record.country,
                record.region,
                record.ror_id,
                record.homepage_url,
                _json(record.official_domains),
                record.qs_rank,
                record.qs_year,
                record.source,
                record.status,
                record.to_json(),
            ),
        )
        self.conn.commit()

    def insert_raw_source(self, record: RawSourceRecord, *, commit: bool = True) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO raw_sources
            (source_url, institution_id, fetched_at, source_type, http_status,
             content_hash, parser_used, crawl_method, error_reason, run_id, final_url,
             content_type, encoding, etag, last_modified, archive_key, body_sha256,
             uncompressed_bytes, compressed_bytes, network_bytes, not_modified, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.source_url,
                record.institution_id,
                record.fetched_at,
                record.source_type,
                record.http_status,
                record.content_hash,
                record.parser_used,
                record.crawl_method,
                record.error_reason,
                record.run_id,
                record.final_url,
                record.content_type,
                record.encoding,
                record.etag,
                record.last_modified,
                record.archive_key,
                record.body_sha256,
                record.uncompressed_bytes,
                record.compressed_bytes,
                record.network_bytes,
                int(record.not_modified),
                record.to_json(),
            ),
        )
        if commit:
            self.conn.commit()

    def get_latest_raw_source(self, institution_id: str, source_url: str) -> RawSourceRecord | None:
        row = self.conn.execute(
            """
            SELECT record_json
            FROM raw_sources
            WHERE institution_id=? AND source_url=? AND archive_key IS NOT NULL
              AND error_reason IS NULL
              AND (http_status BETWEEN 200 AND 299 OR http_status=304 OR not_modified=1)
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            (institution_id, source_url),
        ).fetchone()
        if not row:
            # RFC percent escapes are hex-case-insensitive.  Historical HKU
            # records contain the same URL once with ``%d3%a7`` and once with
            # ``%D3%A7``; an exact-only lookup makes an otherwise valid archive
            # appear missing during offline replay.  SQL narrows candidates,
            # then Python verifies that *only* escape case differs so genuinely
            # case-sensitive path variants are never conflated.
            normalized = _uppercase_percent_escapes(source_url)
            candidates = self.conn.execute(
                """
                SELECT source_url, record_json
                FROM raw_sources
                WHERE institution_id=? AND lower(source_url)=lower(?)
                  AND archive_key IS NOT NULL
                  AND error_reason IS NULL
                  AND (http_status BETWEEN 200 AND 299 OR http_status=304 OR not_modified=1)
                ORDER BY fetched_at DESC
                """,
                (institution_id, source_url),
            ).fetchall()
            row = next(
                (
                    candidate
                    for candidate in candidates
                    if _uppercase_percent_escapes(candidate["source_url"]) == normalized
                ),
                None,
            )
        if not row:
            return None
        return RawSourceRecord(**json.loads(row["record_json"]))

    def insert_person_evidence(self, record: PersonEvidence) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO person_evidence
            (evidence_id, person_temp_id, institution_id, field_name, field_value,
             source_url, source_type, extraction_method, extracted_at, confidence,
             evidence_text, content_hash, run_id, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.evidence_id,
                record.person_temp_id,
                record.institution_id,
                record.field_name,
                record.field_value,
                record.source_url,
                record.source_type,
                record.extraction_method,
                record.extracted_at,
                record.confidence,
                record.evidence_text,
                record.content_hash,
                record.run_id,
                record.to_json(),
            ),
        )
        self.conn.commit()

    def upsert_pi_record(self, record: CanonicalPIRecord) -> None:
        existing = self.conn.execute(
            "SELECT first_seen_at, record_json FROM canonical_pi_records WHERE person_id=?",
            (record.person_id,),
        ).fetchone()
        if existing:
            existing_json = _loads(existing["record_json"], {})
            record.first_seen_at = (
                existing["first_seen_at"]
                or existing_json.get("first_seen_at")
                or record.first_seen_at
            )
        record.first_seen_at = record.first_seen_at or record.last_seen_at or record.last_checked_at
        record.last_seen_at = record.last_seen_at or record.last_checked_at
        dedupe_key = dedupe_key_for_record(record)
        self.conn.execute(
            """
            INSERT INTO canonical_pi_records
             (person_id, display_name, institution_id, institution_name, title,
             department, profile_url, emails_json, research_areas_json,
             contact_confidence, topic_match_confidence,
             current_affiliation_confidence, dedupe_key,
             first_seen_at, last_seen_at, last_seen_run_id, membership_status,
             missing_streak, pool_scope, schema_version, record_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(person_id) DO UPDATE SET
                display_name=excluded.display_name,
                institution_id=excluded.institution_id,
                institution_name=excluded.institution_name,
                title=excluded.title,
                department=excluded.department,
                profile_url=excluded.profile_url,
                emails_json=excluded.emails_json,
                research_areas_json=excluded.research_areas_json,
                contact_confidence=excluded.contact_confidence,
                topic_match_confidence=excluded.topic_match_confidence,
                current_affiliation_confidence=excluded.current_affiliation_confidence,
                dedupe_key=excluded.dedupe_key,
                first_seen_at=excluded.first_seen_at,
                last_seen_at=excluded.last_seen_at,
                last_seen_run_id=excluded.last_seen_run_id,
                membership_status=excluded.membership_status,
                missing_streak=excluded.missing_streak,
                pool_scope=excluded.pool_scope,
                schema_version=excluded.schema_version,
                record_json=excluded.record_json,
                updated_at=excluded.updated_at
            """,
            (
                record.person_id,
                record.display_name,
                record.institution_id,
                record.institution_name,
                record.title,
                record.department,
                record.profile_url,
                _json(record.emails),
                _json(record.research_areas),
                record.contact_confidence,
                record.topic_match_confidence,
                record.current_affiliation_confidence,
                dedupe_key,
                record.first_seen_at,
                record.last_seen_at,
                record.last_seen_run_id,
                record.membership_status,
                record.missing_streak,
                record.pool_scope,
                record.schema_version,
                record.to_json(),
                utc_now_iso(),
            ),
        )
        self._replace_identity_keys(
            record.institution_id,
            record.person_id,
            _identity_index_entries(record),
        )
        self.conn.commit()

    def insert_pi_observation(
        self,
        record: CanonicalPIRecord,
        run_id: str,
        source_url: str,
        observation: dict[str, Any],
    ) -> str:
        observed_at = utc_now_iso()
        observation_id = stable_id(
            "obs",
            record.person_id,
            run_id,
            source_url,
            _json(observation),
        )
        payload = {
            "observation_id": observation_id,
            "person_id": record.person_id,
            "institution_id": record.institution_id,
            "run_id": run_id,
            "source_url": source_url,
            "observed_at": observed_at,
            "observation": observation,
        }
        self.conn.execute(
            """
            INSERT OR REPLACE INTO pi_observations
            (observation_id, person_id, institution_id, run_id, source_url, observed_at, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                record.person_id,
                record.institution_id,
                run_id,
                source_url,
                observed_at,
                _json(payload),
            ),
        )
        self.conn.commit()
        return observation_id

    def upsert_publication_fingerprint(
        self,
        record: OfficialPublicationFingerprint,
        *,
        commit: bool = True,
    ) -> None:
        row = self.conn.execute(
            "SELECT first_seen_at FROM official_publication_fingerprints WHERE fingerprint_id=?",
            (record.fingerprint_id,),
        ).fetchone()
        if row and row["first_seen_at"]:
            record.first_seen_at = row["first_seen_at"]
        self.conn.execute(
            """
            INSERT INTO official_publication_fingerprints
            (fingerprint_id, person_id, institution_id, title, citation_text,
             publication_year, doi, publication_url, source_url, confidence,
             first_seen_at, last_seen_at, last_seen_run_id, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint_id) DO UPDATE SET
                title=excluded.title,
                citation_text=excluded.citation_text,
                publication_year=excluded.publication_year,
                doi=excluded.doi,
                publication_url=excluded.publication_url,
                source_url=excluded.source_url,
                confidence=excluded.confidence,
                last_seen_at=excluded.last_seen_at,
                last_seen_run_id=excluded.last_seen_run_id,
                record_json=excluded.record_json
            """,
            (
                record.fingerprint_id,
                record.person_id,
                record.institution_id,
                record.title,
                record.citation_text,
                record.publication_year,
                record.doi,
                record.publication_url,
                record.source_url,
                record.confidence,
                record.first_seen_at,
                record.last_seen_at,
                record.last_seen_run_id,
                record.to_json(),
            ),
        )
        if commit:
            self.conn.commit()

    def start_publication_refresh_run(
        self,
        run_id: str,
        institution_id: str | None = None,
        source_kind: str = "official_profile",
        *,
        dry_run: bool = False,
        started_at: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create one auditable publication-maintenance run."""

        if not run_id or not source_kind:
            raise ValueError("run_id and source_kind are required")
        started_at = started_at or utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO publication_refresh_runs
            (run_id, institution_id, source_kind, status, dry_run, started_at,
             finished_at, metrics_json, error_reason)
            VALUES (?, ?, ?, 'running', ?, ?, NULL, ?, NULL)
            """,
            (
                run_id,
                institution_id,
                source_kind,
                int(dry_run),
                started_at,
                _json(metrics or {}),
            ),
        )
        self.conn.commit()
        return self.get_publication_refresh_run(run_id) or {}

    def finish_publication_refresh_run(
        self,
        run_id: str,
        status: str,
        metrics: dict[str, Any] | None = None,
        *,
        error_reason: str | None = None,
        finished_at: str | None = None,
    ) -> dict[str, Any]:
        """Finalize a refresh run while retaining its machine-readable metrics."""

        finished_at = finished_at or utc_now_iso()
        cursor = self.conn.execute(
            """
            UPDATE publication_refresh_runs
            SET status=?, finished_at=?, metrics_json=?, error_reason=?
            WHERE run_id=?
            """,
            (status, finished_at, _json(metrics or {}), error_reason, run_id),
        )
        if cursor.rowcount != 1:
            self.conn.rollback()
            raise KeyError(f"Unknown publication refresh run: {run_id}")
        self.conn.commit()
        return self.get_publication_refresh_run(run_id) or {}

    def get_publication_refresh_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM publication_refresh_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["dry_run"] = bool(result["dry_run"])
        result["metrics"] = _loads(result.pop("metrics_json"), {})
        return result

    def get_publication_refresh_state(
        self,
        person_id: str,
        source_url: str,
        source_kind: str = "official_profile",
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM official_publication_refresh_state
            WHERE person_id=? AND source_kind=? AND source_url=?
            """,
            (person_id, source_kind, source_url),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["parse_complete"] = bool(result["parse_complete"])
        result["record"] = _loads(result.pop("record_json"), {})
        return result

    def publication_refresh_decision(
        self,
        person_id: str,
        source_url: str,
        *,
        source_kind: str = "official_profile",
        body_sha256: str | None,
        parser_name: str | None,
        parser_version: str | None,
        config_hash: str | None,
    ) -> dict[str, Any]:
        """Return whether a captured body needs publication parsing.

        The cache key includes parser and publication-specific configuration,
        so a 304 response can still reparse the archived body after parser code
        changes, while a byte-identical 200 response can skip redundant work.
        """

        state = self.get_publication_refresh_state(person_id, source_url, source_kind)
        if state is None:
            return {"should_parse": True, "reason": "no_baseline"}
        if not state["parse_complete"]:
            return {"should_parse": True, "reason": "incomplete_baseline"}
        parse_key = (parser_name or "", parser_version or "", config_hash or "")
        previous_key = (
            state.get("parser_name") or "",
            state.get("parser_version") or "",
            state.get("config_hash") or "",
        )
        if parse_key != previous_key:
            return {"should_parse": True, "reason": "parser_or_config_changed"}
        if body_sha256 and body_sha256 == state.get("body_sha256"):
            return {"should_parse": False, "reason": "same_body_and_parse_key"}
        return {"should_parse": True, "reason": "body_changed"}

    def upsert_publication_refresh_state(
        self,
        person_id: str,
        institution_id: str,
        source_url: str,
        *,
        source_kind: str = "official_profile",
        final_url: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        body_sha256: str | None = None,
        checked_at: str | None = None,
        changed_at: str | None = None,
        last_success_at: str | None = None,
        parser_name: str | None = None,
        parser_version: str | None = None,
        config_hash: str | None = None,
        parse_status: str = "success",
        parse_complete: bool = False,
        publication_count: int = 0,
        last_run_id: str | None = None,
        error_reason: str | None = None,
        record: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Persist conditional-fetch and parser-baseline state for one source."""

        if not person_id or not institution_id or not source_url or not source_kind:
            raise ValueError("person_id, institution_id, source_url and source_kind are required")
        checked_at = checked_at or utc_now_iso()
        previous = self.get_publication_refresh_state(person_id, source_url, source_kind)
        if changed_at is None:
            if previous is None or (
                body_sha256
                and body_sha256 != previous.get("body_sha256")
            ):
                changed_at = checked_at
            else:
                changed_at = previous.get("changed_at") if previous else None
        if last_success_at is None:
            if parse_status in {"success", "not_modified", "same_hash", "legacy_imported"}:
                last_success_at = checked_at
            elif previous:
                last_success_at = previous.get("last_success_at")
        payload = {
            "person_id": person_id,
            "institution_id": institution_id,
            "source_url": source_url,
            "source_kind": source_kind,
            "final_url": final_url,
            "etag": etag,
            "last_modified": last_modified,
            "body_sha256": body_sha256,
            "checked_at": checked_at,
            "changed_at": changed_at,
            "last_success_at": last_success_at,
            "parser_name": parser_name,
            "parser_version": parser_version,
            "config_hash": config_hash,
            "parse_status": parse_status,
            "parse_complete": bool(parse_complete),
            "publication_count": max(0, int(publication_count)),
            "last_run_id": last_run_id,
            "error_reason": error_reason,
            **(record or {}),
        }
        self.conn.execute(
            """
            INSERT INTO official_publication_refresh_state
            (person_id, institution_id, source_url, source_kind, final_url,
             etag, last_modified, body_sha256, checked_at, changed_at,
             last_success_at, parser_name, parser_version, config_hash,
             parse_status, parse_complete, publication_count, last_run_id,
             error_reason, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(person_id, source_kind, source_url) DO UPDATE SET
                institution_id=excluded.institution_id,
                final_url=excluded.final_url,
                etag=excluded.etag,
                last_modified=excluded.last_modified,
                body_sha256=excluded.body_sha256,
                checked_at=excluded.checked_at,
                changed_at=excluded.changed_at,
                last_success_at=excluded.last_success_at,
                parser_name=excluded.parser_name,
                parser_version=excluded.parser_version,
                config_hash=excluded.config_hash,
                parse_status=excluded.parse_status,
                parse_complete=excluded.parse_complete,
                publication_count=excluded.publication_count,
                last_run_id=excluded.last_run_id,
                error_reason=excluded.error_reason,
                record_json=excluded.record_json
            """,
            (
                person_id,
                institution_id,
                source_url,
                source_kind,
                final_url,
                etag,
                last_modified,
                body_sha256,
                checked_at,
                changed_at,
                last_success_at,
                parser_name,
                parser_version,
                config_hash,
                parse_status,
                int(parse_complete),
                max(0, int(publication_count)),
                last_run_id,
                error_reason,
                _json(payload),
            ),
        )
        if commit:
            self.conn.commit()
        return self.get_publication_refresh_state(person_id, source_url, source_kind) or {}

    def get_official_publication_source_claims(
        self,
        *,
        person_id: str | None = None,
        fingerprint_id: str | None = None,
        source_url: str | None = None,
        source_kind: str | None = None,
        include_tombstoned: bool = True,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("person_id", person_id),
            ("fingerprint_id", fingerprint_id),
            ("source_url", source_url),
            ("source_kind", source_kind),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if not include_tombstoned:
            clauses.append("claim_status!='tombstoned'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM official_publication_source_claims
            {where}
            ORDER BY person_id, source_kind, source_url, fingerprint_id
            """,
            params,
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["record"] = _loads(item.pop("record_json"), {})
            output.append(item)
        return output

    def _enqueue_vector_dirty_no_commit(
        self,
        entity_kind: str,
        entity_id: str,
        reason: str,
        *,
        run_id: str | None = None,
        person_id: str | None = None,
        fingerprint_id: str | None = None,
        payload: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> tuple[int, bool]:
        if not entity_kind or not entity_id or not reason:
            raise ValueError("entity_kind, entity_id and reason are required")
        now = created_at or utc_now_iso()
        existing = self.conn.execute(
            """
            SELECT queue_id FROM vector_dirty_queue
            WHERE entity_kind=? AND entity_id=? AND status='pending'
            """,
            (entity_kind, entity_id),
        ).fetchone()
        if existing:
            self.conn.execute(
                """
                UPDATE vector_dirty_queue
                SET person_id=COALESCE(?, person_id),
                    fingerprint_id=COALESCE(?, fingerprint_id),
                    reason=?, run_id=COALESCE(?, run_id), updated_at=?,
                    payload_json=?
                WHERE queue_id=?
                """,
                (
                    person_id,
                    fingerprint_id,
                    reason,
                    run_id,
                    now,
                    _json(payload or {}),
                    existing["queue_id"],
                ),
            )
            return int(existing["queue_id"]), False
        cursor = self.conn.execute(
            """
            INSERT INTO vector_dirty_queue
            (entity_kind, entity_id, person_id, fingerprint_id, reason, run_id,
             status, attempts, created_at, updated_at, processed_at, last_error,
             payload_json)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, NULL, NULL, ?)
            """,
            (
                entity_kind,
                entity_id,
                person_id,
                fingerprint_id,
                reason,
                run_id,
                now,
                now,
                _json(payload or {}),
            ),
        )
        return int(cursor.lastrowid), True

    def enqueue_vector_dirty(
        self,
        entity_kind: str,
        entity_id: str,
        reason: str,
        *,
        run_id: str | None = None,
        person_id: str | None = None,
        fingerprint_id: str | None = None,
        payload: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Idempotently enqueue one pending vector rebuild."""

        queue_id, created = self._enqueue_vector_dirty_no_commit(
            entity_kind,
            entity_id,
            reason,
            run_id=run_id,
            person_id=person_id,
            fingerprint_id=fingerprint_id,
            payload=payload,
            created_at=created_at,
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM vector_dirty_queue WHERE queue_id=?",
            (queue_id,),
        ).fetchone()
        result = dict(row)
        result["payload"] = _loads(result.pop("payload_json"), {})
        result["created"] = created
        return result

    def iter_vector_dirty_queue(
        self,
        *,
        status: str | None = "pending",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM vector_dirty_queue"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at, queue_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = self.conn.execute(sql, params).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json"), {})
            output.append(item)
        return output

    def complete_openalex_sync_jobs(
        self,
        person_id: str,
        run_id: str,
        *,
        completed_at: str | None = None,
    ) -> int:
        """Supersede pending source-sync events after that PI syncs successfully.

        Only unclaimed ``openalex_works_sync`` rows are eligible.  A live or
        failed worker claim is never cleared implicitly, and dry-run/unresolved
        callers must not invoke this method.
        """

        normalized_person_id = " ".join(str(person_id or "").split())
        normalized_run_id = " ".join(str(run_id or "").split())
        if not normalized_person_id or not normalized_run_id:
            raise ValueError("person_id and run_id are required")
        completed_at = completed_at or utc_now_iso()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE vector_dirty_queue
                SET status='completed', updated_at=?, processed_at=?, last_error=NULL,
                    claim_token=NULL, claim_owner=NULL, lease_expires_at=NULL,
                    payload_json=json_set(
                        CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,
                        '$.completed_by_openalex_sync_run_id', ?,
                        '$.completed_by_openalex_sync_at', ?
                    )
                WHERE entity_kind='openalex_works_sync'
                  AND status='pending'
                  AND (person_id=? OR entity_id=?)
                """,
                (
                    completed_at,
                    completed_at,
                    normalized_run_id,
                    completed_at,
                    normalized_person_id,
                    normalized_person_id,
                ),
            )
        return int(cursor.rowcount)

    def claim_vector_dirty_jobs(
        self,
        limit: int = 100,
        *,
        owner: str | None = None,
        lease_seconds: float = 900.0,
        entity_kinds: Iterable[str] | None = None,
        scope_person_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically lease pending or abandoned vector jobs to one worker.

        Every row receives its own unguessable token.  The token, rather than
        only the queue id, is the authority required to finish the job.  A
        ``processing`` row whose lease has expired (including a legacy row
        with no lease) is eligible for recovery after a worker crash.
        """

        claim_limit = max(0, int(limit))
        if claim_limit == 0:
            return []
        try:
            lease_duration = float(lease_seconds)
            if lease_duration <= 0:
                raise ValueError
            now_dt = datetime.now(timezone.utc)
            lease_expires_at = (now_dt + timedelta(seconds=lease_duration)).isoformat(
                timespec="microseconds"
            )
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("lease_seconds must be a positive finite duration") from exc

        now = now_dt.isoformat(timespec="microseconds")
        claim_owner = " ".join((owner or self._vector_claim_owner).split())
        if not claim_owner:
            claim_owner = self._vector_claim_owner
        kinds = tuple(
            dict.fromkeys(
                " ".join(str(value or "").split()) for value in (entity_kinds or ())
            )
        )
        if any(not value for value in kinds):
            raise ValueError("entity_kinds cannot contain blank values")
        kind_clause = ""
        if kinds:
            kind_clause = f"AND q.entity_kind IN ({','.join('?' for _ in kinds)})"

        scoped_people = tuple(
            dict.fromkeys(
                " ".join(str(value or "").split())
                for value in (scope_person_ids or ())
            )
        )
        if any(not value for value in scoped_people):
            raise ValueError("scope_person_ids cannot contain blank values")
        scope_clause = ""
        scope_params: tuple[str, ...] = ()
        if scope_person_ids is not None:
            if not scoped_people:
                return []
            people_placeholders = ",".join("?" for _ in scoped_people)
            scope_clause = f"""
                        AND (
                            (
                                q.entity_kind='career_vector_256'
                                AND q.entity_id IN ({people_placeholders})
                            )
                            OR (
                                q.entity_kind='paper_vector_256'
                                AND EXISTS (
                                    SELECT 1
                                    FROM openalex_person_works AS pw
                                    WHERE pw.openalex_work_id=q.entity_id
                                      AND pw.relationship_status IN ('active', 'missing')
                                      AND pw.person_id IN ({people_placeholders})
                                )
                            )
                        )
            """
            scope_params = (*scoped_people, *scoped_people)

        claimed: list[sqlite3.Row] = []
        # UPDATE ... RETURNING makes selection and reservation one SQLite
        # write statement.  The transaction keeps a batch together, while the
        # eligibility predicate also makes the operation safe if connections
        # race at the statement boundary.
        with self.conn:
            for _ in range(claim_limit):
                token = uuid.uuid4().hex
                row = self.conn.execute(
                    f"""
                    UPDATE vector_dirty_queue
                    SET status='processing', attempts=attempts+1, updated_at=?,
                        processed_at=NULL, claim_token=?, claim_owner=?,
                        lease_expires_at=?
                    WHERE queue_id = (
                        SELECT q.queue_id FROM vector_dirty_queue AS q
                        WHERE (
                            q.status='pending'
                            OR (
                               q.status='processing'
                               AND (
                                   q.lease_expires_at IS NULL
                                   OR q.lease_expires_at<=?
                               )
                            )
                        )
                        {kind_clause}
                        {scope_clause}
                        ORDER BY q.created_at, q.queue_id
                        LIMIT 1
                    )
                      AND (
                          status='pending'
                          OR (
                              status='processing'
                              AND (
                                  lease_expires_at IS NULL
                                  OR lease_expires_at<=?
                              )
                          )
                      )
                    RETURNING *
                    """,
                    (
                        now,
                        token,
                        claim_owner,
                        lease_expires_at,
                        now,
                        *kinds,
                        *scope_params,
                        now,
                    ),
                ).fetchone()
                if row is None:
                    break
                queue_id = int(row["queue_id"])
                self._vector_claim_tokens[queue_id] = token
                claimed.append(row)

        result: list[dict[str, Any]] = []
        for row in claimed:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json"), {})
            result.append(item)
        return result

    def finish_vector_dirty_job(
        self,
        queue_id: int,
        *,
        success: bool,
        error_reason: str | None = None,
        retry: bool = False,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        """Complete, fail, or retry a job only while owning its active lease.

        Existing same-instance callers may omit ``claim_token``: the token
        returned by :meth:`claim_vector_dirty_jobs` is retained privately.
        Cross-process or restart-safe callers should persist and pass the
        returned token explicitly.
        """

        now = utc_now_iso()
        status = "completed" if success else ("pending" if retry else "failed")
        processed_at = now if success or not retry else None
        token = claim_token or self._vector_claim_tokens.get(int(queue_id))
        if not token:
            raise ValueError(
                "claim_token is required unless this storage instance claimed the job"
            )
        cursor = self.conn.execute(
            """
            UPDATE vector_dirty_queue
            SET status=?, updated_at=?, processed_at=?, last_error=?,
                claim_token=NULL, claim_owner=NULL, lease_expires_at=NULL
            WHERE queue_id=? AND status='processing' AND claim_token=?
            """,
            (
                status,
                now,
                processed_at,
                None if success else error_reason,
                queue_id,
                token,
            ),
        )
        if cursor.rowcount != 1:
            self.conn.rollback()
            existing = self.conn.execute(
                "SELECT status, claim_token FROM vector_dirty_queue WHERE queue_id=?",
                (queue_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"Unknown vector dirty queue item: {queue_id}")
            raise RuntimeError(
                "Vector dirty job is no longer processing under this claim token"
            )
        self.conn.commit()
        self._vector_claim_tokens.pop(int(queue_id), None)
        row = self.conn.execute(
            "SELECT * FROM vector_dirty_queue WHERE queue_id=?",
            (queue_id,),
        ).fetchone()
        result = dict(row)
        result["payload"] = _loads(result.pop("payload_json"), {})
        return result

    def start_openalex_sync_run(
        self,
        run_id: str,
        institution_id: str | None = None,
        *,
        sync_mode: str = "delta",
        full_snapshot: bool = False,
        started_at: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start an auditable OpenAlex sync without assuming delta access."""

        if not run_id:
            raise ValueError("run_id is required")
        normalized_mode = " ".join((sync_mode or "").split()).casefold()
        if normalized_mode not in {"delta", "full", "selected_full"}:
            raise ValueError("sync_mode must be delta, full, or selected_full")
        started_at = started_at or utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO openalex_sync_runs
            (run_id, institution_id, sync_mode, full_snapshot, status,
             started_at, finished_at, metrics_json, error_reason)
            VALUES (?, ?, ?, ?, 'running', ?, NULL, ?, NULL)
            """,
            (
                run_id,
                institution_id,
                normalized_mode,
                int(full_snapshot),
                started_at,
                _json(metrics or {}),
            ),
        )
        self.conn.commit()
        return self.get_openalex_sync_run(run_id) or {}

    def finish_openalex_sync_run(
        self,
        run_id: str,
        status: str,
        metrics: dict[str, Any] | None = None,
        *,
        error_reason: str | None = None,
        finished_at: str | None = None,
    ) -> dict[str, Any]:
        normalized_status = " ".join((status or "").split()).casefold()
        if normalized_status not in {
            "success",
            "partial",
            "partial_success",
            "failed",
            "cancelled",
        }:
            raise ValueError("invalid OpenAlex sync status")
        cursor = self.conn.execute(
            """
            UPDATE openalex_sync_runs
            SET status=?, finished_at=?, metrics_json=?, error_reason=?
            WHERE run_id=?
            """,
            (
                normalized_status,
                finished_at or utc_now_iso(),
                _json(metrics or {}),
                error_reason,
                run_id,
            ),
        )
        if cursor.rowcount != 1:
            self.conn.rollback()
            raise KeyError(f"Unknown OpenAlex sync run: {run_id}")
        self.conn.commit()
        return self.get_openalex_sync_run(run_id) or {}

    def get_openalex_sync_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM openalex_sync_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["full_snapshot"] = bool(result["full_snapshot"])
        result["metrics"] = _loads(result.pop("metrics_json"), {})
        return result

    def get_openalex_identity_probe_cache(self, probe_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM openalex_identity_probe_cache WHERE probe_key=?",
            (str(probe_key),),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["works"] = _loads(result.pop("works_json"), [])
        result["record"] = _loads(result.pop("record_json"), {})
        return result

    def upsert_openalex_identity_probe_cache(
        self,
        probe_key: str,
        probe_version: str,
        evidence_kind: str,
        evidence_value: str,
        works: Iterable[Mapping[str, Any]],
        *,
        fetched_at: str,
        expires_at: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        if not all(
            str(value or "").strip()
            for value in (
                probe_key,
                probe_version,
                evidence_kind,
                evidence_value,
                fetched_at,
                expires_at,
            )
        ):
            raise ValueError("OpenAlex identity probe cache fields must be non-empty")
        raw_works = [dict(work) for work in works]
        works_json = _json(raw_works)
        works_sha256 = hashlib.sha256(works_json.encode("utf-8")).hexdigest()
        result_status = "hit" if raw_works else "miss"
        payload = {
            "probe_key": probe_key,
            "probe_version": probe_version,
            "evidence_kind": evidence_kind,
            "evidence_value": evidence_value,
            "result_status": result_status,
            "works_sha256": works_sha256,
            "fetched_at": fetched_at,
            "expires_at": expires_at,
            "last_run_id": run_id,
        }
        self.conn.execute(
            """
            INSERT INTO openalex_identity_probe_cache
            (probe_key, probe_version, evidence_kind, evidence_value,
             result_status, works_json, works_sha256, fetched_at, expires_at,
             last_run_id, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(probe_key) DO UPDATE SET
                probe_version=excluded.probe_version,
                evidence_kind=excluded.evidence_kind,
                evidence_value=excluded.evidence_value,
                result_status=excluded.result_status,
                works_json=excluded.works_json,
                works_sha256=excluded.works_sha256,
                fetched_at=excluded.fetched_at,
                expires_at=excluded.expires_at,
                last_run_id=excluded.last_run_id,
                record_json=excluded.record_json
            """,
            (
                probe_key,
                probe_version,
                evidence_kind,
                evidence_value,
                result_status,
                works_json,
                works_sha256,
                fetched_at,
                expires_at,
                run_id,
                _json(payload),
            ),
        )
        self.conn.commit()
        return self.get_openalex_identity_probe_cache(probe_key) or {}

    def get_openalex_author_link(self, person_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM openalex_author_links WHERE person_id=?",
            (person_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["evidence"] = _loads(result.pop("evidence_json"), {})
        result["record"] = _loads(result.pop("record_json"), {})
        return result

    @staticmethod
    def _author_link_has_reviewed_provenance(link: Mapping[str, Any]) -> bool:
        evidence = link.get("evidence") or {}
        if isinstance(evidence, Mapping):
            if evidence.get("reviewed") is True or evidence.get("manually_reviewed") is True:
                return True
            # Older reviewed rows did not consistently retain the nested
            # boolean, so the structured payload itself is provenance that
            # must never be discarded automatically.
            if isinstance(evidence.get("reviewed_identity"), Mapping):
                return True
        method = str(link.get("match_method") or "").casefold()
        return any(marker in method for marker in ("reviewed", "manual", "audited"))

    def _plan_openalex_author_link_archive(
        self,
        author_link: Mapping[str, Any],
        person_id: str,
        institution_id: str,
        run_id: str,
        observed_at: str,
        archive_request: Mapping[str, Any],
        reviewed_relationship: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate a narrowly scoped automatic-link supersession request."""

        if author_link.get("institution_id") != institution_id:
            raise ValueError("OpenAlex author-link archive institution mismatch")
        if str(author_link.get("link_status") or "").casefold() != "confirmed":
            raise ValueError(
                "Only a confirmed non-reviewed OpenAlex author link can be archived"
            )
        if self._author_link_has_reviewed_provenance(author_link):
            raise ValueError("Existing reviewed OpenAlex author link cannot be archived")

        current_author_id = _normalize_openalex_id(
            str(author_link.get("openalex_author_id") or ""), "A"
        )
        expected_author_id = _normalize_openalex_id(
            str(archive_request.get("expected_openalex_author_id") or ""), "A"
        )
        if not current_author_id or current_author_id != expected_author_id:
            raise ValueError(
                "OpenAlex author-link archive conflicts with the expected automatic link"
            )

        # Fail closed on internally inconsistent persisted identity evidence.
        # Such a row requires review rather than an automatic destructive
        # transition, even when its top-level match method was automatic.
        old_evidence = author_link.get("evidence") or {}
        if not isinstance(old_evidence, Mapping):
            raise ValueError("Existing OpenAlex author-link evidence is invalid")
        evidence_primary_raw = old_evidence.get("primary_openalex_author_id")
        if evidence_primary_raw:
            evidence_primary = _normalize_openalex_id(str(evidence_primary_raw), "A")
            if evidence_primary != current_author_id:
                raise ValueError(
                    "Existing OpenAlex author link conflicts with its persisted evidence"
                )
        confirmed_raw = old_evidence.get("confirmed_openalex_author_ids") or []
        if not isinstance(confirmed_raw, (list, tuple)):
            raise ValueError("Existing OpenAlex author-link evidence is invalid")
        confirmed_ids: set[str] = set()
        for raw_id in confirmed_raw:
            normalized_id = _normalize_openalex_id(str(raw_id or ""), "A")
            if not normalized_id:
                raise ValueError("Existing OpenAlex author-link evidence is invalid")
            confirmed_ids.add(normalized_id)
        if confirmed_ids and current_author_id not in confirmed_ids:
            raise ValueError(
                "Existing OpenAlex author link conflicts with its persisted evidence"
            )

        replacement = reviewed_relationship.get("reviewed_identity")
        if (
            not isinstance(replacement, Mapping)
            or replacement.get("reviewed") is not True
            or replacement.get("sync_mode") != "official_evidence_only"
            or replacement.get("primary_openalex_author_id") not in (None, "")
            or list(replacement.get("confirmed_openalex_author_ids") or [])
        ):
            raise ValueError(
                "Automatic OpenAlex author-link archival requires a reviewed, "
                "identity-pending official-evidence replacement"
            )
        manifest_sha256 = str(replacement.get("manifest_sha256") or "").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
            raise ValueError("Reviewed replacement has no valid manifest SHA-256")
        reason = " ".join(str(archive_request.get("reason") or "").split())
        if reason != "superseded_by_reviewed_official_evidence_only_identity_pending":
            raise ValueError("Unsupported OpenAlex author-link archive reason")

        original_link_json = _json(dict(author_link))
        original_link_sha256 = hashlib.sha256(
            original_link_json.encode("utf-8")
        ).hexdigest()
        return {
            "person_id": person_id,
            "institution_id": institution_id,
            "openalex_author_id": current_author_id,
            "link_status": "confirmed",
            "match_method": author_link.get("match_method"),
            "archived_at": observed_at,
            "archived_run_id": run_id,
            "archive_reason": reason,
            "replacement_manifest_sha256": manifest_sha256,
            "original_link_sha256": original_link_sha256,
            "original_link_json": original_link_json,
            "replacement_evidence_json": _json(dict(replacement)),
        }

    def upsert_openalex_author_link(
        self,
        person_id: str,
        institution_id: str,
        openalex_author_id: str,
        *,
        link_status: str = "confirmed",
        confidence: float | None = None,
        match_method: str | None = None,
        evidence: dict[str, Any] | None = None,
        run_id: str | None = None,
        verified_at: str | None = None,
        last_successful_sync_at: str | None = None,
        last_full_sync_at: str | None = None,
        works_updated_through: str | None = None,
    ) -> dict[str, Any]:
        if not person_id or not institution_id:
            raise ValueError("person_id and institution_id are required")
        author_id = _normalize_openalex_id(openalex_author_id, "A")
        if not author_id:
            raise ValueError(f"Invalid OpenAlex author ID: {openalex_author_id}")
        normalized_status = " ".join((link_status or "").split()).casefold()
        if normalized_status not in {"confirmed", "review", "rejected", "stale"}:
            raise ValueError("invalid OpenAlex author-link status")
        if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("confidence must be between 0 and 1")

        existing = self.get_openalex_author_link(person_id)
        if existing and existing["institution_id"] != institution_id:
            raise ValueError("OpenAlex author link institution cannot change implicitly")
        verified_at = verified_at or utc_now_iso()
        explicit_sync_update = any(
            value is not None
            for value in (
                last_successful_sync_at,
                last_full_sync_at,
                works_updated_through,
            )
        )
        first_linked_at = existing["first_linked_at"] if existing else verified_at
        merged_evidence = dict(existing.get("evidence") or {}) if existing else {}
        if evidence:
            merged_evidence.update(evidence)
        previous_through = existing.get("works_updated_through") if existing else None
        if previous_through and works_updated_through:
            works_updated_through = max(previous_through, works_updated_through)
        else:
            works_updated_through = works_updated_through or previous_through
        last_successful_sync_at = last_successful_sync_at or (
            existing.get("last_successful_sync_at") if existing else None
        )
        last_full_sync_at = last_full_sync_at or (
            existing.get("last_full_sync_at") if existing else None
        )
        last_sync_run_id = (
            run_id
            if run_id and explicit_sync_update
            else (existing.get("last_sync_run_id") if existing else None)
        )
        payload = {
            "person_id": person_id,
            "institution_id": institution_id,
            "openalex_author_id": author_id,
            "link_status": normalized_status,
            "confidence": float(confidence) if confidence is not None else None,
            "match_method": match_method,
            "evidence": merged_evidence,
            "first_linked_at": first_linked_at,
            "last_verified_at": verified_at,
            "last_successful_sync_at": last_successful_sync_at,
            "last_full_sync_at": last_full_sync_at,
            "works_updated_through": works_updated_through,
            "last_sync_run_id": last_sync_run_id,
        }
        self.conn.execute(
            """
            INSERT INTO openalex_author_links
            (person_id, institution_id, openalex_author_id, link_status,
             confidence, match_method, evidence_json, first_linked_at,
             last_verified_at, last_successful_sync_at, last_full_sync_at,
             works_updated_through, last_sync_run_id, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(person_id) DO UPDATE SET
                institution_id=excluded.institution_id,
                openalex_author_id=excluded.openalex_author_id,
                link_status=excluded.link_status,
                confidence=excluded.confidence,
                match_method=excluded.match_method,
                evidence_json=excluded.evidence_json,
                first_linked_at=excluded.first_linked_at,
                last_verified_at=excluded.last_verified_at,
                last_successful_sync_at=excluded.last_successful_sync_at,
                last_full_sync_at=excluded.last_full_sync_at,
                works_updated_through=excluded.works_updated_through,
                last_sync_run_id=excluded.last_sync_run_id,
                record_json=excluded.record_json
            """,
            (
                person_id,
                institution_id,
                author_id,
                normalized_status,
                float(confidence) if confidence is not None else None,
                match_method,
                _json(merged_evidence),
                first_linked_at,
                verified_at,
                last_successful_sync_at,
                last_full_sync_at,
                works_updated_through,
                last_sync_run_id,
                _json(payload),
            ),
        )
        self.conn.commit()
        return self.get_openalex_author_link(person_id) or {}

    def upsert_openalex_work(
        self,
        work: dict[str, Any],
        run_id: str,
        *,
        observed_at: str | None = None,
        dry_run: bool = False,
        enqueue_vectors: bool = True,
    ) -> dict[str, Any]:
        """Upsert normalized OpenAlex work metadata and invalidate vectors."""

        if not isinstance(work, dict) or not run_id:
            raise ValueError("work payload and run_id are required")
        work_id = _normalize_openalex_id(
            str(work.get("openalex_work_id") or work.get("id") or ""),
            "W",
        )
        if not work_id:
            raise ValueError("OpenAlex work payload has no valid work ID")
        observed_at = observed_at or utc_now_iso()
        vector_text, abstract_text, topics = _openalex_work_vector_text(work)
        vector_text_hash = hashlib.sha256(vector_text.encode("utf-8")).hexdigest()
        title = " ".join(str(work.get("title") or work.get("display_name") or "").split())
        doi = str(work.get("doi") or "").strip() or None
        if doi:
            doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I).casefold()
        publication_year = work.get("publication_year")
        try:
            publication_year = int(publication_year) if publication_year not in (None, "") else None
        except (TypeError, ValueError):
            publication_year = None
        raw_json = _json(work)
        existing = self.conn.execute(
            "SELECT * FROM openalex_works WHERE openalex_work_id=?",
            (work_id,),
        ).fetchone()
        created = existing is None
        vector_text_changed = bool(
            existing is not None and existing["vector_text_hash"] != vector_text_hash
        )
        metadata_changed = bool(
            existing is not None and existing["raw_json"] != raw_json
        )
        first_seen_at = existing["first_seen_at"] if existing else observed_at
        vector_jobs: list[dict[str, Any]] = []

        if not dry_run:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO openalex_works
                    (openalex_work_id, doi, title, abstract_text,
                     publication_year, publication_date, updated_date,
                     work_type, language, topics_json, vector_text,
                     vector_text_hash, raw_json, first_seen_at, last_seen_at,
                     last_seen_run_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(openalex_work_id) DO UPDATE SET
                        doi=excluded.doi,
                        title=excluded.title,
                        abstract_text=excluded.abstract_text,
                        publication_year=excluded.publication_year,
                        publication_date=excluded.publication_date,
                        updated_date=excluded.updated_date,
                        work_type=excluded.work_type,
                        language=excluded.language,
                        topics_json=excluded.topics_json,
                        vector_text=excluded.vector_text,
                        vector_text_hash=excluded.vector_text_hash,
                        raw_json=excluded.raw_json,
                        first_seen_at=excluded.first_seen_at,
                        last_seen_at=excluded.last_seen_at,
                        last_seen_run_id=excluded.last_seen_run_id
                    """,
                    (
                        work_id,
                        doi,
                        title,
                        abstract_text,
                        publication_year,
                        work.get("publication_date"),
                        work.get("updated_date"),
                        work.get("type") or work.get("work_type"),
                        work.get("language"),
                        _json(topics),
                        vector_text,
                        vector_text_hash,
                        raw_json,
                        first_seen_at,
                        observed_at,
                        run_id,
                    ),
                )
                if enqueue_vectors and (created or vector_text_changed):
                    reason = "openalex_work_created" if created else "openalex_vector_text_changed"
                    queue_id, queue_created = self._enqueue_vector_dirty_no_commit(
                        "paper_vector_256",
                        work_id,
                        reason,
                        run_id=run_id,
                        payload={"vector_text_hash": vector_text_hash},
                        created_at=observed_at,
                    )
                    vector_jobs.append(
                        {
                            "queue_id": queue_id,
                            "created": queue_created,
                            "entity_kind": "paper_vector_256",
                            "entity_id": work_id,
                            "reason": reason,
                        }
                    )
                    linked_people = self.conn.execute(
                        """
                        SELECT person_id FROM openalex_person_works
                        WHERE openalex_work_id=?
                          AND relationship_status IN ('active', 'missing')
                        """,
                        (work_id,),
                    ).fetchall()
                    for linked in linked_people:
                        person_id = linked["person_id"]
                        queue_id, queue_created = self._enqueue_vector_dirty_no_commit(
                            "career_vector_256",
                            person_id,
                            "paper_vector_dependency_changed",
                            run_id=run_id,
                            person_id=person_id,
                            payload={"openalex_work_id": work_id},
                            created_at=observed_at,
                        )
                        vector_jobs.append(
                            {
                                "queue_id": queue_id,
                                "created": queue_created,
                                "entity_kind": "career_vector_256",
                                "entity_id": person_id,
                                "reason": "paper_vector_dependency_changed",
                            }
                        )

        return {
            "work_id": work_id,
            "created": created,
            "vector_text_changed": vector_text_changed,
            "metadata_changed": metadata_changed,
            "dry_run": bool(dry_run),
            "vector_jobs": vector_jobs,
        }

    def reconcile_openalex_person_works(
        self,
        person_id: str,
        institution_id: str,
        openalex_author_id: str | None,
        run_id: str,
        observed_work_ids: Iterable[str],
        *,
        full_snapshot: bool,
        missing_runs_before_tombstone: int = 2,
        dry_run: bool = False,
        observed_at: str | None = None,
        enqueue_vectors: bool = True,
        relationship_evidence: Mapping[str, Any] | None = None,
        observed_work_author_ids: Mapping[str, str | None] | None = None,
        archive_existing_author_link: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reconcile one PI's works; delta runs never advance absence state."""

        if not person_id or not institution_id or not run_id:
            raise ValueError("person, institution and run identifiers are required")
        author_id = _normalize_openalex_id(openalex_author_id or "", "A")
        reviewed_relationship = (
            dict(relationship_evidence) if relationship_evidence is not None else None
        )
        if not author_id and (
            not reviewed_relationship
            or reviewed_relationship.get("relationship_method")
            != "reviewed_official_evidence_only"
            or reviewed_relationship.get("identity_status") != "pending"
        ):
            raise ValueError(f"Invalid OpenAlex author ID: {openalex_author_id}")
        author_link = self.get_openalex_author_link(person_id)
        if author_id and author_link is None and not dry_run:
            raise KeyError(f"No OpenAlex author link for PI: {person_id}")
        observed_at = observed_at or utc_now_iso()
        author_link_archive: dict[str, Any] | None = None
        if not author_id and author_link is not None:
            if archive_existing_author_link is None:
                raise ValueError(
                    "A reviewed identity-pending Work relationship conflicts with an "
                    "existing OpenAlex author link"
                )
            author_link_archive = self._plan_openalex_author_link_archive(
                author_link,
                person_id,
                institution_id,
                run_id,
                observed_at,
                archive_existing_author_link,
                reviewed_relationship or {},
            )
        elif archive_existing_author_link is not None:
            raise ValueError(
                "OpenAlex author-link archival is only valid for an existing "
                "identity-pending relationship"
            )
        if author_id and author_link and (
            author_link["institution_id"] != institution_id
            or author_link["openalex_author_id"] != author_id
        ):
            raise ValueError("OpenAlex reconciliation does not match the confirmed author link")
        allowed_author_ids = {author_id} if author_id else set()
        if author_link:
            evidence = author_link.get("evidence") or {}
            for raw_profile_id in evidence.get("confirmed_openalex_author_ids", []):
                profile_id = _normalize_openalex_id(str(raw_profile_id), "A")
                if profile_id:
                    allowed_author_ids.add(profile_id)
        threshold = max(1, int(missing_runs_before_tombstone))
        observed_ids: list[str] = []
        observed_id_set: set[str] = set()
        for raw_id in observed_work_ids:
            work_id = _normalize_openalex_id(str(raw_id), "W")
            if not work_id:
                raise ValueError(f"Invalid OpenAlex work ID: {raw_id}")
            if work_id not in observed_id_set:
                observed_ids.append(work_id)
                observed_id_set.add(work_id)
        observed_set = set(observed_ids)
        normalized_work_authors: dict[str, str | None] = {}
        if observed_work_author_ids is not None:
            for raw_work_id, raw_author_id in observed_work_author_ids.items():
                work_id = _normalize_openalex_id(str(raw_work_id), "W")
                if not work_id or work_id not in observed_set:
                    raise ValueError(
                        "observed_work_author_ids must reference an observed Work ID"
                    )
                profile_id = _normalize_openalex_id(str(raw_author_id or ""), "A")
                if raw_author_id is not None and not profile_id:
                    raise ValueError(
                        f"Invalid per-Work OpenAlex author ID: {raw_author_id}"
                    )
                if profile_id and profile_id not in allowed_author_ids:
                    raise ValueError(
                        f"Per-Work OpenAlex author ID is not confirmed for this PI: {profile_id}"
                    )
                if profile_id is None and author_id:
                    raise ValueError(
                        "Confirmed-author reconciliation cannot assign a null per-Work author"
                    )
                normalized_work_authors[work_id] = profile_id

        known: set[str] = set()
        for offset in range(0, len(observed_ids), 800):
            chunk = observed_ids[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            known.update(
                row["openalex_work_id"]
                for row in self.conn.execute(
                    f"SELECT openalex_work_id FROM openalex_works WHERE openalex_work_id IN ({placeholders})",
                    chunk,
                ).fetchall()
            )
        unknown = [work_id for work_id in observed_ids if work_id not in known]
        if unknown and not dry_run:
            raise KeyError(f"Unknown OpenAlex works: {', '.join(unknown[:5])}")

        existing_rows = self.conn.execute(
            "SELECT * FROM openalex_person_works WHERE person_id=?",
            (person_id,),
        ).fetchall()
        existing = {row["openalex_work_id"]: row for row in existing_rows}
        categories: dict[str, list[str]] = {
            "added": [],
            "recovered": [],
            "reactivated": [],
            "missing": [],
            "tombstoned": [],
            "unchanged": [],
            "absence_ignored_delta": [],
        }
        planned: dict[str, dict[str, Any]] = {}

        for work_id in observed_ids:
            relationship_author_id = normalized_work_authors.get(work_id, author_id or None)
            old = existing.get(work_id)
            if old is None:
                categories["added"].append(work_id)
                first_seen_at = observed_at
            else:
                first_seen_at = old["first_seen_at"] or observed_at
                if old["relationship_status"] == "tombstoned":
                    categories["reactivated"].append(work_id)
                elif old["relationship_status"] == "missing" or old["missing_streak"]:
                    categories["recovered"].append(work_id)
                else:
                    categories["unchanged"].append(work_id)
            planned[work_id] = {
                "person_id": person_id,
                "openalex_work_id": work_id,
                "institution_id": institution_id,
                "openalex_author_id": relationship_author_id,
                "relationship_status": "active",
                "missing_streak": 0,
                "first_seen_at": first_seen_at,
                "last_seen_at": observed_at,
                "last_seen_run_id": run_id,
                "last_checked_at": observed_at,
                "tombstoned_at": None,
                "relationship_evidence": reviewed_relationship,
            }

        for work_id, old in existing.items():
            if work_id in observed_set:
                continue
            if not full_snapshot:
                if old["relationship_status"] != "tombstoned":
                    categories["absence_ignored_delta"].append(work_id)
                continue
            if old["relationship_status"] == "tombstoned":
                continue
            streak = int(old["missing_streak"] or 0) + 1
            status = "tombstoned" if streak >= threshold else "missing"
            categories[status].append(work_id)
            planned[work_id] = {
                "person_id": person_id,
                "openalex_work_id": work_id,
                "institution_id": institution_id,
                "openalex_author_id": old["openalex_author_id"] or author_id or None,
                "relationship_status": status,
                "missing_streak": streak,
                "first_seen_at": old["first_seen_at"],
                "last_seen_at": old["last_seen_at"],
                "last_seen_run_id": old["last_seen_run_id"],
                "last_checked_at": observed_at,
                "tombstoned_at": observed_at if status == "tombstoned" else None,
                "relationship_evidence": _loads(old["record_json"], {}).get(
                    "relationship_evidence"
                ),
            }

        collection_changes = {
            work_id: "work_added" for work_id in categories["added"]
        } | {
            work_id: "work_reactivated" for work_id in categories["reactivated"]
        } | {
            work_id: "work_tombstoned" for work_id in categories["tombstoned"]
        }
        vector_jobs: list[dict[str, Any]] = []

        if not dry_run:
            with self.conn:
                if author_link_archive is not None:
                    self.conn.execute(
                        """
                        INSERT INTO openalex_author_link_archives
                        (person_id, institution_id, openalex_author_id, link_status,
                         match_method, archived_at, archived_run_id, archive_reason,
                         replacement_manifest_sha256, original_link_sha256,
                         original_link_json, replacement_evidence_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            author_link_archive["person_id"],
                            author_link_archive["institution_id"],
                            author_link_archive["openalex_author_id"],
                            author_link_archive["link_status"],
                            author_link_archive["match_method"],
                            author_link_archive["archived_at"],
                            author_link_archive["archived_run_id"],
                            author_link_archive["archive_reason"],
                            author_link_archive["replacement_manifest_sha256"],
                            author_link_archive["original_link_sha256"],
                            author_link_archive["original_link_json"],
                            author_link_archive["replacement_evidence_json"],
                        ),
                    )
                    deleted = self.conn.execute(
                        """
                        DELETE FROM openalex_author_links
                        WHERE person_id=? AND institution_id=?
                          AND openalex_author_id=? AND link_status='confirmed'
                        """,
                        (
                            person_id,
                            institution_id,
                            author_link_archive["openalex_author_id"],
                        ),
                    )
                    if deleted.rowcount != 1:
                        raise RuntimeError(
                            "OpenAlex author link changed during reviewed archival"
                        )
                for relationship in planned.values():
                    self.conn.execute(
                        """
                        INSERT INTO openalex_person_works
                        (person_id, openalex_work_id, institution_id,
                         openalex_author_id, relationship_status, missing_streak,
                         first_seen_at, last_seen_at, last_seen_run_id,
                         last_checked_at, tombstoned_at, record_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(person_id, openalex_work_id) DO UPDATE SET
                            institution_id=excluded.institution_id,
                            openalex_author_id=excluded.openalex_author_id,
                            relationship_status=excluded.relationship_status,
                            missing_streak=excluded.missing_streak,
                            first_seen_at=excluded.first_seen_at,
                            last_seen_at=excluded.last_seen_at,
                            last_seen_run_id=excluded.last_seen_run_id,
                            last_checked_at=excluded.last_checked_at,
                            tombstoned_at=excluded.tombstoned_at,
                            record_json=excluded.record_json
                        """,
                        (
                            relationship["person_id"],
                            relationship["openalex_work_id"],
                            relationship["institution_id"],
                            relationship["openalex_author_id"],
                            relationship["relationship_status"],
                            relationship["missing_streak"],
                            relationship["first_seen_at"],
                            relationship["last_seen_at"],
                            relationship["last_seen_run_id"],
                            relationship["last_checked_at"],
                            relationship["tombstoned_at"],
                            _json(relationship),
                        ),
                    )

                updated_through = None
                if observed_ids:
                    placeholders = ",".join("?" for _ in observed_ids)
                    row = self.conn.execute(
                        f"SELECT MAX(updated_date) AS value FROM openalex_works WHERE openalex_work_id IN ({placeholders})",
                        observed_ids,
                    ).fetchone()
                    updated_through = row["value"] if row else None
                previous_through = author_link.get("works_updated_through") if author_link else None
                if previous_through and updated_through:
                    updated_through = max(previous_through, updated_through)
                else:
                    updated_through = updated_through or previous_through
                author_record = dict(author_link.get("record") or {}) if author_link else {}
                author_record.update(
                    {
                        "last_successful_sync_at": observed_at,
                        "last_full_sync_at": (
                            observed_at
                            if full_snapshot
                            else (author_link.get("last_full_sync_at") if author_link else None)
                        ),
                        "works_updated_through": updated_through,
                        "last_sync_run_id": run_id,
                    }
                )
                self.conn.execute(
                    """
                    UPDATE openalex_author_links
                    SET last_successful_sync_at=?,
                        last_full_sync_at=CASE WHEN ? THEN ? ELSE last_full_sync_at END,
                        works_updated_through=?,
                        last_sync_run_id=?,
                        record_json=?
                    WHERE person_id=?
                    """,
                    (
                        observed_at,
                        int(full_snapshot),
                        observed_at,
                        updated_through,
                        run_id,
                        _json(author_record),
                        person_id,
                    ),
                )
                if enqueue_vectors and collection_changes:
                    queue_id, queue_created = self._enqueue_vector_dirty_no_commit(
                        "career_vector_256",
                        person_id,
                        "openalex_work_collection_changed",
                        run_id=run_id,
                        person_id=person_id,
                        payload={"changes": collection_changes},
                        created_at=observed_at,
                    )
                    vector_jobs.append(
                        {
                            "queue_id": queue_id,
                            "created": queue_created,
                            "entity_kind": "career_vector_256",
                            "entity_id": person_id,
                            "reason": "openalex_work_collection_changed",
                        }
                    )

        return {
            "person_id": person_id,
            "institution_id": institution_id,
            "openalex_author_id": author_id,
            "run_id": run_id,
            "full_snapshot": bool(full_snapshot),
            "dry_run": bool(dry_run),
            "author_link_archive": (
                {
                    "action": "planned" if dry_run else "archived",
                    "person_id": author_link_archive["person_id"],
                    "openalex_author_id": author_link_archive["openalex_author_id"],
                    "match_method": author_link_archive["match_method"],
                    "archive_reason": author_link_archive["archive_reason"],
                    "replacement_manifest_sha256": author_link_archive[
                        "replacement_manifest_sha256"
                    ],
                    "original_link_sha256": author_link_archive[
                        "original_link_sha256"
                    ],
                }
                if author_link_archive is not None
                else None
            ),
            **categories,
            "collection_changes": collection_changes,
            "vector_jobs": vector_jobs,
            "counts": {
                **{key: len(value) for key, value in categories.items()},
                "collection_changes": len(collection_changes),
                "vector_jobs": len(vector_jobs),
                "author_links_archived": int(
                    author_link_archive is not None and not dry_run
                ),
                "author_link_archives_planned": int(
                    author_link_archive is not None and dry_run
                ),
            },
        }

    def iter_current_openalex_works(
        self,
        *,
        person_id: str | None = None,
        institution_id: str | None = None,
        openalex_author_id: str | None = None,
    ) -> Iterable[dict[str, Any]]:
        """Iterate current person-work relationships with normalized work data."""

        clauses = ["pw.relationship_status IN ('active', 'missing')"]
        params: list[Any] = []
        if person_id is not None:
            clauses.append("pw.person_id=?")
            params.append(person_id)
        if institution_id is not None:
            clauses.append("pw.institution_id=?")
            params.append(institution_id)
        if openalex_author_id is not None:
            author_id = _normalize_openalex_id(openalex_author_id, "A")
            if not author_id:
                raise ValueError(f"Invalid OpenAlex author ID: {openalex_author_id}")
            clauses.append("pw.openalex_author_id=?")
            params.append(author_id)
        rows = self.conn.execute(
            f"""
            SELECT w.*, pw.person_id, pw.institution_id,
                   pw.openalex_author_id, pw.relationship_status,
                   pw.missing_streak, pw.first_seen_at AS relationship_first_seen_at,
                   pw.last_seen_at AS relationship_last_seen_at,
                   pw.last_seen_run_id AS relationship_last_seen_run_id,
                   pw.last_checked_at AS relationship_last_checked_at
            FROM openalex_person_works pw
            JOIN openalex_works w ON w.openalex_work_id=pw.openalex_work_id
            WHERE {' AND '.join(clauses)}
            ORDER BY pw.person_id,
                     CASE WHEN w.publication_date IS NULL THEN 1 ELSE 0 END,
                     w.publication_date DESC, w.openalex_work_id
            """,
            params,
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["topics"] = _loads(item.pop("topics_json"), [])
            item["raw"] = _loads(item.pop("raw_json"), {})
            yield item

    def upsert_openalex_work_vector(
        self,
        openalex_work_id: str,
        vector: dict[str, Any],
        source_text_hash: str,
        *,
        representation: str = "publication_vector_256",
        encoder_id: str = "production_terms_v1",
        feature_limit: int = 256,
        generated_at: str | None = None,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one global Work-ID vector after a stale-input check."""

        from .index.vector_index import sparse_vector_hash, sparse_vector_json

        work_id = _normalize_openalex_id(openalex_work_id, "W")
        if not work_id:
            raise ValueError(f"Invalid OpenAlex work ID: {openalex_work_id}")
        representation = " ".join((representation or "").split())
        encoder_id = " ".join((encoder_id or "").split())
        source_text_hash = str(source_text_hash or "").strip().casefold()
        if not representation or not encoder_id or not source_text_hash:
            raise ValueError("representation, encoder_id and source_text_hash are required")
        source = self.conn.execute(
            "SELECT vector_text_hash FROM openalex_works WHERE openalex_work_id=?",
            (work_id,),
        ).fetchone()
        if source is None:
            raise KeyError(f"Unknown OpenAlex work: {work_id}")
        if str(source["vector_text_hash"] or "").casefold() != source_text_hash:
            raise RuntimeError(f"OpenAlex vector text changed while encoding {work_id}")

        normalized = _validated_sparse_vector(vector, feature_limit)
        vector_json = sparse_vector_json(normalized)
        vector_hash = sparse_vector_hash(normalized)
        generated_at = generated_at or utc_now_iso()
        payload = {
            "openalex_work_id": work_id,
            "representation": representation,
            "encoder_id": encoder_id,
            "feature_limit": int(feature_limit),
            "feature_count": len(normalized),
            "vector_hash": vector_hash,
            "source_text_hash": source_text_hash,
            "generated_at": generated_at,
            **(record or {}),
        }
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO openalex_work_vectors
                (openalex_work_id, representation, encoder_id, feature_limit,
                 vector_json, feature_count, vector_hash, source_text_hash,
                 generated_at, record_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(openalex_work_id, representation) DO UPDATE SET
                    encoder_id=excluded.encoder_id,
                    feature_limit=excluded.feature_limit,
                    vector_json=excluded.vector_json,
                    feature_count=excluded.feature_count,
                    vector_hash=excluded.vector_hash,
                    source_text_hash=excluded.source_text_hash,
                    generated_at=excluded.generated_at,
                    record_json=excluded.record_json
                """,
                (
                    work_id,
                    representation,
                    encoder_id,
                    int(feature_limit),
                    vector_json,
                    len(normalized),
                    vector_hash,
                    source_text_hash,
                    generated_at,
                    _json(payload),
                ),
            )
        return self.get_openalex_work_vector(work_id, representation=representation) or {}

    def get_openalex_work_vector(
        self,
        openalex_work_id: str,
        *,
        representation: str = "publication_vector_256",
    ) -> dict[str, Any] | None:
        work_id = _normalize_openalex_id(openalex_work_id, "W")
        if not work_id:
            return None
        row = self.conn.execute(
            """
            SELECT * FROM openalex_work_vectors
            WHERE openalex_work_id=? AND representation=?
            """,
            (work_id, representation),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["vector"] = _loads(item.pop("vector_json"), {})
        item["record"] = _loads(item.pop("record_json"), {})
        return item

    def iter_openalex_work_vectors(
        self,
        *,
        representation: str | None = "publication_vector_256",
        encoder_id: str | None = None,
    ) -> Iterable[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if representation is not None:
            clauses.append("representation=?")
            params.append(representation)
        if encoder_id is not None:
            clauses.append("encoder_id=?")
            params.append(encoder_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM openalex_work_vectors
            {where}
            ORDER BY openalex_work_id, representation
            """,
            params,
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["vector"] = _loads(item.pop("vector_json"), {})
            item["record"] = _loads(item.pop("record_json"), {})
            yield item

    def upsert_pi_career_vector(
        self,
        person_id: str,
        vector: dict[str, Any],
        dependency_hash: str,
        work_count: int,
        nonempty_work_count: int,
        *,
        representation: str = "career_vector_256",
        encoder_id: str = "production_terms_v1",
        feature_limit: int = 256,
        generated_at: str | None = None,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one PI career centroid and its exact Work dependency hash."""

        from .index.vector_index import sparse_vector_hash, sparse_vector_json

        person_id = str(person_id or "").strip()
        representation = " ".join((representation or "").split())
        encoder_id = " ".join((encoder_id or "").split())
        dependency_hash = str(dependency_hash or "").strip().casefold()
        if not person_id or not representation or not encoder_id or not dependency_hash:
            raise ValueError(
                "person_id, representation, encoder_id and dependency_hash are required"
            )
        if self.conn.execute(
            "SELECT 1 FROM canonical_pi_records WHERE person_id=?", (person_id,)
        ).fetchone() is None:
            raise KeyError(f"Unknown PI: {person_id}")
        work_count = int(work_count)
        nonempty_work_count = int(nonempty_work_count)
        if work_count < 0 or not 0 <= nonempty_work_count <= work_count:
            raise ValueError("work counts must satisfy 0 <= nonempty <= total")

        normalized = _validated_sparse_vector(vector, feature_limit)
        vector_json = sparse_vector_json(normalized)
        vector_hash = sparse_vector_hash(normalized)
        generated_at = generated_at or utc_now_iso()
        payload = {
            "person_id": person_id,
            "representation": representation,
            "encoder_id": encoder_id,
            "feature_limit": int(feature_limit),
            "feature_count": len(normalized),
            "vector_hash": vector_hash,
            "dependency_hash": dependency_hash,
            "work_count": work_count,
            "nonempty_work_count": nonempty_work_count,
            "generated_at": generated_at,
            **(record or {}),
        }
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO pi_career_vectors
                (person_id, representation, encoder_id, feature_limit,
                 vector_json, feature_count, vector_hash, dependency_hash,
                 work_count, nonempty_work_count, generated_at, record_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(person_id, representation) DO UPDATE SET
                    encoder_id=excluded.encoder_id,
                    feature_limit=excluded.feature_limit,
                    vector_json=excluded.vector_json,
                    feature_count=excluded.feature_count,
                    vector_hash=excluded.vector_hash,
                    dependency_hash=excluded.dependency_hash,
                    work_count=excluded.work_count,
                    nonempty_work_count=excluded.nonempty_work_count,
                    generated_at=excluded.generated_at,
                    record_json=excluded.record_json
                """,
                (
                    person_id,
                    representation,
                    encoder_id,
                    int(feature_limit),
                    vector_json,
                    len(normalized),
                    vector_hash,
                    dependency_hash,
                    work_count,
                    nonempty_work_count,
                    generated_at,
                    _json(payload),
                ),
            )
        return self.get_pi_career_vector(person_id, representation=representation) or {}

    def get_pi_career_vector(
        self,
        person_id: str,
        *,
        representation: str = "career_vector_256",
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM pi_career_vectors
            WHERE person_id=? AND representation=?
            """,
            (person_id, representation),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["vector"] = _loads(item.pop("vector_json"), {})
        item["record"] = _loads(item.pop("record_json"), {})
        return item

    def iter_pi_career_vectors(
        self,
        *,
        representation: str | None = "career_vector_256",
        encoder_id: str | None = None,
    ) -> Iterable[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if representation is not None:
            clauses.append("representation=?")
            params.append(representation)
        if encoder_id is not None:
            clauses.append("encoder_id=?")
            params.append(encoder_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM pi_career_vectors
            {where}
            ORDER BY person_id, representation
            """,
            params,
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["vector"] = _loads(item.pop("vector_json"), {})
            item["record"] = _loads(item.pop("record_json"), {})
            yield item

    def reconcile_official_publication_claims(
        self,
        person_id: str,
        institution_id: str,
        source_url: str,
        run_id: str,
        observed_fingerprint_ids: Iterable[str],
        *,
        source_kind: str = "official_profile",
        complete: bool,
        missing_runs_before_tombstone: int = 2,
        dry_run: bool = False,
        observed_at: str | None = None,
        enqueue_vectors: bool = True,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Diff one complete source snapshot against its publication claims.

        Absence is source-local and advances only for a complete parse.  The
        first absence is ``no_longer_observed``; a soft tombstone is emitted at
        the configured threshold.  A work remains globally current while any
        source has an active or provisional claim.
        """

        if not all((person_id, institution_id, source_url, source_kind, run_id)):
            raise ValueError("person, institution, source and run identifiers are required")
        # The production refresh entry point enforces a minimum of two
        # confirmations.  Keep this lower-level primitive backward compatible
        # for controlled migrations and legacy fixture construction.
        threshold = max(1, int(missing_runs_before_tombstone))
        observed_at = observed_at or utc_now_iso()
        observed_ids = sorted({str(value) for value in observed_fingerprint_ids if value})

        publication_rows: dict[str, sqlite3.Row] = {}
        for offset in range(0, len(observed_ids), 800):
            chunk = observed_ids[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"""
                SELECT fingerprint_id, person_id, institution_id, first_seen_at
                FROM official_publication_fingerprints
                WHERE fingerprint_id IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            publication_rows.update({row["fingerprint_id"]: row for row in rows})
        unknown = [value for value in observed_ids if value not in publication_rows]
        if unknown and not dry_run:
            raise KeyError(f"Unknown publication fingerprints: {', '.join(unknown[:5])}")
        # A dry run must be able to preview newly parsed fingerprints without
        # writing their materialized rows first.  Ownership is supplied by the
        # refresh scope and is safe to synthesize solely for the in-memory diff.
        for fingerprint_id in unknown:
            publication_rows[fingerprint_id] = {
                "fingerprint_id": fingerprint_id,
                "person_id": person_id,
                "institution_id": institution_id,
                "first_seen_at": observed_at,
            }
        mismatched = [
            value
            for value, row in publication_rows.items()
            if row["person_id"] != person_id or row["institution_id"] != institution_id
        ]
        if mismatched:
            raise ValueError(
                "Publication fingerprints do not belong to the requested person/institution: "
                + ", ".join(mismatched[:5])
            )

        existing_rows = self.conn.execute(
            """
            SELECT * FROM official_publication_source_claims
            WHERE person_id=? AND source_kind=? AND source_url=?
            """,
            (person_id, source_kind, source_url),
        ).fetchall()
        existing = {row["fingerprint_id"]: row for row in existing_rows}
        observed_set = set(observed_ids)
        categories: dict[str, list[str]] = {
            "added_claims": [],
            "recovered_claims": [],
            "reactivated_claims": [],
            "no_longer_observed": [],
            "tombstoned": [],
            "unchanged": [],
            "absence_ignored_incomplete": [],
        }
        planned: dict[str, dict[str, Any]] = {}

        for fingerprint_id in observed_ids:
            old = existing.get(fingerprint_id)
            if old is None:
                categories["added_claims"].append(fingerprint_id)
                first_seen_at = publication_rows[fingerprint_id]["first_seen_at"] or observed_at
            else:
                first_seen_at = old["first_seen_at"]
                if old["claim_status"] == "tombstoned":
                    categories["reactivated_claims"].append(fingerprint_id)
                elif old["claim_status"] == "no_longer_observed" or old["missing_streak"]:
                    categories["recovered_claims"].append(fingerprint_id)
                else:
                    categories["unchanged"].append(fingerprint_id)
            planned[fingerprint_id] = {
                "fingerprint_id": fingerprint_id,
                "person_id": person_id,
                "institution_id": institution_id,
                "source_url": source_url,
                "source_kind": source_kind,
                "claim_status": "active",
                "missing_streak": 0,
                "first_seen_at": first_seen_at,
                "last_seen_at": observed_at,
                "last_seen_run_id": run_id,
                "last_checked_at": observed_at,
                "tombstoned_at": None,
            }

        for fingerprint_id, old in existing.items():
            if fingerprint_id in observed_set:
                continue
            if not complete:
                categories["absence_ignored_incomplete"].append(fingerprint_id)
                continue
            streak = int(old["missing_streak"] or 0) + 1
            status = "tombstoned" if streak >= threshold else "no_longer_observed"
            if status == "tombstoned":
                categories["tombstoned"].append(fingerprint_id)
            else:
                categories["no_longer_observed"].append(fingerprint_id)
            planned[fingerprint_id] = {
                "fingerprint_id": fingerprint_id,
                "person_id": person_id,
                "institution_id": institution_id,
                "source_url": source_url,
                "source_kind": source_kind,
                "claim_status": status,
                "missing_streak": streak,
                "first_seen_at": old["first_seen_at"],
                "last_seen_at": old["last_seen_at"],
                "last_seen_run_id": old["last_seen_run_id"],
                "last_checked_at": observed_at,
                "tombstoned_at": observed_at if status == "tombstoned" else None,
            }

        current_statuses = set(_CURRENT_PUBLICATION_CLAIM_STATUSES)
        visibility_changes: dict[str, str] = {}
        for fingerprint_id, claim in planned.items():
            all_claims = self.conn.execute(
                """
                SELECT source_kind, source_url, claim_status
                FROM official_publication_source_claims
                WHERE fingerprint_id=?
                """,
                (fingerprint_id,),
            ).fetchall()
            before_current = any(row["claim_status"] in current_statuses for row in all_claims)
            other_current = any(
                row["claim_status"] in current_statuses
                and not (
                    row["source_kind"] == source_kind
                    and row["source_url"] == source_url
                )
                for row in all_claims
            )
            after_current = other_current or claim["claim_status"] in current_statuses
            if not before_current and after_current:
                visibility_changes[fingerprint_id] = "publication_reactivated"
            elif before_current and not after_current:
                visibility_changes[fingerprint_id] = "publication_tombstoned"
            elif fingerprint_id in categories["added_claims"] and not all_claims:
                visibility_changes[fingerprint_id] = "publication_added"

        enqueued: list[dict[str, Any]] = []
        if not dry_run:
            try:
                for claim in planned.values():
                    payload = dict(claim)
                    self.conn.execute(
                        """
                        INSERT INTO official_publication_source_claims
                        (fingerprint_id, person_id, institution_id, source_url,
                         source_kind, claim_status, missing_streak, first_seen_at,
                         last_seen_at, last_seen_run_id, last_checked_at,
                         tombstoned_at, record_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(fingerprint_id, source_kind, source_url) DO UPDATE SET
                            person_id=excluded.person_id,
                            institution_id=excluded.institution_id,
                            claim_status=excluded.claim_status,
                            missing_streak=excluded.missing_streak,
                            first_seen_at=excluded.first_seen_at,
                            last_seen_at=excluded.last_seen_at,
                            last_seen_run_id=excluded.last_seen_run_id,
                            last_checked_at=excluded.last_checked_at,
                            tombstoned_at=excluded.tombstoned_at,
                            record_json=excluded.record_json
                        """,
                        (
                            claim["fingerprint_id"],
                            claim["person_id"],
                            claim["institution_id"],
                            claim["source_url"],
                            claim["source_kind"],
                            claim["claim_status"],
                            claim["missing_streak"],
                            claim["first_seen_at"],
                            claim["last_seen_at"],
                            claim["last_seen_run_id"],
                            claim["last_checked_at"],
                            claim["tombstoned_at"],
                            _json(payload),
                        ),
                    )
                if enqueue_vectors:
                    if visibility_changes:
                        # Official-page titles are identity clues, not final
                        # embedding input.  First refresh the confirmed
                        # OpenAlex works manifest; that stage alone may enqueue
                        # affected paper/career vector rebuilds.
                        reason = "official_publication_claim_changed"
                        queue_id, created = self._enqueue_vector_dirty_no_commit(
                            "openalex_works_sync",
                            person_id,
                            reason,
                            run_id=run_id,
                            person_id=person_id,
                            payload={"visibility_changes": visibility_changes},
                        )
                        enqueued.append(
                            {
                                "queue_id": queue_id,
                                "created": created,
                                "entity_kind": "openalex_works_sync",
                                "entity_id": person_id,
                                "reason": reason,
                            }
                        )

                canonical_row = self.conn.execute(
                    "SELECT record_json FROM canonical_pi_records WHERE person_id=?",
                    (person_id,),
                ).fetchone()
                if canonical_row is not None:
                    canonical = _pi_from_json(canonical_row["record_json"])
                    canonical.publications_summary = self.publication_summary(person_id)
                    self.conn.execute(
                        """
                        UPDATE canonical_pi_records
                        SET record_json=?, updated_at=?
                        WHERE person_id=?
                        """,
                        (canonical.to_json(), observed_at, person_id),
                    )
                if commit:
                    self.conn.commit()
            except Exception:
                if commit:
                    self.conn.rollback()
                raise

        result: dict[str, Any] = {
            "person_id": person_id,
            "source_url": source_url,
            "source_kind": source_kind,
            "run_id": run_id,
            "complete": bool(complete),
            "dry_run": bool(dry_run),
            **categories,
            "visibility_changes": visibility_changes,
            "vector_jobs": enqueued,
        }
        result["counts"] = {
            key: len(value)
            for key, value in categories.items()
        } | {
            "visibility_changes": len(visibility_changes),
            "vector_jobs": len(enqueued),
        }
        return result

    def publication_summary(self, person_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n, MAX(f.publication_year) AS latest_year,
                   SUM(CASE WHEN f.doi IS NOT NULL AND f.doi!='' THEN 1 ELSE 0 END) AS doi_count
            FROM official_publication_fingerprints f
            WHERE f.person_id=?
              AND publication_is_meaningful(
                    f.title, f.citation_text, f.publication_year, f.doi, f.publication_url
                  )=1
              AND (
                    NOT EXISTS (
                        SELECT 1 FROM official_publication_source_claims c
                        WHERE c.fingerprint_id=f.fingerprint_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM official_publication_source_claims c
                        WHERE c.fingerprint_id=f.fingerprint_id
                          AND c.claim_status IN ('active', 'no_longer_observed')
                    )
              )
            """,
            (person_id,),
        ).fetchone()
        return {
            "official_fingerprint_count": int(row["n"] or 0),
            "official_fingerprint_latest_year": row["latest_year"],
            "official_fingerprint_doi_count": int(row["doi_count"] or 0),
        }

    def publication_text_by_person(
        self,
        person_ids: Iterable[str],
        limit: int = 20,
    ) -> dict[str, list[str]]:
        """Load searchable, meaningful official publication text per person.

        The predicate is deliberately re-applied while reading.  Older databases
        can contain navigation labels or aggregate Pure facets that predate the
        parser-side quality gate; those rows remain available for provenance but
        must never become research evidence or influence recommendations.
        """
        if isinstance(person_ids, str):
            normalized_ids = [person_ids]
        else:
            normalized_ids = list(dict.fromkeys(str(value) for value in person_ids if value))
        if not normalized_ids or limit <= 0:
            return {}

        result: dict[str, list[str]] = {}
        seen_by_person: dict[str, set[str]] = {}
        count_by_person: dict[str, int] = {}
        # Stay below SQLite's common 999-parameter limit while supporting large
        # regional pools in a single API call.
        for offset in range(0, len(normalized_ids), 800):
            chunk = normalized_ids[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"""
                SELECT f.person_id, f.title, f.citation_text, f.publication_year, f.doi,
                       f.publication_url, f.fingerprint_id
                FROM official_publication_fingerprints f
                WHERE f.person_id IN ({placeholders})
                  AND (
                        NOT EXISTS (
                            SELECT 1 FROM official_publication_source_claims c
                            WHERE c.fingerprint_id=f.fingerprint_id
                        )
                        OR EXISTS (
                            SELECT 1 FROM official_publication_source_claims c
                            WHERE c.fingerprint_id=f.fingerprint_id
                              AND c.claim_status IN ('active', 'no_longer_observed')
                        )
                  )
                ORDER BY f.person_id,
                         CASE WHEN f.publication_year IS NULL THEN 1 ELSE 0 END,
                         f.publication_year DESC,
                         f.fingerprint_id
                """,
                chunk,
            ).fetchall()
            for row in rows:
                person_id = row["person_id"]
                if count_by_person.get(person_id, 0) >= limit:
                    continue
                fingerprint = dict(row)
                if not is_meaningful_publication_fingerprint(fingerprint):
                    continue
                title = " ".join((row["title"] or "").split())
                citation = " ".join((row["citation_text"] or "").split())
                if citation and citation.casefold() != title.casefold():
                    text = f"{title}. {citation}" if title else citation
                else:
                    text = title or citation
                text = text.strip(" .")
                key = text.casefold()
                seen = seen_by_person.setdefault(person_id, set())
                if not text or key in seen:
                    continue
                seen.add(key)
                result.setdefault(person_id, []).append(text)
                count_by_person[person_id] = count_by_person.get(person_id, 0) + 1
        return result

    def purge_invalid_publication_fingerprints(
        self,
        institution_id: str | None = None,
    ) -> dict[str, int]:
        """Explicitly remove legacy non-publication fingerprints.

        This operation is intentionally never run from ``init_db``.  Callers can
        first audit the old rows, then opt in for a specific institution or the
        whole database.  Canonical publication summaries are refreshed for every
        affected person after the deletion.
        """
        sql = """
            SELECT fingerprint_id, person_id, title, citation_text,
                   publication_year, doi, publication_url
            FROM official_publication_fingerprints
        """
        params: tuple[str, ...] = ()
        if institution_id:
            sql += " WHERE institution_id=?"
            params = (institution_id,)
        rows = self.conn.execute(sql, params).fetchall()
        invalid_rows = [
            row
            for row in rows
            if not is_meaningful_publication_fingerprint(dict(row))
        ]
        if not invalid_rows:
            return {"scanned": len(rows), "deleted": 0, "people_refreshed": 0}

        affected_person_ids = {row["person_id"] for row in invalid_rows}
        self.conn.executemany(
            "DELETE FROM official_publication_fingerprints WHERE fingerprint_id=?",
            [(row["fingerprint_id"],) for row in invalid_rows],
        )

        people_refreshed = 0
        for person_id in sorted(affected_person_ids):
            row = self.conn.execute(
                "SELECT record_json FROM canonical_pi_records WHERE person_id=?",
                (person_id,),
            ).fetchone()
            if row is None:
                continue
            record = _pi_from_json(row["record_json"])
            record.publications_summary = self.publication_summary(person_id)
            self.conn.execute(
                "UPDATE canonical_pi_records SET record_json=?, updated_at=? WHERE person_id=?",
                (record.to_json(), utc_now_iso(), person_id),
            )
            people_refreshed += 1
        self.conn.commit()
        return {
            "scanned": len(rows),
            "deleted": len(invalid_rows),
            "people_refreshed": people_refreshed,
        }

    def reconcile_pi_membership(
        self,
        institution_id: str,
        run_id: str,
        seen_person_ids: set[str],
        *,
        crawl_complete: bool,
        missing_runs_before_inactive: int = 2,
    ) -> dict[str, int]:
        counts = {"active": len(seen_person_ids), "newly_missing": 0, "newly_inactive": 0}
        if not crawl_complete:
            return counts
        rows = self.conn.execute(
            "SELECT person_id, record_json FROM canonical_pi_records WHERE institution_id=?",
            (institution_id,),
        ).fetchall()
        for row in rows:
            if row["person_id"] in seen_person_ids:
                continue
            record = _pi_from_json(row["record_json"])
            record.missing_streak = int(record.missing_streak or 0) + 1
            if record.missing_streak >= max(1, missing_runs_before_inactive):
                if record.membership_status != "inactive":
                    counts["newly_inactive"] += 1
                record.membership_status = "inactive"
                record.current_affiliation_confidence = "none"
            else:
                if record.membership_status == "active":
                    counts["newly_missing"] += 1
                record.membership_status = "missing"
                record.current_affiliation_confidence = "low"
            record.schema_version = 2
            self.upsert_pi_record(record)
            verdict_row = self.conn.execute(
                "SELECT record_json FROM contact_verdicts WHERE person_id=?",
                (record.person_id,),
            ).fetchone()
            if verdict_row:
                verdict = _verdict_from_json(verdict_row["record_json"])
                verdict.current_affiliation_confidence = record.current_affiliation_confidence
                verdict.run_id = run_id
                self.upsert_contact_verdict(verdict)
        return counts

    def insert_email_evidence(self, record: EmailEvidence) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO email_evidence
            (email, source_url, person_id, source_type, domain_aligned, official_source,
             extracted_at, confidence, verdict, association, run_id, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.email.lower(),
                record.source_url,
                record.person_id,
                record.source_type,
                int(record.domain_aligned),
                int(record.official_source),
                record.extracted_at,
                record.confidence,
                record.verdict,
                record.association,
                record.run_id,
                record.to_json(),
            ),
        )
        self.conn.commit()

    def upsert_contact_verdict(self, record: PIContactVerdict) -> None:
        self.conn.execute(
            """
            INSERT INTO contact_verdicts
            (person_id, verdict, reasons_json, recommended_action, last_live_checked_at,
             contact_confidence, topic_match_confidence,
             current_affiliation_confidence, run_id, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(person_id) DO UPDATE SET
                verdict=excluded.verdict,
                reasons_json=excluded.reasons_json,
                recommended_action=excluded.recommended_action,
                last_live_checked_at=excluded.last_live_checked_at,
                contact_confidence=excluded.contact_confidence,
                topic_match_confidence=excluded.topic_match_confidence,
                current_affiliation_confidence=excluded.current_affiliation_confidence,
                run_id=excluded.run_id,
                record_json=excluded.record_json
            """,
            (
                record.person_id,
                record.verdict,
                _json(record.reasons),
                record.recommended_action,
                record.last_live_checked_at,
                record.contact_confidence,
                record.topic_match_confidence,
                record.current_affiliation_confidence,
                record.run_id,
                record.to_json(),
            ),
        )
        self.conn.commit()

    def record_crawl_error(
        self,
        institution_id: str | None,
        source_url: str | None,
        stage: str,
        reason: str,
        run_id: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO crawl_errors (institution_id, source_url, stage, reason, created_at, run_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (institution_id, source_url, stage, reason, utc_now_iso(), run_id),
        )
        self.conn.commit()

    def insert_match_result(
        self,
        applicant_source: str,
        person_id: str,
        display_name: str,
        institution_name: str,
        match_score: float,
        topic_score: float,
        contact_score: float,
        institution_score: float,
        total_score: float,
        topic_overlap: str,
        contact_verdict: str,
        explanation: str,
        institution_fit_score: float | None = None,
        research_fit_score: float | None = None,
    ) -> None:
        institution_fit_score = institution_score if institution_fit_score is None else institution_fit_score
        research_fit_score = topic_score if research_fit_score is None else research_fit_score
        self.conn.execute(
            """
            INSERT INTO match_results
            (applicant_source, person_id, display_name, institution_name, match_score,
             institution_fit_score, research_fit_score,
             topic_score, contact_score, institution_score, total_score,
             topic_overlap, contact_verdict, explanation, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                applicant_source,
                person_id,
                display_name,
                institution_name,
                match_score,
                institution_fit_score,
                research_fit_score,
                topic_score,
                contact_score,
                institution_score,
                total_score,
                topic_overlap,
                contact_verdict,
                explanation,
                utc_now_iso(),
            ),
        )
        self.conn.commit()

    def iter_pi_records(self, include_inactive: bool = False) -> Iterable[CanonicalPIRecord]:
        sql = "SELECT record_json FROM canonical_pi_records"
        if not include_inactive:
            sql += " WHERE COALESCE(membership_status, 'active')!='inactive'"
        rows = self.conn.execute(sql).fetchall()
        for row in rows:
            yield _pi_from_json(row["record_json"])

    def get_contact_verdicts(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT person_id, verdict FROM contact_verdicts").fetchall()
        return {row["person_id"]: row["verdict"] for row in rows}

    def get_contact_verdict_records(self) -> dict[str, PIContactVerdict]:
        rows = self.conn.execute("SELECT record_json FROM contact_verdicts").fetchall()
        verdicts = [_verdict_from_json(row["record_json"]) for row in rows]
        return {verdict.person_id: verdict for verdict in verdicts}

    def get_pi_record(self, person_id: str) -> CanonicalPIRecord | None:
        row = self.conn.execute("SELECT record_json FROM canonical_pi_records WHERE person_id=?", (person_id,)).fetchone()
        if not row:
            return None
        return _pi_from_json(row["record_json"])

    def find_existing_duplicate(
        self,
        record: CanonicalPIRecord,
        *,
        candidate_person_ids: set[str] | None = None,
    ) -> tuple[str, str] | None:
        if record.institution_id not in self._identity_index_checked_institutions:
            self.sync_identity_index(record.institution_id)
        record_name = _normalize_key_part(record.display_name)
        record_emails = _safe_email_identities(record)
        record_exact_profiles = {
            normalize_profile_url(value)
            for value in _record_profile_urls(record)
            if _is_official_exact_profile_candidate(value, record)
        }
        record_external = _strong_external_identities(record)
        record_slugs = {_profile_slug(value) for value in _record_profile_urls(record)} - {""}
        lookup_entries = _identity_index_entries(record) - {_IDENTITY_INDEX_SENTINEL}
        candidate_ids: set[str] = set()
        ordered_entries = sorted(lookup_entries)
        for offset in range(0, len(ordered_entries), _IDENTITY_INDEX_QUERY_CHUNK):
            chunk = ordered_entries[offset : offset + _IDENTITY_INDEX_QUERY_CHUNK]
            wanted = ", ".join("(?, ?)" for _entry in chunk)
            params: list[str] = [item for entry in chunk for item in entry]
            params.extend([record.institution_id, record.person_id])
            rows = self.conn.execute(
                f"""
                WITH wanted(identity_kind, identity_value) AS (VALUES {wanted})
                SELECT DISTINCT k.person_id
                FROM pi_identity_keys AS k
                JOIN wanted AS w
                  ON w.identity_kind=k.identity_kind
                 AND w.identity_value=k.identity_value
                WHERE k.institution_id=? AND k.person_id!=?
                """,
                params,
            ).fetchall()
            candidate_ids.update(str(row["person_id"]) for row in rows)
        if not candidate_ids:
            return None
        if candidate_person_ids is not None:
            candidate_ids.intersection_update(candidate_person_ids)
            if not candidate_ids:
                return None

        rows: list[sqlite3.Row] = []
        ordered_candidate_ids = sorted(candidate_ids)
        for offset in range(0, len(ordered_candidate_ids), 900):
            chunk = ordered_candidate_ids[offset : offset + 900]
            placeholders = ", ".join("?" for _person_id in chunk)
            rows.extend(
                self.conn.execute(
                    f"""
                    SELECT person_id, record_json
                    FROM canonical_pi_records
                    WHERE institution_id=? AND person_id IN ({placeholders})
                    """,
                    [record.institution_id, *chunk],
                ).fetchall()
            )

        candidates: list[tuple[int, str, str, str]] = []
        priority_and_reason = [
            ("orcid", 0, "same_orcid"),
            ("openalex_author_id", 1, "same_openalex_author_id"),
            ("official_person_id", 2, "same_official_person_id"),
            ("scopus_author_id", 3, "same_scopus_author_id"),
            ("google_scholar_id", 4, "same_google_scholar_id"),
        ]
        for row in rows:
            existing = _pi_from_json(row["record_json"])
            if row["person_id"] == record.person_id:
                continue
            existing_name = _normalize_key_part(existing.display_name)
            existing_external = _strong_external_identities(existing)
            matched = False
            for kind, priority, reason in priority_and_reason:
                if record_external[kind].intersection(existing_external[kind]):
                    candidates.append((priority, existing.first_seen_at or "9999", existing.person_id, reason))
                    matched = True
                    break
            if matched:
                continue

            # Same-name rows must not be combined through a weak profile/email
            # fallback when independent official pages describe different
            # departments and contact identities.  Keep such pairs distinct for
            # explicit review; a shared ORCID/OpenAlex/official person ID above
            # remains sufficient to resolve a genuine cross-appointment.
            if _has_separating_official_identity_conflict(record, existing):
                continue

            existing_exact_profiles = {
                normalize_profile_url(value)
                for value in _record_profile_urls(existing)
                if _is_official_exact_profile_candidate(value, existing)
            }
            shared_exact_profiles = record_exact_profiles.intersection(existing_exact_profiles)
            existing_emails = _safe_email_identities(existing)
            shared_person_emails = record_emails.intersection(existing_emails)
            if shared_exact_profiles and _names_compatible_on_exact_profile(record, existing):
                reason = (
                    "same_normalized_name_and_profile_url"
                    if existing_name == record_name
                    else "same_profile_url_with_name_alias"
                )
                candidates.append((5, existing.first_seen_at or "9999", existing.person_id, reason))
                continue
            # A previous parser version could mistake an all-caps surname such
            # as FUNG/FONG for a fellowship suffix, leaving ``Mr Keith`` beside
            # the corrected ``Keith FUNG``.  Do not weaken name matching to
            # repair that history.  Instead require two independent,
            # person-local official anchors: the exact person-specific profile
            # URL and the exact non-role email.
            if shared_exact_profiles and shared_person_emails:
                candidates.append(
                    (
                        5,
                        existing.first_seen_at or "9999",
                        existing.person_id,
                        "same_profile_url_and_person_email",
                    )
                )
                continue
            if record_emails.intersection(existing_emails) and _names_compatible(record, existing):
                reason = (
                    "same_normalized_name_and_email"
                    if existing_name == record_name
                    else "same_email_with_name_alias"
                )
                candidates.append((6, existing.first_seen_at or "9999", existing.person_id, reason))
                continue
            if existing_name == record_name:
                existing_slugs = {_profile_slug(value) for value in _record_profile_urls(existing)} - {""}
                if record_slugs.intersection(existing_slugs):
                    candidates.append(
                        (7, existing.first_seen_at or "9999", existing.person_id, "same_normalized_name_and_profile_slug")
                    )
        if not candidates:
            return None
        _priority, _first_seen, person_id, reason = min(candidates)
        return person_id, reason

    def find_unresolved_duplicate_groups(
        self,
        institution_id: str,
        person_ids: Iterable[str] | None = None,
    ) -> list[list[str]]:
        """Return still-distinct canonical records joined by safe identity.

        Rows in ``duplicates`` are a history of successful identity merges and
        must not be treated as unresolved data-quality failures.  This audit
        instead reruns the production identity rules against the canonical
        records that still exist.  Those rules require a strong external ID,
        or a person-specific profile/email plus compatible names; identical
        display names alone never create an edge.
        """

        requested = set(person_ids) if person_ids is not None else None
        rows = self.conn.execute(
            """
            SELECT person_id, record_json
            FROM canonical_pi_records
            WHERE institution_id=?
              AND COALESCE(membership_status, 'active')!='inactive'
            ORDER BY person_id
            """,
            (institution_id,),
        ).fetchall()
        records = {
            str(row["person_id"]): _pi_from_json(row["record_json"])
            for row in rows
            if requested is None or str(row["person_id"]) in requested
        }
        eligible = set(records)
        if len(eligible) < 2:
            return []

        parent = {person_id: person_id for person_id in eligible}

        def find(person_id: str) -> str:
            while parent[person_id] != person_id:
                parent[person_id] = parent[parent[person_id]]
                person_id = parent[person_id]
            return person_id

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root == right_root:
                return
            kept, merged = sorted((left_root, right_root))
            parent[merged] = kept

        for person_id, record in records.items():
            duplicate = self.find_existing_duplicate(
                record,
                candidate_person_ids=eligible,
            )
            if duplicate:
                union(person_id, duplicate[0])

        groups: dict[str, list[str]] = {}
        for person_id in sorted(eligible):
            groups.setdefault(find(person_id), []).append(person_id)
        return sorted(
            (members for members in groups.values() if len(members) > 1),
            key=lambda members: tuple(members),
        )

    def preferred_canonical_person_id(self, *person_ids: str) -> str:
        candidates: list[tuple[str, str]] = []
        for person_id in set(person_ids):
            row = self.conn.execute(
                "SELECT first_seen_at FROM canonical_pi_records WHERE person_id=?",
                (person_id,),
            ).fetchone()
            if row:
                candidates.append((row["first_seen_at"] or "9999", person_id))
        if not candidates:
            raise ValueError("No canonical PI records were found for identity consolidation")
        return min(candidates)[1]

    def consolidate_person_ids(
        self,
        duplicate_person_id: str,
        canonical_person_id: str,
        institution_id: str,
        reason: str,
        run_id: str,
        *,
        commit: bool = True,
    ) -> str:
        """Merge two reviewed identities and retain an alias for the removed ID.

        By default this method owns its transaction, preserving the historical
        call contract.  Batch callers may pass ``commit=False`` while holding an
        outer transaction so several reviewed consolidations succeed or roll
        back together.
        """
        duplicate_person_id = self.resolve_person_id(duplicate_person_id)
        canonical_person_id = self.resolve_person_id(canonical_person_id)
        if duplicate_person_id == canonical_person_id:
            return canonical_person_id
        rows = self.conn.execute(
            "SELECT person_id, institution_id, record_json FROM canonical_pi_records WHERE person_id IN (?, ?)",
            (duplicate_person_id, canonical_person_id),
        ).fetchall()
        if len(rows) != 2 or {row["institution_id"] for row in rows} != {institution_id}:
            raise ValueError("PI identity consolidation requires two records from the same institution")

        records = {
            str(row["person_id"]): _pi_from_json(row["record_json"])
            for row in rows
        }
        canonical_record = records[canonical_person_id]
        duplicate_record = records[duplicate_person_id]

        # The reviewer-selected canonical row wins scalar conflicts.  A scalar
        # from the duplicate only fills a genuine gap; evidence-bearing
        # collections are combined below.
        canonical_name_invalid = is_non_person_name(
            canonical_record.display_name
        ) or is_title_contaminated_name(canonical_record.display_name)
        duplicate_name_valid = not (
            is_non_person_name(duplicate_record.display_name)
            or is_title_contaminated_name(duplicate_record.display_name)
        )
        promoted_duplicate_name = canonical_name_invalid and duplicate_name_valid
        if promoted_duplicate_name:
            canonical_record.display_name = duplicate_record.display_name
            canonical_record.given_name = duplicate_record.given_name
            canonical_record.family_name = duplicate_record.family_name
        else:
            canonical_record.display_name = (
                canonical_record.display_name or duplicate_record.display_name
            )
            canonical_record.given_name = (
                canonical_record.given_name or duplicate_record.given_name
            )
            canonical_record.family_name = (
                canonical_record.family_name or duplicate_record.family_name
            )
        canonical_record.institution_name = (
            canonical_record.institution_name or duplicate_record.institution_name
        )
        canonical_record.ror_id = canonical_record.ror_id or duplicate_record.ror_id
        canonical_record.title = _merge_parallel_titles(
            canonical_record.title,
            duplicate_record.title,
        )
        canonical_record.department = canonical_record.department or duplicate_record.department
        canonical_record.profile_url = next(
            (
                value
                for value in (
                    canonical_record.profile_url,
                    duplicate_record.profile_url,
                )
                if value and not is_unusable_profile_url(value)
            ),
            None,
        )
        canonical_record.lab_url = canonical_record.lab_url or duplicate_record.lab_url
        canonical_record.pool_scope = canonical_record.pool_scope or duplicate_record.pool_scope

        def unique_text(values: Iterable[str | None]) -> list[str]:
            result: list[str] = []
            seen: set[str] = set()
            for value in values:
                if not value or not value.strip():
                    continue
                normalized = " ".join(value.split()).casefold()
                if normalized in seen:
                    continue
                seen.add(normalized)
                result.append(value)
            return result

        canonical_record.departments = unique_text(
            [
                canonical_record.department,
                *(canonical_record.departments or []),
                duplicate_record.department,
                *(duplicate_record.departments or []),
            ]
        )
        canonical_record.research_areas = unique_text(
            [
                *(canonical_record.research_areas or []),
                *(duplicate_record.research_areas or []),
            ]
        )
        canonical_record.field_sources = {
            **(duplicate_record.field_sources or {}),
            **(canonical_record.field_sources or {}),
        }
        if promoted_duplicate_name and (duplicate_record.field_sources or {}).get(
            "display_name"
        ):
            canonical_record.field_sources["display_name"] = (
                duplicate_record.field_sources["display_name"]
            )
        canonical_record.source_evidence_ids = sorted(
            set(
                (canonical_record.source_evidence_ids or [])
                + (duplicate_record.source_evidence_ids or [])
            )
        )

        first_seen_values = [
            value
            for value in (canonical_record.first_seen_at, duplicate_record.first_seen_at)
            if value
        ]
        if first_seen_values:
            canonical_record.first_seen_at = min(first_seen_values)
        if (
            duplicate_record.last_seen_at
            and (
                not canonical_record.last_seen_at
                or duplicate_record.last_seen_at > canonical_record.last_seen_at
            )
        ):
            canonical_record.last_seen_at = duplicate_record.last_seen_at
            canonical_record.last_seen_run_id = duplicate_record.last_seen_run_id
        if (
            duplicate_record.last_checked_at
            and duplicate_record.last_checked_at > canonical_record.last_checked_at
        ):
            canonical_record.last_checked_at = duplicate_record.last_checked_at

        canonical_external_ids = canonical_record.external_ids or {}
        duplicate_external_ids = duplicate_record.external_ids or {}
        merged_external_ids: dict[str, Any] = {}
        for key in sorted(set(canonical_external_ids).union(duplicate_external_ids)):
            values: list[Any] = []
            for source in (canonical_external_ids, duplicate_external_ids):
                value = source.get(key)
                candidates = value if isinstance(value, list) else [value]
                for candidate in candidates:
                    if candidate not in (None, "") and candidate not in values:
                        values.append(candidate)
            if values:
                merged_external_ids[key] = values[0] if len(values) == 1 else values
        canonical_record.external_ids = merged_external_ids

        profile_urls = [
            canonical_record.profile_url,
            *(canonical_record.profile_urls or []),
            duplicate_record.profile_url,
            *(duplicate_record.profile_urls or []),
        ]
        canonical_record.profile_urls = list(
            dict.fromkeys(
                value
                for value in profile_urls
                if value and not is_unusable_profile_url(value)
            )
        )
        aliases = {
            value
            for value in [
                *(canonical_record.aliases or []),
                *(duplicate_record.aliases or []),
            ]
            if value
            and not is_non_person_name(value)
            and not is_title_contaminated_name(value)
        }
        if duplicate_record.display_name != canonical_record.display_name:
            if (
                not is_non_person_name(duplicate_record.display_name)
                and not is_title_contaminated_name(duplicate_record.display_name)
            ):
                aliases.add(duplicate_record.display_name)
        aliases.discard(canonical_record.display_name)
        canonical_record.aliases = sorted(aliases)
        if (
            duplicate_record.email_association == "person_local"
            and canonical_record.email_association == "person_local"
        ):
            canonical_record.emails = sorted(
                set(canonical_record.emails + duplicate_record.emails)
            )
        elif (
            duplicate_record.email_association == "person_local"
            and duplicate_record.emails
        ):
            # An empty person-local observation is not evidence that a
            # canonical address is wrong.  In particular, a sparse directory
            # duplicate must not erase a non-empty profile address merely
            # because its association label was more optimistic.
            canonical_record.emails = sorted(set(duplicate_record.emails))
            canonical_record.email_association = "person_local"

        observed_at = utc_now_iso()
        transaction = self.conn if commit else nullcontext()
        with transaction:
            for row in self.conn.execute(
                "SELECT observation_id, record_json FROM pi_observations WHERE person_id=?",
                (duplicate_person_id,),
            ).fetchall():
                payload = _loads(row["record_json"], {})
                payload["person_id"] = canonical_person_id
                self.conn.execute(
                    "UPDATE pi_observations SET person_id=?, record_json=? WHERE observation_id=?",
                    (canonical_person_id, _json(payload), row["observation_id"]),
                )

            for row in self.conn.execute(
                "SELECT evidence_id, record_json FROM person_evidence WHERE person_temp_id=?",
                (duplicate_person_id,),
            ).fetchall():
                payload = _loads(row["record_json"], {})
                payload["person_temp_id"] = canonical_person_id
                self.conn.execute(
                    """
                    UPDATE person_evidence
                    SET person_temp_id=?, record_json=?
                    WHERE evidence_id=?
                    """,
                    (canonical_person_id, _json(payload), row["evidence_id"]),
                )

            email_rows = self.conn.execute(
                "SELECT * FROM email_evidence WHERE person_id=?",
                (duplicate_person_id,),
            ).fetchall()
            for row in email_rows:
                payload = _loads(row["record_json"], {})
                payload["person_id"] = canonical_person_id
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO email_evidence
                    (email, source_url, person_id, source_type, domain_aligned, official_source,
                     extracted_at, confidence, verdict, association, run_id, record_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["email"],
                        row["source_url"],
                        canonical_person_id,
                        row["source_type"],
                        row["domain_aligned"],
                        row["official_source"],
                        row["extracted_at"],
                        row["confidence"],
                        row["verdict"],
                        row["association"],
                        row["run_id"],
                        _json(payload),
                    ),
                )
            self.conn.execute("DELETE FROM email_evidence WHERE person_id=?", (duplicate_person_id,))

            publication_rows = self.conn.execute(
                "SELECT * FROM official_publication_fingerprints WHERE person_id=?",
                (duplicate_person_id,),
            ).fetchall()
            for row in publication_rows:
                identity_key = (row["doi"] or "").strip().casefold()
                if not identity_key:
                    identity_key = f"{' '.join(row['title'].casefold().split())}|{row['publication_year'] or ''}"
                new_fingerprint_id = stable_id("pubfp", canonical_person_id, identity_key)
                payload = _loads(row["record_json"], {})
                payload["fingerprint_id"] = new_fingerprint_id
                payload["person_id"] = canonical_person_id
                collision = self.conn.execute(
                    "SELECT first_seen_at, last_seen_at FROM official_publication_fingerprints WHERE fingerprint_id=?",
                    (new_fingerprint_id,),
                ).fetchone()
                if collision:
                    self.conn.execute(
                        """
                        UPDATE official_publication_fingerprints
                        SET first_seen_at=MIN(first_seen_at, ?),
                            last_seen_at=MAX(last_seen_at, ?),
                            last_seen_run_id=?, confidence=MAX(confidence, ?)
                        WHERE fingerprint_id=?
                        """,
                        (
                            row["first_seen_at"],
                            row["last_seen_at"],
                            row["last_seen_run_id"],
                            row["confidence"],
                            new_fingerprint_id,
                        ),
                    )
                    self.conn.execute(
                        "DELETE FROM official_publication_fingerprints WHERE fingerprint_id=?",
                        (row["fingerprint_id"],),
                    )
                else:
                    self.conn.execute(
                        """
                        UPDATE official_publication_fingerprints
                        SET fingerprint_id=?, person_id=?, record_json=?
                        WHERE fingerprint_id=?
                        """,
                        (new_fingerprint_id, canonical_person_id, _json(payload), row["fingerprint_id"]),
                    )

            self.conn.execute(
                "UPDATE match_results SET person_id=? WHERE person_id=?",
                (canonical_person_id, duplicate_person_id),
            )
            self.conn.execute(
                "UPDATE pi_identity_aliases SET canonical_person_id=? WHERE canonical_person_id=?",
                (canonical_person_id, duplicate_person_id),
            )
            self.conn.execute(
                """
                INSERT INTO pi_identity_aliases
                (alias_person_id, canonical_person_id, institution_id, reason,
                 first_seen_at, last_seen_at, last_seen_run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(alias_person_id) DO UPDATE SET
                    canonical_person_id=excluded.canonical_person_id,
                    reason=excluded.reason,
                    last_seen_at=excluded.last_seen_at,
                    last_seen_run_id=excluded.last_seen_run_id
                """,
                (
                    duplicate_person_id,
                    canonical_person_id,
                    institution_id,
                    reason,
                    observed_at,
                    observed_at,
                    run_id,
                ),
            )
            self.conn.execute(
                "DELETE FROM pi_identity_keys WHERE person_id=?",
                (duplicate_person_id,),
            )
            self.conn.execute("DELETE FROM canonical_pi_records WHERE person_id=?", (duplicate_person_id,))

            # Publication rows have now moved (and collisions have been
            # collapsed), so derive the summary from the authoritative table
            # instead of retaining either row's stale cached summary.
            canonical_record.publications_summary = self.publication_summary(
                canonical_person_id
            )

            # Re-evaluate contactability from the merged person-local evidence.
            # This prevents a no-email canonical verdict from masking a strong
            # duplicate email after consolidation.
            from .verify.confidence import contact_verdict_for_pi

            evidence_rows = self.conn.execute(
                "SELECT record_json FROM email_evidence WHERE person_id=?",
                (canonical_person_id,),
            ).fetchall()
            email_evidence = [
                EmailEvidence(**_model_payload(EmailEvidence, row["record_json"]))
                for row in evidence_rows
            ]
            verdict = contact_verdict_for_pi(canonical_record, email_evidence)
            verdict.last_live_checked_at = canonical_record.last_checked_at
            verdict.run_id = run_id
            canonical_record.contact_confidence = verdict.contact_confidence
            canonical_record.topic_match_confidence = verdict.topic_match_confidence
            canonical_record.current_affiliation_confidence = (
                verdict.current_affiliation_confidence
            )

            self.conn.execute(
                "DELETE FROM contact_verdicts WHERE person_id IN (?, ?)",
                (canonical_person_id, duplicate_person_id),
            )
            self.conn.execute(
                """
                INSERT INTO contact_verdicts
                (person_id, verdict, reasons_json, recommended_action,
                 last_live_checked_at, contact_confidence,
                 topic_match_confidence, current_affiliation_confidence,
                 run_id, record_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    verdict.person_id,
                    verdict.verdict,
                    _json(verdict.reasons),
                    verdict.recommended_action,
                    verdict.last_live_checked_at,
                    verdict.contact_confidence,
                    verdict.topic_match_confidence,
                    verdict.current_affiliation_confidence,
                    verdict.run_id,
                    verdict.to_json(),
                ),
            )

            self.conn.execute(
                """
                UPDATE canonical_pi_records
                SET display_name=?, institution_id=?, institution_name=?, title=?,
                    department=?, profile_url=?, emails_json=?, research_areas_json=?,
                    contact_confidence=?, topic_match_confidence=?,
                    current_affiliation_confidence=?, dedupe_key=?, first_seen_at=?,
                    last_seen_at=?, last_seen_run_id=?, membership_status=?,
                    missing_streak=?, pool_scope=?, schema_version=?, record_json=?,
                    updated_at=?
                WHERE person_id=?
                """,
                (
                    canonical_record.display_name,
                    canonical_record.institution_id,
                    canonical_record.institution_name,
                    canonical_record.title,
                    canonical_record.department,
                    canonical_record.profile_url,
                    _json(canonical_record.emails),
                    _json(canonical_record.research_areas),
                    canonical_record.contact_confidence,
                    canonical_record.topic_match_confidence,
                    canonical_record.current_affiliation_confidence,
                    dedupe_key_for_record(canonical_record),
                    canonical_record.first_seen_at,
                    canonical_record.last_seen_at,
                    canonical_record.last_seen_run_id,
                    canonical_record.membership_status,
                    canonical_record.missing_streak,
                    canonical_record.pool_scope,
                    canonical_record.schema_version,
                    canonical_record.to_json(),
                    observed_at,
                    canonical_person_id,
                ),
            )
            self._replace_identity_keys(
                institution_id,
                canonical_person_id,
                _identity_index_entries(canonical_record),
            )
        return canonical_person_id

    def resolve_person_id(self, person_id: str, run_id: str | None = None) -> str:
        resolved = person_id
        seen: set[str] = set()
        while resolved not in seen:
            seen.add(resolved)
            row = self.conn.execute(
                "SELECT canonical_person_id FROM pi_identity_aliases WHERE alias_person_id=?",
                (resolved,),
            ).fetchone()
            if not row:
                break
            resolved = row["canonical_person_id"]
        if run_id is not None and resolved != person_id:
            self.conn.execute(
                """
                UPDATE pi_identity_aliases
                SET last_seen_at=?, last_seen_run_id=?
                WHERE alias_person_id=?
                """,
                (utc_now_iso(), run_id, person_id),
            )
            self.conn.commit()
        return resolved

    def upsert_person_id_alias(
        self,
        alias_person_id: str,
        canonical_person_id: str,
        institution_id: str,
        reason: str,
        run_id: str,
    ) -> None:
        canonical_person_id = self.resolve_person_id(canonical_person_id)
        if alias_person_id == canonical_person_id:
            return
        existing = self.conn.execute(
            "SELECT canonical_person_id FROM pi_identity_aliases WHERE alias_person_id=?",
            (alias_person_id,),
        ).fetchone()
        if existing and existing["canonical_person_id"] != canonical_person_id:
            raise ValueError(
                f"Conflicting PI identity alias {alias_person_id}: "
                f"{existing['canonical_person_id']} != {canonical_person_id}"
            )
        observed_at = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO pi_identity_aliases
            (alias_person_id, canonical_person_id, institution_id, reason,
             first_seen_at, last_seen_at, last_seen_run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(alias_person_id) DO UPDATE SET
                canonical_person_id=excluded.canonical_person_id,
                reason=excluded.reason,
                last_seen_at=excluded.last_seen_at,
                last_seen_run_id=excluded.last_seen_run_id
            """,
            (
                alias_person_id,
                canonical_person_id,
                institution_id,
                reason,
                observed_at,
                observed_at,
                run_id,
            ),
        )
        self.conn.commit()

    def record_duplicate(
        self,
        institution_id: str,
        group_key: str,
        kept_person_id: str,
        duplicate_person_id: str,
        reason: str,
        run_id: str | None = None,
        *,
        commit: bool = True,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO duplicates
            (institution_id, group_key, kept_person_id, duplicate_person_id, reason, created_at, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (institution_id, group_key, kept_person_id, duplicate_person_id, reason, utc_now_iso(), run_id),
        )
        if commit:
            self.conn.commit()

    def record_parse_metric(
        self,
        institution_id: str,
        source_url: str,
        parser_name: str,
        candidate_blocks: int,
        people_extracted: int,
        filtered_blocks: int,
        run_id: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO parse_metrics
            (institution_id, source_url, parser_name, candidate_blocks, people_extracted,
             filtered_blocks, created_at, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                institution_id,
                source_url,
                parser_name,
                candidate_blocks,
                people_extracted,
                filtered_blocks,
                utc_now_iso(),
                run_id,
            ),
        )
        self.conn.commit()

    def start_ingestion_run(
        self,
        run_id: str,
        institution_id: str,
        institution_name: str,
        config_name: str,
        config_sha256: str,
        pool_scope: str,
    ) -> None:
        started_at = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO ingestion_runs
            (run_id, institution_id, institution_name, config_name, config_sha256,
             pool_scope, pages_attempted, pages_successfully_fetched, pages_failed,
             people_extracted, emails_extracted, status, created_at, started_at,
             crawl_complete, metrics_json)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 'running', ?, ?, 0, '{}')
            """,
            (
                run_id,
                institution_id,
                institution_name,
                config_name,
                config_sha256,
                pool_scope,
                started_at,
                started_at,
            ),
        )
        self.conn.commit()

    def finish_ingestion_run(
        self,
        run_id: str,
        metrics: dict[str, Any],
        people_extracted: int,
        emails_extracted: int,
        status: str,
        crawl_complete: bool,
    ) -> None:
        self.conn.execute(
            """
            UPDATE ingestion_runs
            SET pages_attempted=?, pages_successfully_fetched=?, pages_failed=?,
                people_extracted=?, emails_extracted=?, status=?, finished_at=?,
                crawl_complete=?, metrics_json=?
            WHERE run_id=?
            """,
            (
                int(metrics.get("pages_attempted", 0)),
                int(metrics.get("pages_succeeded", 0)),
                int(metrics.get("pages_failed", 0)),
                people_extracted,
                emails_extracted,
                status,
                utc_now_iso(),
                int(crawl_complete),
                _json(metrics),
                run_id,
            ),
        )
        self.conn.commit()

    def get_ingestion_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM ingestion_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["metrics"] = _loads(result.pop("metrics_json", None), {})
        return result

    def record_ingestion_run(
        self,
        institution_id: str,
        institution_name: str,
        config_name: str,
        pages_attempted: int,
        pages_successfully_fetched: int,
        pages_failed: int,
        people_extracted: int,
        emails_extracted: int,
        status: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO ingestion_runs
            (institution_id, institution_name, config_name, pages_attempted, pages_successfully_fetched,
             pages_failed, people_extracted, emails_extracted, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                institution_id,
                institution_name,
                config_name,
                pages_attempted,
                pages_successfully_fetched,
                pages_failed,
                people_extracted,
                emails_extracted,
                status,
                utc_now_iso(),
            ),
        )
        self.conn.commit()

    def audit_counts(self) -> dict[str, Any]:
        counts: dict[str, Any] = {}
        for key, sql in {
            "institutions_imported": "SELECT COUNT(*) FROM institutions",
            "institutions_selected_for_batch": "SELECT COUNT(DISTINCT institution_id) FROM ingestion_runs",
            "institutions_attempted_crawl": "SELECT COUNT(DISTINCT institution_id) FROM raw_sources",
            "institutions_successfully_crawled": "SELECT COUNT(DISTINCT institution_id) FROM raw_sources WHERE http_status BETWEEN 200 AND 299 OR http_status=304 OR not_modified=1",
            "institutions_failed": """
                SELECT COUNT(*) FROM (
                    SELECT institution_id
                    FROM ingestion_runs
                    GROUP BY institution_id
                    HAVING SUM(pages_successfully_fetched)=0
                )
            """,
            "pages_attempted": "SELECT COUNT(*) FROM raw_sources",
            "pages_successfully_fetched": "SELECT COUNT(*) FROM raw_sources WHERE http_status BETWEEN 200 AND 299 OR http_status=304 OR not_modified=1",
            "pages_failed": "SELECT COUNT(*) FROM raw_sources WHERE COALESCE(not_modified, 0)=0 AND (http_status IS NULL OR http_status < 200 OR (http_status >= 300 AND http_status != 304))",
            "network_bytes_downloaded": "SELECT COALESCE(SUM(network_bytes), 0) FROM raw_sources",
            "raw_archive_unique_blobs": "SELECT COUNT(*) FROM (SELECT archive_key FROM raw_sources WHERE archive_key IS NOT NULL GROUP BY archive_key)",
            "raw_archive_unique_compressed_bytes": "SELECT COALESCE(SUM(compressed_bytes), 0) FROM (SELECT archive_key, MAX(compressed_bytes) AS compressed_bytes FROM raw_sources WHERE archive_key IS NOT NULL GROUP BY archive_key)",
            "candidate_people_extracted": "SELECT COUNT(DISTINCT person_temp_id) FROM person_evidence",
            "canonical_pi_records_created": "SELECT COUNT(*) FROM canonical_pi_records",
            "canonical_pi_records_current": "SELECT COUNT(*) FROM canonical_pi_records WHERE COALESCE(membership_status, 'active')!='inactive'",
            "canonical_pi_records_missing": "SELECT COUNT(*) FROM canonical_pi_records WHERE membership_status='missing'",
            "canonical_pi_records_inactive": "SELECT COUNT(*) FROM canonical_pi_records WHERE membership_status='inactive'",
            "emails_extracted": "SELECT COUNT(DISTINCT email) FROM email_evidence",
            "high_confidence_contactable": "SELECT COUNT(*) FROM contact_verdicts c JOIN canonical_pi_records p ON p.person_id=c.person_id WHERE c.verdict='high_confidence_contactable' AND c.current_affiliation_confidence IN ('high','medium') AND COALESCE(p.membership_status, 'active')!='inactive'",
            "research_evidence_ready": """
                SELECT COUNT(*) FROM canonical_pi_records p
                WHERE COALESCE(p.membership_status, 'active')!='inactive'
                  AND (p.research_areas_json NOT IN ('[]', '', 'null')
                    OR EXISTS (
                        SELECT 1 FROM official_publication_fingerprints f
                        WHERE f.person_id=p.person_id
                          AND publication_is_meaningful(
                                f.title, f.citation_text, f.publication_year,
                                f.doi, f.publication_url
                              )=1
                    ))
            """,
            "contact_review_required": "SELECT COUNT(*) FROM contact_verdicts c JOIN canonical_pi_records p ON p.person_id=c.person_id WHERE (c.contact_confidence IN ('none','low') OR c.verdict IN ('stale_risk','current_affiliation_conflict','no_official_email')) AND COALESCE(p.membership_status, 'active')!='inactive'",
            "ambiguous_records": "SELECT COUNT(*) FROM contact_verdicts c JOIN canonical_pi_records p ON p.person_id=c.person_id WHERE (c.contact_confidence IN ('none','low') OR c.topic_match_confidence IN ('unknown','low','none')) AND COALESCE(p.membership_status, 'active')!='inactive'",
            "stale_risk": "SELECT COUNT(*) FROM contact_verdicts c JOIN canonical_pi_records p ON p.person_id=c.person_id WHERE c.verdict='stale_risk' AND COALESCE(p.membership_status, 'active')!='inactive'",
            "failures": "SELECT COUNT(*) FROM crawl_errors",
            "ambiguous_non_person_blocks_filtered": "SELECT COALESCE(SUM(filtered_blocks), 0) FROM parse_metrics",
            "duplicate_records": "SELECT COUNT(*) FROM duplicates",
            "pi_identity_aliases": "SELECT COUNT(*) FROM pi_identity_aliases",
            "official_publication_fingerprints": "SELECT COUNT(*) FROM official_publication_fingerprints",
            "meaningful_official_publication_fingerprints": """
                SELECT COUNT(*) FROM official_publication_fingerprints f
                WHERE publication_is_meaningful(
                    f.title, f.citation_text, f.publication_year, f.doi, f.publication_url
                )=1
            """,
        }.items():
            counts[key] = self.conn.execute(sql).fetchone()[0]
        rows = self.conn.execute(
            "SELECT reason, COUNT(*) AS n FROM crawl_errors GROUP BY reason ORDER BY n DESC, reason"
        ).fetchall()
        counts["failure_reasons"] = {row["reason"]: row["n"] for row in rows}
        return counts

    def write_audit_sample(self, out_path: str | Path, sample_size: int = 60) -> int:
        requested = [
            (
                20,
                """
                SELECT p.person_id
                FROM canonical_pi_records p JOIN contact_verdicts c ON c.person_id=p.person_id
                WHERE c.verdict='high_confidence_contactable'
                  AND COALESCE(p.membership_status, 'active')!='inactive'
                ORDER BY RANDOM() LIMIT 20
                """,
            ),
            (
                20,
                """
                SELECT p.person_id
                FROM canonical_pi_records p
                WHERE (p.research_areas_json NOT IN ('[]', '', 'null')
                   OR EXISTS (
                        SELECT 1 FROM official_publication_fingerprints f
                        WHERE f.person_id=p.person_id
                          AND publication_is_meaningful(
                                f.title, f.citation_text, f.publication_year,
                                f.doi, f.publication_url
                              )=1
                   ))
                  AND COALESCE(p.membership_status, 'active')!='inactive'
                ORDER BY RANDOM() LIMIT 20
                """,
            ),
            (
                10,
                """
                SELECT p.person_id
                FROM canonical_pi_records p
                WHERE p.research_areas_json IN ('[]', '', 'null')
                  AND NOT EXISTS (
                        SELECT 1 FROM official_publication_fingerprints f
                        WHERE f.person_id=p.person_id
                          AND publication_is_meaningful(
                                f.title, f.citation_text, f.publication_year,
                                f.doi, f.publication_url
                              )=1
                  )
                  AND COALESCE(p.membership_status, 'active')!='inactive'
                ORDER BY RANDOM() LIMIT 10
                """,
            ),
            (
                10,
                """
                SELECT p.person_id
                FROM canonical_pi_records p JOIN contact_verdicts c ON c.person_id=p.person_id
                WHERE (c.verdict!='high_confidence_contactable'
                   OR c.contact_confidence IN ('none','low')
                   OR c.topic_match_confidence IN ('unknown','low','none'))
                  AND COALESCE(p.membership_status, 'active')!='inactive'
                ORDER BY RANDOM() LIMIT 10
                """,
            ),
        ]
        selected: list[str] = []
        seen: set[str] = set()
        for _limit, sql in requested:
            for row in self.conn.execute(sql).fetchall():
                person_id = row["person_id"]
                if person_id not in seen:
                    selected.append(person_id)
                    seen.add(person_id)
        if len(selected) < sample_size:
            rows = self.conn.execute(
                """
                SELECT person_id FROM canonical_pi_records
                WHERE COALESCE(membership_status, 'active')!='inactive'
                ORDER BY RANDOM()
                """
            ).fetchall()
            for row in rows:
                person_id = row["person_id"]
                if person_id not in seen:
                    selected.append(person_id)
                    seen.add(person_id)
                if len(selected) >= sample_size:
                    break

        fieldnames = [
            "person_id",
            "display_name",
            "title",
            "department",
            "institution_name",
            "email",
            "verdict",
            "contact_confidence",
            "topic_match_confidence",
            "research_evidence_status",
            "source_url",
            "evidence_text",
            "extraction_method",
            "parser_used",
            "source_type",
            "domain_aligned",
            "official_source",
            "reasons",
            "manually_verified_name_email_pair",
            "manually_verified_title",
            "manually_verified_researcher_identity",
            "manually_verified_current_affiliation",
            "audit_error_type",
            "audit_notes",
        ]
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for person_id in selected[:sample_size]:
                writer.writerow(self._audit_row_for_person(person_id, fieldnames))
        return min(len(selected), sample_size)

    def _audit_row_for_person(self, person_id: str, fieldnames: list[str]) -> dict[str, Any]:
        pi_row = self.conn.execute("SELECT record_json FROM canonical_pi_records WHERE person_id=?", (person_id,)).fetchone()
        verdict_row = self.conn.execute("SELECT record_json FROM contact_verdicts WHERE person_id=?", (person_id,)).fetchone()
        if not pi_row:
            return {name: "" for name in fieldnames} | {"person_id": person_id}
        pi = _pi_from_json(pi_row["record_json"])
        verdict = _verdict_from_json(verdict_row["record_json"]) if verdict_row else None
        email_row = self.conn.execute(
            """
            SELECT record_json FROM email_evidence
            WHERE person_id=?
            ORDER BY official_source DESC, domain_aligned DESC, confidence DESC, email
            LIMIT 1
            """,
            (person_id,),
        ).fetchone()
        email_ev = EmailEvidence(**json.loads(email_row["record_json"])) if email_row else None

        evidence_rows = []
        if pi.source_evidence_ids:
            placeholders = ",".join("?" for _ in pi.source_evidence_ids)
            evidence_rows = self.conn.execute(
                f"SELECT record_json FROM person_evidence WHERE evidence_id IN ({placeholders})",
                tuple(pi.source_evidence_ids),
            ).fetchall()
        evidences = [PersonEvidence(**json.loads(row["record_json"])) for row in evidence_rows]
        preferred = next((e for e in evidences if e.field_name == "emails"), None) or next(iter(evidences), None)
        reasons = verdict.reasons if verdict else []
        row = {name: "" for name in fieldnames}
        row.update(
            {
                "person_id": pi.person_id,
                "display_name": pi.display_name,
                "title": pi.title or "",
                "department": pi.department or "",
                "institution_name": pi.institution_name,
                "email": email_ev.email if email_ev else (pi.emails[0] if pi.emails else ""),
                "verdict": verdict.verdict if verdict else "unverified",
                "contact_confidence": verdict.contact_confidence if verdict else pi.contact_confidence,
                "topic_match_confidence": verdict.topic_match_confidence if verdict else pi.topic_match_confidence,
                "research_evidence_status": (
                    "present"
                    if pi.research_areas
                    or self.publication_summary(pi.person_id)["official_fingerprint_count"] > 0
                    else "missing"
                ),
                "source_url": preferred.source_url if preferred else (email_ev.source_url if email_ev else pi.profile_url or ""),
                "evidence_text": preferred.evidence_text if preferred else "",
                "extraction_method": preferred.extraction_method if preferred else "",
                "parser_used": preferred.extraction_method if preferred else "",
                "source_type": preferred.source_type if preferred else (email_ev.source_type if email_ev else ""),
                "domain_aligned": "" if email_ev is None else str(email_ev.domain_aligned).lower(),
                "official_source": "" if email_ev is None else str(email_ev.official_source).lower(),
                "reasons": "; ".join(reasons),
            }
        )
        return row

    def export(self, out_dir: str | Path) -> None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for filename in _DEPRECATED_EXPORT_FILENAMES:
            (out / filename).unlink(missing_ok=True)
        self._export_query(
            out / "institutions.csv",
            "SELECT institution_id, name, country, region, ror_id, homepage_url, qs_rank, qs_year, source, status FROM institutions ORDER BY name",
        )
        self._export_jsonl(
            out / "pi_records.jsonl",
            "SELECT record_json FROM canonical_pi_records WHERE COALESCE(membership_status, 'active')!='inactive' ORDER BY institution_name, display_name",
        )
        self._export_jsonl(
            out / "inactive_pi_records.jsonl",
            "SELECT record_json FROM canonical_pi_records WHERE membership_status='inactive' ORDER BY institution_name, display_name",
        )
        self._export_query(
            out / "pi_identity_aliases.csv",
            """
            SELECT alias_person_id, canonical_person_id, institution_id, reason,
                   first_seen_at, last_seen_at, last_seen_run_id
            FROM pi_identity_aliases
            ORDER BY institution_id, canonical_person_id, alias_person_id
            """,
        )
        self._export_query(
            out / "contact_verdicts.csv",
            """
            SELECT c.person_id, c.verdict, c.contact_confidence,
                   c.topic_match_confidence,
                   c.current_affiliation_confidence, c.reasons_json AS reasons,
                   c.recommended_action, c.last_live_checked_at
            FROM contact_verdicts c JOIN canonical_pi_records p ON p.person_id=c.person_id
            WHERE COALESCE(p.membership_status, 'active')!='inactive'
            ORDER BY c.verdict, c.person_id
            """,
        )
        self._export_query(
            out / "high_confidence_contactable.csv",
            """
            SELECT c.person_id, p.display_name, p.institution_name, p.title,
                   p.emails_json AS emails, c.verdict, c.contact_confidence,
                   c.recommended_action
            FROM contact_verdicts c JOIN canonical_pi_records p ON p.person_id=c.person_id
            WHERE c.verdict='high_confidence_contactable'
              AND c.current_affiliation_confidence IN ('high', 'medium')
              AND COALESCE(p.membership_status, 'active')!='inactive'
            ORDER BY p.institution_name, p.display_name
            """,
        )
        self._export_query(
            out / "research_evidence_ready.csv",
            """
            SELECT p.person_id, p.display_name, p.title, p.department, p.institution_name,
                   p.profile_url, p.emails_json AS emails, p.research_areas_json AS research_areas,
                   (SELECT COUNT(*) FROM official_publication_fingerprints f
                    WHERE f.person_id=p.person_id
                      AND publication_is_meaningful(
                            f.title, f.citation_text, f.publication_year,
                            f.doi, f.publication_url
                          )=1)
                       AS official_publication_count,
                   c.verdict, c.contact_confidence, c.topic_match_confidence,
                   c.current_affiliation_confidence, c.reasons_json AS reasons
            FROM canonical_pi_records p
            JOIN contact_verdicts c ON c.person_id=p.person_id
            WHERE (p.research_areas_json NOT IN ('[]', '', 'null')
               OR EXISTS (
                    SELECT 1 FROM official_publication_fingerprints f
                    WHERE f.person_id=p.person_id
                      AND publication_is_meaningful(
                            f.title, f.citation_text, f.publication_year,
                            f.doi, f.publication_url
                          )=1
               ))
              AND COALESCE(p.membership_status, 'active')!='inactive'
            ORDER BY p.institution_name, p.display_name
            """,
        )
        self._export_query(
            out / "research_evidence_review_queue.csv",
            """
            SELECT p.person_id, p.display_name, p.title, p.department, p.institution_name,
                   p.profile_url, p.emails_json AS emails, p.research_areas_json AS research_areas,
                   c.verdict, c.contact_confidence, c.topic_match_confidence,
                   c.current_affiliation_confidence, c.reasons_json AS reasons
            FROM canonical_pi_records p
            JOIN contact_verdicts c ON c.person_id=p.person_id
            WHERE p.research_areas_json IN ('[]', '', 'null')
              AND NOT EXISTS (
                    SELECT 1 FROM official_publication_fingerprints f
                    WHERE f.person_id=p.person_id
                      AND publication_is_meaningful(
                            f.title, f.citation_text, f.publication_year,
                            f.doi, f.publication_url
                          )=1
              )
              AND COALESCE(p.membership_status, 'active')!='inactive'
            ORDER BY p.institution_name, p.display_name
            """,
        )
        self._export_query(
            out / "contact_review_queue.csv",
            """
            SELECT p.person_id, p.display_name, p.title, p.department, p.institution_name,
                   p.profile_url, p.emails_json AS emails, c.verdict,
                   c.contact_confidence, c.current_affiliation_confidence,
                   c.reasons_json AS reasons
            FROM canonical_pi_records p
            JOIN contact_verdicts c ON c.person_id=p.person_id
            WHERE (c.contact_confidence IN ('none', 'low')
               OR c.verdict IN ('stale_risk', 'current_affiliation_conflict', 'no_official_email'))
              AND COALESCE(p.membership_status, 'active')!='inactive'
            ORDER BY p.institution_name, p.display_name
            """,
        )
        self._export_query(
            out / "stale_risk.csv",
            """
            SELECT c.person_id, p.display_name, p.institution_name, p.title,
                   p.emails_json AS emails, c.verdict, c.contact_confidence,
                   c.current_affiliation_confidence,
                   c.recommended_action
            FROM contact_verdicts c JOIN canonical_pi_records p ON p.person_id=c.person_id
            WHERE c.verdict IN ('stale_risk', 'current_affiliation_conflict')
              AND COALESCE(p.membership_status, 'active')!='inactive'
            ORDER BY p.institution_name, p.display_name
            """,
        )
        self._export_query(
            out / "failures.csv",
            "SELECT institution_id, source_url, stage, reason, created_at FROM crawl_errors ORDER BY created_at",
        )
        self._export_jsonl(out / "evidence.jsonl", "SELECT record_json FROM person_evidence ORDER BY institution_id, person_temp_id")
        self._export_query(
            out / "match_results.csv",
            """
            SELECT applicant_source, person_id, display_name, institution_name, match_score,
                   institution_fit_score, research_fit_score,
                   topic_score, contact_score, institution_score, total_score,
                   topic_overlap, contact_verdict, explanation, created_at
            FROM match_results ORDER BY created_at, total_score DESC
            """,
        )
        self._export_query(
            out / "active_research_pool.csv",
            """
            SELECT p.person_id, p.display_name, p.title, p.department, p.institution_name,
                   p.profile_url, p.emails_json AS emails, p.research_areas_json AS research_areas,
                   c.verdict, c.contact_confidence, c.topic_match_confidence,
                   c.current_affiliation_confidence, c.reasons_json AS reasons
            FROM canonical_pi_records p
            JOIN contact_verdicts c ON c.person_id=p.person_id
            WHERE COALESCE(p.membership_status, 'active')!='inactive'
              AND (p.research_areas_json NOT IN ('[]', '', 'null')
                OR EXISTS (
                    SELECT 1 FROM official_publication_fingerprints f
                    WHERE f.person_id=p.person_id
                      AND publication_is_meaningful(
                            f.title, f.citation_text, f.publication_year,
                            f.doi, f.publication_url
                          )=1
                ))
            ORDER BY p.institution_name, p.display_name
            """,
        )
        self._export_query(
            out / "duplicates.csv",
            """
            SELECT institution_id, group_key, kept_person_id, duplicate_person_id, reason, created_at
            FROM duplicates ORDER BY created_at, group_key
            """,
        )
        self._export_query(
            out / "parse_metrics.csv",
            """
            SELECT institution_id, source_url, parser_name, candidate_blocks,
                   people_extracted, filtered_blocks, created_at
            FROM parse_metrics ORDER BY created_at, source_url, parser_name
            """,
        )
        self.export_institution_quality_report(out / "institution_quality_report.csv")

    def export_institution_quality_report(self, path: str | Path) -> None:
        rows = self.conn.execute(
            """
            SELECT
                i.name AS institution_name,
                COALESCE(MAX(r.config_name), i.source) AS config_name,
                COUNT(DISTINCT rs.source_url) AS pages_attempted,
                COUNT(DISTINCT CASE WHEN rs.http_status BETWEEN 200 AND 299 OR rs.http_status=304 OR rs.not_modified=1 THEN rs.source_url END) AS pages_successfully_fetched,
                COUNT(DISTINCT p.person_id) AS people_extracted,
                COUNT(DISTINCT ee.email) AS emails_extracted,
                COUNT(DISTINCT CASE WHEN c.verdict='high_confidence_contactable' THEN c.person_id END) AS high_confidence_contactable,
                COUNT(DISTINCT CASE WHEN p.research_areas_json NOT IN ('[]', '', 'null')
                    OR EXISTS (
                        SELECT 1 FROM official_publication_fingerprints f
                        WHERE f.person_id=p.person_id
                          AND publication_is_meaningful(
                                f.title, f.citation_text, f.publication_year,
                                f.doi, f.publication_url
                              )=1
                    )
                    THEN p.person_id END) AS research_evidence_ready,
                COUNT(DISTINCT CASE WHEN c.contact_confidence IN ('none','low')
                    OR c.verdict IN ('stale_risk', 'current_affiliation_conflict', 'no_official_email')
                    THEN c.person_id END) AS contact_review_required,
                COUNT(DISTINCT CASE WHEN c.verdict IN ('stale_risk', 'current_affiliation_conflict') THEN c.person_id END) AS stale_risk,
                COUNT(DISTINCT CASE WHEN c.contact_confidence IN ('none','low') OR c.topic_match_confidence IN ('unknown','low','none') THEN c.person_id END) AS ambiguous_records,
                COUNT(DISTINCT ce.id) AS failures,
                COALESCE((
                    SELECT SUM(pm.filtered_blocks)
                    FROM parse_metrics pm
                    WHERE pm.institution_id=i.institution_id
                ), 0) AS ambiguous_non_person_blocks_filtered,
                COALESCE((
                    SELECT COUNT(*)
                    FROM duplicates d
                    WHERE d.institution_id=i.institution_id
                ), 0) AS duplicate_records,
                COALESCE((
                    SELECT pe.extraction_method
                    FROM person_evidence pe
                    WHERE pe.institution_id=i.institution_id
                    GROUP BY pe.extraction_method
                    ORDER BY COUNT(*) DESC, pe.extraction_method
                    LIMIT 1
                ), '') AS dominant_parser_used
            FROM institutions i
            LEFT JOIN ingestion_runs r ON r.institution_id=i.institution_id
            LEFT JOIN raw_sources rs ON rs.institution_id=i.institution_id
            LEFT JOIN canonical_pi_records p ON p.institution_id=i.institution_id
                AND COALESCE(p.membership_status, 'active')!='inactive'
            LEFT JOIN email_evidence ee ON ee.person_id=p.person_id
            LEFT JOIN contact_verdicts c ON c.person_id=p.person_id
            LEFT JOIN crawl_errors ce ON ce.institution_id=i.institution_id
            GROUP BY i.institution_id, i.name, i.source
            ORDER BY i.name
            """
        ).fetchall()
        fieldnames = [
            "institution_name",
            "config_name",
            "pages_attempted",
            "pages_successfully_fetched",
            "people_extracted",
            "emails_extracted",
            "high_confidence_contactable",
            "research_evidence_ready",
            "contact_review_required",
            "stale_risk",
            "ambiguous_records",
            "failures",
            "ambiguous_non_person_blocks_filtered",
            "duplicate_records",
            "dominant_parser_used",
            "notes",
        ]
        with Path(path).open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                data = dict(row)
                notes = []
                if data["people_extracted"] and data["high_confidence_contactable"] > data["people_extracted"] * 0.75:
                    notes.append("high contactable ratio; inspect sample")
                if data["failures"]:
                    notes.append("crawl failures present")
                data["notes"] = "; ".join(notes)
                writer.writerow(data)

    def _export_query(self, path: Path, sql: str) -> None:
        rows = self.conn.execute(sql).fetchall()
        if rows:
            fieldnames = rows[0].keys()
        else:
            probe = self.conn.execute(sql + " LIMIT 0") if "LIMIT" not in sql.upper() else self.conn.execute(sql)
            fieldnames = probe.description and [d[0] for d in probe.description] or []
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))

    def _export_jsonl(self, path: Path, sql: str) -> None:
        rows = self.conn.execute(sql).fetchall()
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(row["record_json"] + "\n")
