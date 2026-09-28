#!/usr/bin/env python3
"""Read-only final acceptance audit for a merged PI index database.

The database is opened with SQLite ``mode=ro`` and ``query_only=ON``.  The
script never creates tables, updates records, or writes beside the database.
When ``--out`` is supplied, only the requested JSON report file is written.

Run from the repository root, for example::

    python scripts/final_acceptance_audit.py \
      --db outputs/hong_kong_ugc_fixed_20260714/pi_index.db \
      --out outputs/hong_kong_ugc_fixed_20260714/audit/final_acceptance.json \
      --samples 3 --fail-on-anomalies
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import sys
from types import SimpleNamespace
import unicodedata
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from pi_index.parsers.publications import (  # noqa: E402
    is_meaningful_publication_fingerprint,
)
from pi_index.storage import (  # noqa: E402
    _names_compatible_on_exact_profile,
    is_external_research_identity_url,
    normalize_profile_url,
)


ACTIVE_STATUS = "active"
STRONG_IDENTITY_FAMILIES = ("orcid", "openalex", "profile")
EXPECTED_UGC_SCHOOLS = {
    "City University of Hong Kong",
    "Hong Kong Baptist University",
    "Lingnan University",
    "The Chinese University of Hong Kong",
    "The Education University of Hong Kong",
    "The Hong Kong Polytechnic University",
    "The Hong Kong University of Science and Technology",
    "The University of Hong Kong",
}
LEGACY_SUPERVISION_KEYS = {
    "contactable_non_supervisor",
    "likely_supervisor_candidate",
    "pi_detection",
    "pi_supervisor_confidence",
    "retired_or_emeritus_risk",
    "supervision_eligibility",
    "supervision_signal",
    "supervision_signals",
    "supervision_score",
    "supervisor_eligible",
    "supervisor_signal",
    "supervisor_validity_score",
}


# Final acceptance deliberately recognizes only unmistakable page chrome,
# collective role headings, status pages and headline/sentence fragments.  It
# does not infer supervision eligibility from appointment titles: Lecturer,
# Dr, Research Assistant Professor and other legitimate titles are inspected
# only when they have themselves replaced the person's *display name*.
NON_PERSON_EXACT_DISPLAY_NAMES = frozenset(
    {
        "about us",
        "academic rankings",
        "academic staff",
        "adjunct professors",
        "adjunct/visiting professors",
        "awards and honors",
        "awards and honours",
        "course taught",
        "current research",
        "departmental awards",
        "distinguished professors",
        "distinguished visiting professors",
        "dive into details",
        "faculty",
        "faculty & staff",
        "faculty and staff",
        "faculty members",
        "faculty staff",
        "global research assistant professors",
        "honorary and adjunct professors",
        "honorary professor",
        "honorary professors",
        "link to profile",
        "our people",
        "page not found",
        "people",
        "posted in",
        "posted on",
        "research assistant professors",
        "research professors",
        "reset filters",
        "scholarship winners",
        "selected publications",
        "staff",
        "staff profile",
        "students and alumni success stories",
        "visiting professors / scholar",
        "visiting professors / scholars",
    }
)
NON_PERSON_DISPLAY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "homepage_chrome",
        re.compile(r"^(?:welcome\s+to\s+)?[^\n]+(?:['’]s)\s+home\s*page$", re.I),
    ),
    (
        "publication_section_heading",
        re.compile(r"^select(?:ed)?\s+publications?(?:\s*\([^)]*\))?$", re.I),
    ),
    (
        "awards_or_scholarship_heading",
        re.compile(
            r"\b(?:scholarships?\s*(?:&|and)\s*awards?|"
            r"awards?\s*(?:&|and)\s*honou?rs?)$",
            re.I,
        ),
    ),
    (
        "news_headline",
        re.compile(
            r"^(?:congratulations?\s+to|guests?\s+visit|promotion\s+of|"
            r"welcome\s+(?:prof(?:essor)?|dr)\.?\b)",
            re.I,
        ),
    ),
    (
        "news_headline",
        re.compile(
            r"(?:\bpaper\s+(?:was\s+)?accepted\s+(?:in|by)\b|"
            r"\bwon\b.{0,120}\b(?:poster|presentation|award|prize)\b)",
            re.I,
        ),
    ),
    (
        "news_headline",
        re.compile(r"^another\b.{0,120}\bgraduate\b", re.I),
    ),
    (
        "site_slogan",
        re.compile(r"^a\s+hub\s+of\b.{0,100}\b(?:era|minds?)\b", re.I),
    ),
    (
        "quotation_or_sentence",
        re.compile(r"\bappears\s+for\s+a\s+little\s+time\s+and\s+then\s+vanishes\b", re.I),
    ),
    (
        "biography_sentence_fragment",
        re.compile(r"\b[a-z][a-z'\-]{2,}\.\s+(?:he|she|they)$", re.I),
    ),
)
GENERIC_PROFILE_PATH_SLUGS = frozenset(
    {
        "academic-staff",
        "directory",
        "faculty",
        "faculty-and-staff",
        "our-people",
        "people",
        "persons",
        "profiles",
        "scholars",
        "staff",
    }
)
RESEARCH_PORTAL_ROOT_LABELS = frozenset(
    {"experts", "profiles", "pure", "researchers", "scholars"}
)
GENERIC_PROFILE_FRAGMENTS = frozenset(
    {"about", "bio", "biography", "contact", "overview", "profile"}
)


# These selectors are deliberately exact-name based unless an official profile
# fragment is the stable identifier.  That keeps e.g. Catherine Chan distinct
# from Catherine K. K. Chan while still allowing punctuation/case variation.
NAMED_CASES: tuple[dict[str, Any], ...] = (
    {
        "key": "aaron_seokhyun_yoon",
        "exact_names": ("Aaron Seokhyun Yoon",),
        "required_emails": ("yoon@hku.hk",),
        "expected_count": 1,
    },
    {
        "key": "andy_chow",
        "exact_names": ("Andy Chow", "Andy H. F. Chow"),
        "required_emails": ("andychow@cityu.edu.hk",),
        "require_research_evidence": True,
        "expected_count": 1,
    },
    {
        "key": "gregg_rockett",
        "exact_names": ("Gregg Rockett",),
        "expect_no_email": True,
        "require_research_evidence": True,
        "expected_count": 1,
    },
    {
        "key": "yuanwei_yao",
        "exact_names": ("Yuanwei Yao", "Yuan-wei Yao", "Yuanwei YAO", "Yuan-wei YAO"),
        "required_emails": ("ywyao@hku.hk",),
        "forbidden_emails": ("singhang@hku.hk",),
        "expected_count": 1,
    },
    {
        "key": "sing_hang_cheung",
        "exact_names": ("Sing-hang Cheung", "Sing Hang Cheung"),
        "required_emails": ("singhang@hku.hk",),
        "forbidden_emails": ("ywyao@hku.hk",),
        "expected_count": 1,
    },
    {
        "key": "anqi_sun",
        "exact_names": ("Anqi Sun",),
        "required_title_fragments": ("Research Assistant Professor",),
        "expected_count": 1,
    },
    {
        "key": "ada_tian",
        "exact_names": ("Ada T.T. Tian", "Ada Tian", "Tingting Tian"),
        "required_title_fragments": ("Research Assistant Professor",),
        "expected_count": 1,
    },
    {
        "key": "aelrun_goette",
        "exact_names": ("Aelrun Goette",),
        "expect_no_email": True,
        "required_title_fragments": ("Honorary Lecturer",),
        "expected_count": 1,
    },
    {
        "key": "richard_allen",
        "exact_names": ("Richard Allen",),
        "max_title_length": 160,
        "expected_count": 1,
    },
    {
        "key": "catherine_chan",
        "exact_names": ("Catherine Chan",),
        "max_title_length": 160,
        "expected_count": 1,
    },
    {
        "key": "alex_shi_architecture",
        "profile_fragments": ("/cris/rp/rp02773", "/staff/rec/shi-alex-shuai"),
        "required_name_fragments": ("Shi", "Alex"),
        "required_emails": ("alexshi@hku.hk",),
        "required_profile_fragments": ("/staff/rec/shi-alex-shuai",),
        "expected_count": 1,
    },
    {
        "key": "hesheng_chen",
        "exact_names": ("CHEN Hesheng", "Hesheng Chen"),
        "required_emails": ("heshchen@cityu.edu.hk",),
        "required_profile_fragments": ("/persons/heshchen",),
        "expected_count": 1,
    },
    {
        "key": "ah_kok_wong",
        "exact_names": ("Ah-Kok Wong", "Ah Kok Wong"),
        "expect_no_email": True,
        "require_research_evidence": True,
        "expected_count": 1,
    },
    {
        "key": "andrew_hoang",
        "exact_names": ("Andrew Hoang",),
        "required_emails": ("andrewph@hku.hk",),
        "required_title_fragments": ("Senior Lecturer",),
        "expected_count": 1,
    },
    {
        "key": "eray_arda_akartuna",
        "exact_names": ("Eray Arda Akartuna",),
        "require_research_evidence": True,
        "expected_count": 1,
    },
    {
        "key": "adrian_kyle_yee",
        "exact_names": ("Adrian Kyle Yee",),
        "require_research_evidence": True,
        "expected_count": 1,
    },
    {
        "key": "abhiroop_mukherjee",
        "exact_names": ("Abhiroop Mukherjee",),
        "require_research_evidence": True,
        "expected_count": 1,
    },
    {
        "key": "alain_chiaradia",
        "exact_names": ("Alain Chiaradia",),
        "expected_count": 1,
    },
    {
        "key": "alessandra_cianchetta",
        "exact_names": ("Alessandra Cianchetta",),
        "expected_count": 1,
    },
    {
        "key": "anthony_gar_on_yeh",
        "exact_names": ("Anthony Gar On Yeh", "Anthony G. O. Yeh"),
        "expected_count": 1,
    },
    {
        "key": "benjamin_moorhouse",
        "exact_names": ("Benjamin Moorhouse",),
        "expected_count": 1,
    },
    {
        "key": "aoyama_reijiro",
        "exact_names": ("AOYAMA Reijiro", "Reijiro Aoyama"),
        "expected_count": 1,
    },
    {
        "key": "kevin_kin_man_tsia",
        "exact_names": ("Kevin Kin Man TSIA",),
        "forbidden_title_fragments": (",;",),
        "required_title_fragments": ("Professor", "Program Director"),
        "expected_count": 1,
    },
    {
        "key": "matthew_hsu_shi_shin",
        "exact_names": ("Matthew HSU Shi Shin",),
        "exact_title": "Clinical Practitioner",
        "expected_count": 1,
    },
    {
        "key": "raymond_o_yu",
        "exact_names": ("Raymond O Yu",),
        "exact_title": "Clinical Practitioner",
        "expected_count": 1,
    },
    {
        "key": "samuel_ching_on_hang",
        "exact_names": ("Samuel CHING On Hang",),
        "exact_title": "Clinical Practitioner",
        "expected_count": 1,
    },
    {
        "key": "chen_lin_law",
        "exact_names": ("Chen Lin",),
        "required_title_fragments": ("Professor", "Chair of Finance"),
        "expected_count": 1,
    },
    {
        "key": "brian_tang_law",
        "profile_fragments": ("/academic_staff/brian-tang",),
        "required_name_fragments": ("Brian", "Tang"),
        "required_emails": ("bwtang@hku.hk",),
        "exact_title": "Principal Professional Practitioner",
        "expected_count": 1,
    },
    {
        "key": "jiahui_duan_law",
        "profile_fragments": ("/academic_staff/dr-jiahui-duan",),
        "required_name_fragments": ("Jiahui", "Duan"),
        "required_emails": ("jhduan@hku.hk",),
        "exact_title": "Post-Doctoral Fellow",
        "expected_count": 1,
    },
    {
        "key": "marcelo_thompson_law",
        "profile_fragments": ("/academic_staff/dr-marcelo-thompson",),
        "required_name_fragments": ("Marcelo", "Thompson"),
        "required_emails": ("marcelo.thompson@hku.hk",),
        "exact_title": "Adjunct Associate Professor",
        "expected_count": 1,
    },
    {
        "key": "marianne_von_blomberg_law",
        "profile_fragments": ("/academic_staff/dr-marianne-von-blomberg",),
        "required_name_fragments": ("Marianne", "Blomberg"),
        "required_emails": ("mvonblom@hku.hk",),
        "exact_title": "Global Academic Fellow",
        "expected_count": 1,
    },
    {
        "key": "xiang_zhang_science",
        "profile_fragments": ("/people/zhang-xiang",),
        "required_name_fragments": ("Xiang", "Zhang"),
        "required_title_fragments": ("President", "Chair of Physics"),
        "expected_count": 1,
    },
    {
        "key": "terry_lo_science",
        "profile_fragments": ("/people/lo-terry-kin-chung",),
        "required_name_fragments": ("Terry", "Lo"),
        "exact_title": "Professional Practitioner, Faculty of Science, HKU",
        "expected_count": 1,
    },
)


NAMED_CASE_INSTITUTIONS = {
    "aaron_seokhyun_yoon": "The University of Hong Kong",
    "andy_chow": "City University of Hong Kong",
    "gregg_rockett": "The Hong Kong Polytechnic University",
    "yuanwei_yao": "The University of Hong Kong",
    "sing_hang_cheung": "The University of Hong Kong",
    "anqi_sun": "City University of Hong Kong",
    "ada_tian": "The Hong Kong Polytechnic University",
    "aelrun_goette": "The University of Hong Kong",
    "richard_allen": "The University of Hong Kong",
    "catherine_chan": "The University of Hong Kong",
    "alex_shi_architecture": "The University of Hong Kong",
    "hesheng_chen": "City University of Hong Kong",
    "ah_kok_wong": "The University of Hong Kong",
    "andrew_hoang": "The University of Hong Kong",
    "eray_arda_akartuna": "City University of Hong Kong",
    "adrian_kyle_yee": "Hong Kong Baptist University",
    "abhiroop_mukherjee": "The Hong Kong University of Science and Technology",
    "alain_chiaradia": "The University of Hong Kong",
    "alessandra_cianchetta": "The University of Hong Kong",
    "anthony_gar_on_yeh": "The University of Hong Kong",
    "benjamin_moorhouse": "City University of Hong Kong",
    "aoyama_reijiro": "The Chinese University of Hong Kong",
    "kevin_kin_man_tsia": "The University of Hong Kong",
    "matthew_hsu_shi_shin": "The University of Hong Kong",
    "raymond_o_yu": "The University of Hong Kong",
    "samuel_ching_on_hang": "The University of Hong Kong",
    "chen_lin_law": "The University of Hong Kong",
    "brian_tang_law": "The University of Hong Kong",
    "jiahui_duan_law": "The University of Hong Kong",
    "marcelo_thompson_law": "The University of Hong Kong",
    "marianne_von_blomberg_law": "The University of Hong Kong",
    "xiang_zhang_science": "The University of Hong Kong",
    "terry_lo_science": "The University of Hong Kong",
}


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _clean_list(value: Any) -> list[str]:
    parsed = _json_value(value, value)
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, (list, tuple, set)):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in parsed:
        cleaned = _clean_text(item)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def _fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _clean_text(value)).casefold()
    return re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).strip()


def _normalize_email(value: Any) -> str:
    return _clean_text(value).casefold().removeprefix("mailto:").strip(" <>.,;:")


def _normalize_profile(value: Any) -> str:
    # Use the same comparison contract as ingestion.  In particular, identity-
    # bearing query parameters (``profile.php?id=123``) and person fragments
    # must survive, while transport/www/tracking noise must not.
    return normalize_profile_url(_clean_text(value))


def _non_person_display_reason(value: Any) -> str | None:
    """Return a conservative reason when a display name is unmistakably chrome.

    Appointment titles are intentionally not considered.  This function sees
    only the canonical display-name string, so a real person whose separate
    title is ``Research Assistant Professor`` remains valid.
    """

    cleaned = _clean_text(value)
    if not cleaned:
        return "missing_or_blank_display_name"
    normalized = cleaned.casefold().strip(" -:|")
    if normalized in NON_PERSON_EXACT_DISPLAY_NAMES:
        return "known_page_or_collective_heading"
    for reason, pattern in NON_PERSON_DISPLAY_PATTERNS:
        if pattern.search(cleaned):
            return reason
    return None


def _profile_url_anomaly_reason(value: Any) -> str | None:
    """Return why a canonical/profile-list URL cannot serve as a person page."""

    cleaned = _clean_text(value)
    if not cleaned:
        return None
    try:
        parsed = urlsplit(cleaned)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return "malformed_url"
    if parsed.scheme.casefold() not in {"http", "https"} or not host:
        return "non_http_or_hostless_url"
    if is_external_research_identity_url(cleaned):
        return "external_research_identity_url"

    path = unquote(parsed.path or "/").casefold().rstrip("/") or "/"
    if path == "/error/404" or path.endswith("/error/404"):
        return "error_404_url"

    # Query parameters commonly carry the person ID.  A non-generic fragment
    # can do the same (for example ``/academic-staff#AlexGearin``).  Neither is
    # an aggregate URL merely because its base path is a directory page.
    fragment = re.sub(r"[^a-z0-9]+", "", unquote(parsed.fragment or "").casefold())
    has_person_fragment = bool(fragment and fragment not in GENERIC_PROFILE_FRAGMENTS)
    if not parsed.query and not has_person_fragment:
        final_slug = path.rsplit("/", 1)[-1].removesuffix(".html").removesuffix(".htm")
        if final_slug in GENERIC_PROFILE_PATH_SLUGS:
            return "aggregate_directory_url"
        first_host_label = host.split(".", 1)[0]
        if path == "/" and first_host_label in RESEARCH_PORTAL_ROOT_LABELS:
            return "generic_research_portal_root"
    return None


def _active_serving_anomalies(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Audit critical person-name and profile-URL serving anomalies."""

    non_person_display_names: list[dict[str, Any]] = []
    unusable_profile_urls: list[dict[str, Any]] = []
    for record in records:
        reason = _non_person_display_reason(record.get("display_name"))
        if reason:
            non_person_display_names.append(
                {
                    "person_id": record["person_id"],
                    "name": record["display_name"],
                    "school": record["institution_name"],
                    "reason": reason,
                }
            )

        locations_by_url: dict[str, set[str]] = defaultdict(set)
        primary = _clean_text(record.get("profile_url"))
        if primary:
            locations_by_url[primary].add("canonical_pi_records.profile_url")
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        for profile in _clean_list(payload.get("profile_urls")):
            locations_by_url[profile].add("record_json.profile_urls")
        # Preserve coverage if a caller constructed an audit record directly
        # rather than through ``_active_records``.
        for profile in _clean_list(record.get("profile_urls")):
            locations_by_url[profile].add("profile_urls")

        for profile, locations in sorted(locations_by_url.items()):
            url_reason = _profile_url_anomaly_reason(profile)
            if not url_reason:
                continue
            unusable_profile_urls.append(
                {
                    "person_id": record["person_id"],
                    "name": record["display_name"],
                    "school": record["institution_name"],
                    "url": profile,
                    "locations": sorted(locations),
                    "reason": url_reason,
                }
            )

    return {
        "non_person_display_names": sorted(
            non_person_display_names,
            key=lambda item: (item["school"].casefold(), _fold(item["name"]), item["person_id"]),
        ),
        "unusable_profile_urls": sorted(
            unusable_profile_urls,
            key=lambda item: (
                item["school"].casefold(),
                _fold(item["name"]),
                item["person_id"],
                item["url"].casefold(),
            ),
        ),
    }


def _normalize_orcid(value: Any) -> str:
    raw = _clean_text(value)
    match = re.search(r"\b\d{4}-\d{4}-\d{4}-[\dXx]{4}\b", raw)
    return match.group(0).upper() if match else ""


def _normalize_openalex(value: Any) -> str:
    raw = _clean_text(value)
    match = re.search(r"(?:^|[/\s])A\d{5,}(?:$|[/?#\s])", raw, flags=re.I)
    if match:
        nested = re.search(r"A\d{5,}", match.group(0), flags=re.I)
        return nested.group(0).upper() if nested else ""
    if re.fullmatch(r"A\d{5,}", raw, flags=re.I):
        return raw.upper()
    return ""


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {
        _clean_text(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})")
        if _clean_text(row["name"])
    }


def _open_read_only(database: Path) -> sqlite3.Connection:
    resolved = database.resolve(strict=True)
    connection = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _resolve_person(person_id: str, aliases: dict[str, str]) -> str:
    resolved = person_id
    seen: set[str] = set()
    while resolved in aliases and resolved not in seen:
        seen.add(resolved)
        resolved = aliases[resolved]
    return resolved


def _aliases(connection: sqlite3.Connection) -> dict[str, str]:
    if not _table_exists(connection, "pi_identity_aliases"):
        return {}
    return {
        _clean_text(row["alias_person_id"]): _clean_text(row["canonical_person_id"])
        for row in connection.execute(
            "SELECT alias_person_id, canonical_person_id FROM pi_identity_aliases"
        )
        if _clean_text(row["alias_person_id"]) and _clean_text(row["canonical_person_id"])
    }


def _alias_integrity(
    connection: sqlite3.Connection,
    aliases: dict[str, str],
) -> dict[str, Any]:
    if not _table_exists(connection, "pi_identity_aliases"):
        return {
            "available": False,
            "violations": [{"kind": "missing_table", "table": "pi_identity_aliases"}],
        }

    canonical_rows = {
        _clean_text(row["person_id"]): {
            "institution_id": _clean_text(row["institution_id"]),
            "membership_status": _clean_text(row["membership_status"]),
        }
        for row in connection.execute(
            "SELECT person_id, institution_id, membership_status FROM canonical_pi_records"
        )
    }
    alias_columns = _table_columns(connection, "pi_identity_aliases")
    has_institution = "institution_id" in alias_columns
    select_columns = "alias_person_id, canonical_person_id"
    if has_institution:
        select_columns += ", institution_id"

    violations: list[dict[str, Any]] = []
    for row in connection.execute(f"SELECT {select_columns} FROM pi_identity_aliases"):
        alias_id = _clean_text(row["alias_person_id"])
        canonical_id = _clean_text(row["canonical_person_id"])
        alias_institution = _clean_text(row["institution_id"]) if has_institution else ""
        if alias_id == canonical_id:
            violations.append(
                {"kind": "self_alias", "alias_person_id": alias_id, "canonical_person_id": canonical_id}
            )
        if alias_id in canonical_rows:
            violations.append(
                {
                    "kind": "alias_still_canonical",
                    "alias_person_id": alias_id,
                    "canonical_person_id": canonical_id,
                    "membership_status": canonical_rows[alias_id]["membership_status"],
                }
            )
        resolved = _resolve_person(canonical_id, aliases)
        target = canonical_rows.get(resolved)
        if target is None:
            violations.append(
                {
                    "kind": "dangling_target",
                    "alias_person_id": alias_id,
                    "canonical_person_id": canonical_id,
                    "resolved_person_id": resolved,
                }
            )
        elif alias_institution and target["institution_id"] != alias_institution:
            violations.append(
                {
                    "kind": "institution_mismatch",
                    "alias_person_id": alias_id,
                    "canonical_person_id": canonical_id,
                    "alias_institution_id": alias_institution,
                    "target_institution_id": target["institution_id"],
                }
            )

        seen: set[str] = set()
        current = alias_id
        while current in aliases and current not in seen:
            seen.add(current)
            current = aliases[current]
        if current in seen:
            cycle = sorted(seen)
            marker = {"kind": "alias_cycle", "person_ids": cycle}
            if marker not in violations:
                violations.append(marker)

    return {"available": True, "violations": violations}


def _active_records(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(connection, "canonical_pi_records"):
        raise RuntimeError("required table canonical_pi_records is missing")
    records: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT * FROM canonical_pi_records "
        "WHERE lower(coalesce(membership_status, 'active'))='active'"
    ):
        payload = _json_value(row["record_json"], {})
        if not isinstance(payload, dict):
            payload = {}
        emails = [_normalize_email(value) for value in _clean_list(row["emails_json"])]
        emails = list(dict.fromkeys(value for value in emails if value and "@" in value))
        areas = _clean_list(row["research_areas_json"])
        profile_urls = _clean_list(payload.get("profile_urls"))
        primary_profile = _clean_text(row["profile_url"])
        if primary_profile:
            profile_urls.insert(0, primary_profile)
        profile_urls = list(dict.fromkeys(value for value in profile_urls if value))
        external_ids = payload.get("external_ids")
        if not isinstance(external_ids, dict):
            external_ids = {}
        records.append(
            {
                "person_id": _clean_text(row["person_id"]),
                "institution_id": _clean_text(row["institution_id"]),
                "institution_name": _clean_text(row["institution_name"]),
                "display_name": _clean_text(row["display_name"]),
                "aliases": _clean_list(payload.get("aliases")),
                "title": _clean_text(row["title"]) or None,
                "department": _clean_text(row["department"]) or None,
                "emails": emails,
                "email_association": _clean_text(payload.get("email_association")) or "none",
                "profile_url": primary_profile or None,
                "profile_urls": profile_urls,
                "research_areas": areas,
                "external_ids": external_ids,
                "contact_confidence": _clean_text(row["contact_confidence"]) or None,
                "topic_match_confidence": _clean_text(row["topic_match_confidence"]) or None,
                "current_affiliation_confidence": _clean_text(
                    row["current_affiliation_confidence"]
                )
                or None,
                "payload": payload,
            }
        )
    return records


def _publication_counts(
    connection: sqlite3.Connection,
    active_by_id: dict[str, dict[str, Any]],
    aliases: dict[str, str],
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    if not _table_exists(connection, "official_publication_fingerprints"):
        return {}
    for row in connection.execute("SELECT * FROM official_publication_fingerprints"):
        fingerprint = dict(row)
        if not is_meaningful_publication_fingerprint(fingerprint):
            continue
        person_id = _resolve_person(_clean_text(row["person_id"]), aliases)
        if person_id in active_by_id:
            counts[person_id] += 1
    return dict(counts)


def _official_person_local_email_evidence(
    connection: sqlite3.Connection,
    aliases: dict[str, str],
) -> dict[str, Any]:
    required_columns = {
        "person_id",
        "email",
        "official_source",
        "association",
    }
    columns = _table_columns(connection, "email_evidence")
    if not required_columns.issubset(columns):
        return {
            "available": False,
            "pairs": set(),
            "missing_columns": sorted(required_columns - columns),
        }
    pairs: set[tuple[str, str]] = set()
    for row in connection.execute(
        """
        SELECT person_id, email
        FROM email_evidence
        WHERE official_source=1
          AND lower(coalesce(association, ''))='person_local'
        """
    ):
        person_id = _resolve_person(_clean_text(row["person_id"]), aliases)
        email = _normalize_email(row["email"])
        if person_id and email and "@" in email:
            pairs.add((person_id, email))
    return {"available": True, "pairs": pairs, "missing_columns": []}


def _record_summary(record: dict[str, Any], publication_count: int) -> dict[str, Any]:
    return {
        "person_id": record["person_id"],
        "name": record["display_name"],
        "institution_id": record["institution_id"],
        "school": record["institution_name"],
        "title": record["title"],
        "department": record["department"],
        "emails": record["emails"],
        "profile_url": record["profile_url"],
        "profile_urls": record["profile_urls"],
        "official_research_areas": record["research_areas"],
        "meaningful_official_publication_count": publication_count,
        "external_ids": record["external_ids"],
        "contact_confidence": record["contact_confidence"],
        "topic_match_confidence": record["topic_match_confidence"],
        "current_affiliation_confidence": record["current_affiliation_confidence"],
    }


def _sample(
    records: Iterable[dict[str, Any]],
    publication_counts: dict[str, int],
    limit: int,
) -> list[dict[str, Any]]:
    ordered = sorted(
        records,
        key=lambda item: (
            item["institution_name"].casefold(),
            _fold(item["display_name"]),
            item["person_id"],
        ),
    )
    return [
        _record_summary(record, publication_counts.get(record["person_id"], 0))
        for record in ordered[:limit]
    ]


def _per_school(
    records: list[dict[str, Any]],
    publication_counts: dict[str, int],
    sample_limit: int,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(record["institution_id"], record["institution_name"])].append(record)

    result: list[dict[str, Any]] = []
    for (institution_id, school), members in sorted(
        grouped.items(), key=lambda item: item[0][1].casefold()
    ):
        no_email = [record for record in members if not record["emails"]]
        no_title = [record for record in members if not record["title"]]
        no_profile = [record for record in members if not record["profile_urls"]]
        no_areas = [record for record in members if not record["research_areas"]]
        with_publications = [
            record for record in members if publication_counts.get(record["person_id"], 0) > 0
        ]
        no_areas_with_publications = [
            record
            for record in no_areas
            if publication_counts.get(record["person_id"], 0) > 0
        ]
        no_research_evidence = [
            record
            for record in no_areas
            if publication_counts.get(record["person_id"], 0) == 0
        ]
        result.append(
            {
                "institution_id": institution_id,
                "school": school,
                "metrics": {
                    "active": len(members),
                    "no_email": len(no_email),
                    "no_title": len(no_title),
                    "no_profile": len(no_profile),
                    "with_official_research_areas": len(members) - len(no_areas),
                    "no_official_research_areas": len(no_areas),
                    "with_meaningful_official_publications": len(with_publications),
                    "no_official_areas_but_with_meaningful_official_publications": len(
                        no_areas_with_publications
                    ),
                    "no_official_areas_and_no_meaningful_official_publications": len(
                        no_research_evidence
                    ),
                },
                "samples": {
                    "no_email": _sample(no_email, publication_counts, sample_limit),
                    "no_title": _sample(no_title, publication_counts, sample_limit),
                    "no_profile": _sample(no_profile, publication_counts, sample_limit),
                    "no_official_research_areas": _sample(
                        no_areas, publication_counts, sample_limit
                    ),
                    "no_official_areas_but_with_meaningful_official_publications": _sample(
                        no_areas_with_publications, publication_counts, sample_limit
                    ),
                    "no_official_areas_and_no_meaningful_official_publications": _sample(
                        no_research_evidence, publication_counts, sample_limit
                    ),
                },
            }
        )
    return result


def _shared_email_groups(
    records: list[dict[str, Any]],
    aliases: dict[str, str],
    publication_counts: dict[str, int],
) -> list[dict[str, Any]]:
    active_by_id = {record["person_id"]: record for record in records}
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for record in records:
        canonical_id = _resolve_person(record["person_id"], aliases)
        if canonical_id not in active_by_id:
            canonical_id = record["person_id"]
        for email in record["emails"]:
            grouped[(record["institution_id"], email)].add(canonical_id)
    result = []
    for (institution_id, email), person_ids in sorted(grouped.items()):
        if len(person_ids) < 2:
            continue
        members = [active_by_id[person_id] for person_id in sorted(person_ids)]
        result.append(
            {
                "institution_id": institution_id,
                "email": email,
                "members": [
                    _record_summary(member, publication_counts.get(member["person_id"], 0))
                    for member in members
                ],
            }
        )
    return result


def _external_identity_values(record: dict[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {"orcid": set(), "openalex": set()}
    for key, raw_value in record["external_ids"].items():
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        for value in values:
            key_folded = _fold(key)
            if "orcid" in key_folded:
                normalized = _normalize_orcid(value)
                if normalized:
                    result["orcid"].add(normalized)
            if "openalex" in key_folded:
                normalized = _normalize_openalex(value)
                if normalized:
                    result["openalex"].add(normalized)
    return result


def _strong_identity_duplicates(
    connection: sqlite3.Connection,
    records: list[dict[str, Any]],
    aliases: dict[str, str],
    publication_counts: dict[str, int],
) -> dict[str, Any]:
    active_by_id = {record["person_id"]: record for record in records}
    grouped: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    # Canonical JSON fallback catches stale/missing identity-index rows.
    for record in records:
        canonical_id = _resolve_person(record["person_id"], aliases)
        if canonical_id not in active_by_id:
            canonical_id = record["person_id"]
        for family, values in _external_identity_values(record).items():
            for value in values:
                grouped[(record["institution_id"], family, value)].add(canonical_id)

    identity_index_available = _table_exists(connection, "pi_identity_keys")
    indexed_active_person_ids: set[str] = set()
    if identity_index_available:
        for row in connection.execute(
            "SELECT institution_id, person_id, identity_kind, identity_value "
            "FROM pi_identity_keys"
        ):
            raw_person_id = _clean_text(row["person_id"])
            if (
                _clean_text(row["identity_kind"]) == "__indexed__"
                and _clean_text(row["identity_value"]) == "1"
                and raw_person_id in active_by_id
            ):
                indexed_active_person_ids.add(raw_person_id)
            person_id = _resolve_person(raw_person_id, aliases)
            if person_id not in active_by_id:
                continue
            kind = _clean_text(row["identity_kind"]).casefold()
            value = _clean_text(row["identity_value"])
            family = ""
            normalized = ""
            if "orcid" in kind:
                family, normalized = "orcid", _normalize_orcid(value)
            elif "openalex" in kind:
                family, normalized = "openalex", _normalize_openalex(value)
            elif kind in {"profile", "profile_exact"}:
                family, normalized = "profile", _normalize_profile(value)
            if family and normalized:
                grouped[(_clean_text(row["institution_id"]), family, normalized)].add(person_id)

    duplicates: dict[str, list[dict[str, Any]]] = {
        family: [] for family in STRONG_IDENTITY_FAMILIES
    }
    for (institution_id, family, value), person_ids in sorted(grouped.items()):
        if len(person_ids) < 2:
            continue
        members = [active_by_id[person_id] for person_id in sorted(person_ids)]
        components = [members]
        if family == "profile":
            # ``profile_exact`` is a candidate-generating index, not an
            # unconditional identity.  Mirror the production resolver: the
            # exact official URL must also have compatible names or the same
            # person-local, non-role email.  This prevents list/lab pages from
            # being reported as strong duplicates merely because several
            # people legitimately share one aggregate URL.
            parent = {member["person_id"]: member["person_id"] for member in members}

            def find(person_id: str) -> str:
                while parent[person_id] != person_id:
                    parent[person_id] = parent[parent[person_id]]
                    person_id = parent[person_id]
                return person_id

            def union(left: str, right: str) -> None:
                left_root, right_root = find(left), find(right)
                if left_root != right_root:
                    parent[right_root] = left_root

            for index, left in enumerate(members):
                left_name = SimpleNamespace(
                    display_name=left["display_name"], aliases=left.get("aliases") or []
                )
                left_emails = (
                    set(left["emails"])
                    if left.get("email_association") == "person_local"
                    else set()
                )
                for right in members[index + 1 :]:
                    right_name = SimpleNamespace(
                        display_name=right["display_name"], aliases=right.get("aliases") or []
                    )
                    right_emails = (
                        set(right["emails"])
                        if right.get("email_association") == "person_local"
                        else set()
                    )
                    if _names_compatible_on_exact_profile(left_name, right_name) or left_emails.intersection(right_emails):
                        union(left["person_id"], right["person_id"])

            grouped_members: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for member in members:
                grouped_members[find(member["person_id"])].append(member)
            components = [
                component for component in grouped_members.values() if len(component) > 1
            ]

        for component in components:
            duplicates[family].append(
                {
                    "institution_id": institution_id,
                    "identity_value": value,
                    "members": [
                        _record_summary(member, publication_counts.get(member["person_id"], 0))
                        for member in component
                    ],
                }
            )
    return {
        "identity_index_available": identity_index_available,
        "identity_index_complete": bool(
            identity_index_available
            and indexed_active_person_ids == set(active_by_id)
        ),
        "missing_active_person_ids": sorted(set(active_by_id) - indexed_active_person_ids),
        "counts": {family: len(duplicates[family]) for family in STRONG_IDENTITY_FAMILIES},
        "groups": duplicates,
    }


def _exact_profile_groups(
    records: list[dict[str, Any]],
    aliases: dict[str, str],
    publication_counts: dict[str, int],
) -> list[dict[str, Any]]:
    active_by_id = {record["person_id"]: record for record in records}
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for record in records:
        person_id = _resolve_person(record["person_id"], aliases)
        if person_id not in active_by_id:
            person_id = record["person_id"]
        for profile in record["profile_urls"]:
            normalized = _normalize_profile(profile)
            if normalized:
                grouped[(record["institution_id"], normalized)].add(person_id)
    result = []
    for (institution_id, profile), person_ids in sorted(grouped.items()):
        if len(person_ids) < 2:
            continue
        members = [active_by_id[person_id] for person_id in sorted(person_ids)]
        result.append(
            {
                "institution_id": institution_id,
                "normalized_profile_url": profile,
                "members": [
                    _record_summary(member, publication_counts.get(member["person_id"], 0))
                    for member in members
                ],
            }
        )
    return result


def _same_name_groups(
    records: list[dict[str, Any]],
    aliases: dict[str, str],
    publication_counts: dict[str, int],
) -> list[dict[str, Any]]:
    active_by_id = {record["person_id"]: record for record in records}
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for record in records:
        person_id = _resolve_person(record["person_id"], aliases)
        if person_id not in active_by_id:
            person_id = record["person_id"]
        grouped[(record["institution_id"], _fold(record["display_name"]))].add(person_id)
    result = []
    for (institution_id, name), person_ids in sorted(grouped.items()):
        if not name or len(person_ids) < 2:
            continue
        members = [active_by_id[person_id] for person_id in sorted(person_ids)]
        result.append(
            {
                "institution_id": institution_id,
                "normalized_name": name,
                "members": [
                    _record_summary(member, publication_counts.get(member["person_id"], 0))
                    for member in members
                ],
            }
        )
    return result


def _case_matches(case: dict[str, Any], record: dict[str, Any]) -> bool:
    expected_institution = NAMED_CASE_INSTITUTIONS.get(case["key"])
    if expected_institution and _fold(record["institution_name"]) != _fold(expected_institution):
        return False
    folded_names = {
        _fold(name)
        for name in (record["display_name"], *record.get("aliases", ()))
        if _fold(name)
    }
    exact_names = {_fold(name) for name in case.get("exact_names", ())}
    name_match = bool(exact_names & folded_names)
    profile_match = any(
        fragment.casefold() in profile.casefold()
        for fragment in case.get("profile_fragments", ())
        for profile in record["profile_urls"]
    )
    return name_match or profile_match


def _validate_case(
    case: dict[str, Any],
    matches: list[dict[str, Any]],
    publication_counts: dict[str, int],
    email_evidence: dict[str, Any] | None = None,
) -> list[str]:
    failures: list[str] = []
    expected_count = case.get("expected_count")
    if expected_count is not None and len(matches) != expected_count:
        failures.append(f"expected {expected_count} active record(s), found {len(matches)}")
    if not matches:
        return failures

    emails = {email for record in matches for email in record["emails"]}
    required_emails = {
        _normalize_email(required) for required in case.get("required_emails", ())
    }
    for required in sorted(required_emails):
        if required not in emails:
            failures.append(f"required email missing: {required}")
    if required_emails and emails != required_emails:
        unexpected = sorted(emails - required_emails)
        if unexpected:
            failures.append(f"unexpected email(s) present: {unexpected}")
    if required_emails and email_evidence is not None:
        if not email_evidence["available"]:
            failures.append(
                "official person-local email evidence unavailable"
            )
        else:
            pairs = email_evidence["pairs"]
            for required in sorted(required_emails):
                if not any((record["person_id"], required) in pairs for record in matches):
                    failures.append(
                        f"official person-local evidence missing for email: {required}"
                    )
    for forbidden in case.get("forbidden_emails", ()):
        if _normalize_email(forbidden) in emails:
            failures.append(f"forbidden email present: {_normalize_email(forbidden)}")
    if case.get("expect_no_email") and emails:
        failures.append(f"expected no official email, found {sorted(emails)}")

    joined_names = " | ".join(record["display_name"] for record in matches).casefold()
    for fragment in case.get("required_name_fragments", ()):
        if fragment.casefold() not in joined_names:
            failures.append(f"required name fragment missing: {fragment}")

    titles = [record["title"] or "" for record in matches]
    joined_titles = " | ".join(titles)
    exact_title = case.get("exact_title")
    if exact_title and not any(_fold(title) == _fold(exact_title) for title in titles):
        failures.append(f"exact title missing: {exact_title}")
    for fragment in case.get("required_title_fragments", ()):
        if fragment.casefold() not in joined_titles.casefold():
            failures.append(f"required title fragment missing: {fragment}")
    for fragment in case.get("forbidden_title_fragments", ()):
        if fragment.casefold() in joined_titles.casefold():
            failures.append(f"forbidden title fragment present: {fragment}")
    max_title_length = case.get("max_title_length")
    if max_title_length is not None and any(len(title) > max_title_length for title in titles):
        failures.append(f"title exceeds {max_title_length} characters")

    profiles = [profile for record in matches for profile in record["profile_urls"]]
    for fragment in case.get("required_profile_fragments", ()):
        if not any(fragment.casefold() in profile.casefold() for profile in profiles):
            failures.append(f"required profile fragment missing: {fragment}")

    if case.get("require_research_evidence") and not any(
        record["research_areas"] or publication_counts.get(record["person_id"], 0) > 0
        for record in matches
    ):
        failures.append("neither official research areas nor meaningful official publications found")
    return failures


def _named_case_report(
    records: list[dict[str, Any]],
    publication_counts: dict[str, int],
    email_evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    report = []
    for case in NAMED_CASES:
        matches = [record for record in records if _case_matches(case, record)]
        failures = _validate_case(case, matches, publication_counts, email_evidence)
        report.append(
            {
                "key": case["key"],
                "pass": not failures,
                "failures": failures,
                "matches": [
                    _record_summary(record, publication_counts.get(record["person_id"], 0))
                    for record in sorted(matches, key=lambda item: item["person_id"])
                ],
            }
        )
    return report


def _legacy_key_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = _clean_text(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            if key.casefold() in LEGACY_SUPERVISION_KEYS:
                paths.append(path)
            paths.extend(_legacy_key_paths(nested, path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            paths.extend(_legacy_key_paths(nested, f"{prefix}[{index}]"))
    return paths


def _serving_json_audit(connection: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    legacy_violations: list[dict[str, Any]] = []
    invalid_json: list[dict[str, Any]] = []
    targets = (
        ("canonical_pi_records", "person_id", "display_name"),
        ("contact_verdicts", "person_id", None),
    )
    for table, id_column, name_column in targets:
        columns = _table_columns(connection, table)
        if not columns or "record_json" not in columns:
            continue
        selected = f"{id_column}, record_json"
        if name_column and name_column in columns:
            selected += f", {name_column}"
        for row in connection.execute(f"SELECT {selected} FROM {table}"):
            record_id = _clean_text(row[id_column])
            name = _clean_text(row[name_column]) if name_column and name_column in columns else ""
            try:
                payload = json.loads(str(row["record_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                invalid_json.append(
                    {
                        "table": table,
                        "record_id": record_id,
                        "error": type(exc).__name__,
                    }
                )
                continue
            paths = sorted(set(_legacy_key_paths(payload)))
            if paths:
                violation = {
                    "table": table,
                    "record_id": record_id,
                    "keys": paths,
                }
                if name:
                    violation["name"] = name
                legacy_violations.append(violation)

    # A reused legacy database can retain retired physical columns even after
    # its JSON payloads are cleaned.  They are not part of the neutral serving
    # contract and must be visible to final acceptance rather than silently
    # ignored.
    for table in ("canonical_pi_records", "contact_verdicts", "match_results"):
        columns = _table_columns(connection, table)
        legacy_columns = sorted(
            column for column in columns if column.casefold() in LEGACY_SUPERVISION_KEYS
        )
        if legacy_columns:
            legacy_violations.append(
                {
                    "table": table,
                    "record_id": None,
                    "keys": legacy_columns,
                    "kind": "physical_columns",
                }
            )
    return {
        "legacy_violations": legacy_violations,
        "invalid_json": invalid_json,
    }


def _integrity(connection: sqlite3.Connection) -> dict[str, Any]:
    integrity_rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    quick_rows = [row[0] for row in connection.execute("PRAGMA quick_check")]
    foreign_key_rows = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
    return {
        "integrity_check": integrity_rows,
        "quick_check": quick_rows,
        "foreign_key_check": foreign_key_rows,
        "ok": integrity_rows == ["ok"] and quick_rows == ["ok"] and not foreign_key_rows,
    }


def _faculty_enrichment_audit(
    database: Path,
    allowlist: Path,
    institution_id: str,
    expected_count: int,
) -> dict[str, Any]:
    """Delegate to the same fail-closed validator used by promotion."""

    script = REPOSITORY_ROOT / "scripts" / "promote_faculty_pilot.py"
    spec = importlib.util.spec_from_file_location("faculty_promotion_acceptance", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Faculty promotion validator: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.audit_faculty_enrichment(
        database,
        allowlist,
        institution_id=institution_id,
        expected_count=expected_count,
    )


def audit_database(
    database: Path,
    *,
    sample_limit: int = 3,
    faculty_enrichment_allowlist: Path | None = None,
    faculty_enrichment_institution_id: str | None = None,
    faculty_enrichment_expected_count: int = 124,
) -> dict[str, Any]:
    if faculty_enrichment_allowlist is not None and not str(
        faculty_enrichment_institution_id or ""
    ).strip():
        raise ValueError(
            "faculty_enrichment_institution_id is required with the Faculty allowlist"
        )
    faculty_enrichment = (
        _faculty_enrichment_audit(
            database,
            faculty_enrichment_allowlist,
            str(faculty_enrichment_institution_id),
            int(faculty_enrichment_expected_count),
        )
        if faculty_enrichment_allowlist is not None
        else None
    )
    connection = _open_read_only(database)
    try:
        integrity = _integrity(connection)
        aliases = _aliases(connection)
        alias_integrity = _alias_integrity(connection, aliases)
        records = _active_records(connection)
        active_by_id = {record["person_id"]: record for record in records}
        publication_counts = _publication_counts(connection, active_by_id, aliases)
        email_evidence = _official_person_local_email_evidence(connection, aliases)
        schools = _per_school(records, publication_counts, sample_limit)
        shared_email_groups = _shared_email_groups(records, aliases, publication_counts)
        strong_duplicates = _strong_identity_duplicates(
            connection, records, aliases, publication_counts
        )
        exact_profile_groups = _exact_profile_groups(records, aliases, publication_counts)
        same_name_groups = _same_name_groups(records, aliases, publication_counts)
        named_cases = _named_case_report(records, publication_counts, email_evidence)
        active_serving_anomalies = _active_serving_anomalies(records)
        serving_json = _serving_json_audit(connection)
        legacy_fields = serving_json["legacy_violations"]
        invalid_json = serving_json["invalid_json"]

        observed_school_names = {item["school"] for item in schools}
        observed_school_folds = {_fold(name) for name in observed_school_names}
        expected_school_folds = {_fold(name) for name in EXPECTED_UGC_SCHOOLS}
        missing_schools = sorted(
            name for name in EXPECTED_UGC_SCHOOLS if _fold(name) not in observed_school_folds
        )
        unexpected_schools = sorted(
            name for name in observed_school_names if _fold(name) not in expected_school_folds
        )

        acceptance_checks = {
            "integrity_ok": integrity["ok"],
            "ugc_eight_schools_exact": (
                len(schools) == len(EXPECTED_UGC_SCHOOLS)
                and not missing_schools
                and not unexpected_schools
            ),
            "shared_email_cross_canonical_groups_zero": not shared_email_groups,
            "identity_index_available": strong_duplicates["identity_index_available"],
            "identity_index_complete": strong_duplicates["identity_index_complete"],
            "identity_alias_integrity_ok": not alias_integrity["violations"],
            "strong_orcid_duplicates_zero": strong_duplicates["counts"]["orcid"] == 0,
            "strong_openalex_duplicates_zero": strong_duplicates["counts"]["openalex"] == 0,
            "strong_profile_duplicates_zero": strong_duplicates["counts"]["profile"] == 0,
            "active_non_person_display_names_zero": not active_serving_anomalies[
                "non_person_display_names"
            ],
            "active_profile_urls_usable": not active_serving_anomalies[
                "unusable_profile_urls"
            ],
            "named_cases_pass": all(case["pass"] for case in named_cases),
            "legacy_supervision_fields_zero": not legacy_fields,
            "serving_record_json_valid": not invalid_json,
        }
        if faculty_enrichment is not None:
            acceptance_checks["faculty_enrichment_exact_cohort_pass"] = bool(
                faculty_enrichment["pass"]
            )
        return {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "database": str(database.resolve()),
            "read_only": True,
            "definitions": {
                "official_research_areas": (
                    "Non-empty canonical research_areas_json derived from archived official sources."
                ),
                "meaningful_official_publications": (
                    "Rows in official_publication_fingerprints passing "
                    "is_meaningful_publication_fingerprint after alias resolution."
                ),
                "strong_identity_duplicates": (
                    "Distinct active canonical records at one institution sharing normalized ORCID, "
                    "OpenAlex author ID, or indexed strong profile/profile_exact identity."
                ),
                "exact_profile_groups": (
                    "Informational exact normalized profile URL groups; shared directory/group URLs "
                    "may require human classification."
                ),
                "same_name_groups": (
                    "Informational exact NFKC/case-folded same-school name groups; same name alone "
                    "is never sufficient to merge."
                ),
                "active_non_person_display_names": (
                    "Critical active records whose canonical display name is unmistakable page/UI "
                    "chrome, a collective role heading, a news headline, or a sentence fragment. "
                    "Separate appointment titles are not used by this check."
                ),
                "unusable_profile_urls": (
                    "Critical non-HTTP(S), malformed, 404, external research-identity, aggregate "
                    "directory, or generic research-portal-root URLs found in canonical profile_url "
                    "or record_json.profile_urls."
                ),
            },
            "summary": {
                "schools": len(schools),
                "active": len(records),
                "people_with_meaningful_official_publications": len(publication_counts),
                "shared_email_groups": len(shared_email_groups),
                "strong_identity_duplicate_groups": strong_duplicates["counts"],
                "exact_profile_groups": len(exact_profile_groups),
                "same_name_groups": len(same_name_groups),
                "named_case_failures": sum(not case["pass"] for case in named_cases),
                "active_non_person_display_names": len(
                    active_serving_anomalies["non_person_display_names"]
                ),
                "unusable_profile_urls": len(
                    active_serving_anomalies["unusable_profile_urls"]
                ),
                "legacy_supervision_field_records": len(legacy_fields),
                "invalid_serving_record_json": len(invalid_json),
                "faculty_enrichment_exact_cohort_pass": (
                    faculty_enrichment["pass"]
                    if faculty_enrichment is not None
                    else None
                ),
            },
            "acceptance": {
                "pass": all(acceptance_checks.values()),
                "checks": acceptance_checks,
            },
            "integrity": integrity,
            "school_set": {
                "expected": sorted(EXPECTED_UGC_SCHOOLS),
                "observed": sorted(observed_school_names),
                "missing": missing_schools,
                "unexpected": unexpected_schools,
            },
            "schools": schools,
            "named_cases": named_cases,
            "shared_email_groups": shared_email_groups,
            "strong_identity_duplicates": strong_duplicates,
            "identity_alias_integrity": alias_integrity,
            "email_evidence": {
                "available": email_evidence["available"],
                "official_person_local_pairs": len(email_evidence["pairs"]),
                "missing_columns": email_evidence["missing_columns"],
            },
            "exact_profile_groups": exact_profile_groups,
            "same_name_groups": same_name_groups,
            "active_serving_anomalies": active_serving_anomalies,
            "legacy_supervision_fields": legacy_fields,
            "invalid_serving_record_json": invalid_json,
            "faculty_enrichment": faculty_enrichment,
        }
    finally:
        connection.close()


def _write_report(report: dict[str, Any], output: str) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output == "-":
        sys.stdout.write(rendered)
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Merged SQLite PI index database")
    parser.add_argument(
        "--out",
        default="-",
        help="JSON output path, or '-' for stdout (default)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=3,
        help="Per-school sample rows for each missing-field category (default: 3)",
    )
    parser.add_argument(
        "--fail-on-anomalies",
        action="store_true",
        help="Exit 1 when any critical acceptance check fails",
    )
    parser.add_argument(
        "--faculty-enrichment-allowlist",
        type=Path,
        help=(
            "Optional exact person allowlist for OpenAlex/publication-vector/career-vector "
            "acceptance"
        ),
    )
    parser.add_argument(
        "--faculty-enrichment-institution-id",
        help="Required with --faculty-enrichment-allowlist",
    )
    parser.add_argument(
        "--faculty-enrichment-expected-count",
        type=int,
        default=124,
        help="Exact Faculty cohort size (default: 124)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.samples < 0:
        raise SystemExit("--samples must be >= 0")
    if args.faculty_enrichment_expected_count < 1:
        raise SystemExit("--faculty-enrichment-expected-count must be >= 1")
    if args.faculty_enrichment_allowlist and not args.faculty_enrichment_institution_id:
        raise SystemExit(
            "--faculty-enrichment-institution-id is required with "
            "--faculty-enrichment-allowlist"
        )
    report = audit_database(
        args.db,
        sample_limit=args.samples,
        faculty_enrichment_allowlist=args.faculty_enrichment_allowlist,
        faculty_enrichment_institution_id=args.faculty_enrichment_institution_id,
        faculty_enrichment_expected_count=args.faculty_enrichment_expected_count,
    )
    _write_report(report, args.out)
    if args.fail_on_anomalies and not report["acceptance"]["pass"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
