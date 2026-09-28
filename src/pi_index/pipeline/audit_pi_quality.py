from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Any, Iterable

from ..parsers.publications import is_meaningful_publication_fingerprint


REPORT_BASENAME = "pi_quality_audit"
RECORD_SAMPLE_CATEGORIES = (
    "active",
    "no_email",
    "no_title",
    "no_profile",
    "no_research_areas",
    "meaningful_publication_people",
    "no_research_evidence",
    "suspicious_title",
    "incomplete_name",
)
METRIC_CATEGORIES = RECORD_SAMPLE_CATEGORIES + (
    "exact_same_name_groups",
    "shared_email_groups",
)

_BIOGRAPHY_TITLE_RE = re.compile(
    r"\b(?:is|was|has been|currently serves?|joined|received (?:his|her|their)|"
    r"obtained (?:his|her|their)|research (?:focus|interests?)|"
    r"before (?:joining|coming)|holds? (?:a|the) (?:degree|position))\b",
    re.IGNORECASE,
)
_GENERIC_NAME_RE = re.compile(
    r"^(?:unknown|faculty|staff|profile|administrator|admin|academic|member|name)$",
    re.IGNORECASE,
)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_list(value: Any) -> list[str]:
    parsed = _json_value(value, value)
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        cleaned = _clean_text(item)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            result.append(cleaned)
            seen.add(key)
    return result


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def _record_from_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_value(_row_value(row, "record_json"), {})
    if not isinstance(payload, dict):
        payload = {}

    emails = _clean_list(_row_value(row, "emails_json", payload.get("emails")))
    research_areas = _clean_list(
        _row_value(row, "research_areas_json", payload.get("research_areas"))
    )
    profile_urls = _clean_list(payload.get("profile_urls"))
    profile = _clean_text(_row_value(row, "profile_url", payload.get("profile_url")))
    if not profile and profile_urls:
        profile = profile_urls[0]

    external_ids = payload.get("external_ids")
    if not isinstance(external_ids, dict):
        external_ids = {}
    evidence_ids = _clean_list(payload.get("source_evidence_ids"))
    status = _clean_text(
        _row_value(row, "membership_status", payload.get("membership_status") or "active")
    ).casefold()

    return {
        "person_id": _clean_text(_row_value(row, "person_id", payload.get("person_id"))),
        "name": _clean_text(_row_value(row, "display_name", payload.get("display_name"))),
        "institution_id": _clean_text(
            _row_value(row, "institution_id", payload.get("institution_id"))
        ),
        "school": _clean_text(
            _row_value(row, "institution_name", payload.get("institution_name"))
        ),
        "title": _clean_text(_row_value(row, "title", payload.get("title"))) or None,
        "emails": [email.casefold().removeprefix("mailto:") for email in emails],
        "profile": profile or None,
        "research_areas": research_areas,
        "external_ids": external_ids,
        "source_evidence_ids": evidence_ids,
        "membership_status": status or "active",
    }


def _normalize_exact_name(name: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name)).strip().casefold()


def suspicious_title_reasons(title: str | None) -> list[str]:
    """Return deterministic reasons that a title looks like biography spillover."""

    cleaned = _clean_text(title)
    if not cleaned:
        return []
    reasons: list[str] = []
    if len(cleaned) > 160 or len(cleaned.split()) > 25:
        reasons.append("overlong")
    if len(cleaned) >= 80 and _BIOGRAPHY_TITLE_RE.search(cleaned):
        reasons.append("biography_sentence")
    if len(cleaned) >= 100 and len(re.findall(r"[.!?](?:\s|$)", cleaned)) >= 2:
        reasons.append("multiple_sentences")
    return reasons


def incomplete_name_reasons(name: str | None) -> list[str]:
    """Flag structurally incomplete names using name-shape checks only."""

    cleaned = _clean_text(name)
    if not cleaned:
        return ["blank"]
    if _GENERIC_NAME_RE.fullmatch(cleaned):
        return ["generic_label"]
    if "<" in cleaned or ">" in cleaned:
        return ["markup_contamination"]
    tokens = re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", cleaned, flags=re.UNICODE)
    if not tokens:
        return ["no_name_tokens"]
    if len(tokens) == 1 and not _CJK_RE.search(cleaned):
        return ["single_token"]
    if not _CJK_RE.search(cleaned) and all(
        len(token.replace("-", "").replace("'", "")) == 1 for token in tokens
    ):
        return ["initials_only"]
    return []


def _alias_map(conn: sqlite3.Connection) -> dict[str, str]:
    if not _table_exists(conn, "pi_identity_aliases"):
        return {}
    return {
        _clean_text(row["alias_person_id"]): _clean_text(row["canonical_person_id"])
        for row in conn.execute(
            "SELECT alias_person_id, canonical_person_id FROM pi_identity_aliases"
        )
        if _clean_text(row["alias_person_id"]) and _clean_text(row["canonical_person_id"])
    }


def _resolve_person_id(person_id: str, aliases: dict[str, str]) -> str:
    resolved = person_id
    seen: set[str] = set()
    while resolved in aliases and resolved not in seen:
        seen.add(resolved)
        resolved = aliases[resolved]
    return resolved


def _meaningful_publications(
    conn: sqlite3.Connection,
    active_by_id: dict[str, dict[str, Any]],
    aliases: dict[str, str],
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    if not _table_exists(conn, "official_publication_fingerprints"):
        return {}
    for row in conn.execute("SELECT * FROM official_publication_fingerprints"):
        fingerprint = dict(row)
        if not is_meaningful_publication_fingerprint(fingerprint):
            continue
        person_id = _resolve_person_id(_clean_text(fingerprint.get("person_id")), aliases)
        if person_id in active_by_id:
            counts[person_id] += 1
    return dict(counts)


def _sample_record(
    record: dict[str, Any],
    *,
    publication_count: int = 0,
    reasons: Iterable[str] = (),
) -> dict[str, Any]:
    sample = {
        "name": record["name"],
        "school": record["school"],
        "title": record["title"],
        "email": record["emails"][0] if record["emails"] else None,
        "emails": record["emails"],
        "profile": record["profile"],
        "source_ids": {
            "canonical_person_id": record["person_id"],
            "external_ids": record["external_ids"],
            "evidence_ids": record["source_evidence_ids"][:5],
            "evidence_id_count": len(record["source_evidence_ids"]),
        },
    }
    if publication_count:
        sample["meaningful_publication_count"] = publication_count
    reason_list = list(reasons)
    if reason_list:
        sample["reasons"] = reason_list
    return sample


def _record_sort_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (record["school"].casefold(), record["name"].casefold(), record["person_id"])


def _group_sample(
    *,
    key_name: str,
    key_value: str,
    members: Iterable[dict[str, Any]],
    publication_counts: dict[str, int],
) -> dict[str, Any]:
    sorted_members = sorted(members, key=_record_sort_key)
    return {
        key_name: key_value,
        "members": [
            _sample_record(
                member,
                publication_count=publication_counts.get(member["person_id"], 0),
            )
            for member in sorted_members
        ],
    }


def _shared_email_groups(
    conn: sqlite3.Connection,
    active_by_id: dict[str, dict[str, Any]],
    aliases: dict[str, str],
    publication_counts: dict[str, int],
) -> list[dict[str, Any]]:
    raw_bindings: dict[str, set[str]] = defaultdict(set)
    canonical_bindings: dict[str, set[str]] = defaultdict(set)

    for person_id, record in active_by_id.items():
        for email in record["emails"]:
            raw_bindings[email].add(person_id)
            canonical_bindings[email].add(person_id)

    # Historical/raw evidence is used only to identify alias bindings for an
    # email that is still present on the resolved canonical record.  This
    # prevents stale, rejected evidence from becoming a current shared email.
    if _table_exists(conn, "email_evidence"):
        for row in conn.execute("SELECT email, person_id FROM email_evidence"):
            raw_person_id = _clean_text(row["person_id"])
            email = _clean_text(row["email"]).casefold().removeprefix("mailto:")
            if not raw_person_id or not email:
                continue
            canonical_id = _resolve_person_id(raw_person_id, aliases)
            canonical_record = active_by_id.get(canonical_id)
            if canonical_record is None or email not in canonical_record["emails"]:
                continue
            raw_bindings[email].add(raw_person_id)
            canonical_bindings[email].add(canonical_id)

    groups: list[dict[str, Any]] = []
    for email in sorted(raw_bindings):
        raw_ids = sorted(raw_bindings[email])
        canonical_ids = sorted(
            {
                _resolve_person_id(person_id, aliases)
                for person_id in raw_ids
                if _resolve_person_id(person_id, aliases) in active_by_id
            }
            | canonical_bindings[email]
        )
        if len(canonical_ids) > 1:
            classification = "cross_canonical"
        elif len(canonical_ids) == 1 and len(raw_ids) > 1:
            classification = "same_person_alias"
        else:
            continue
        members = [active_by_id[person_id] for person_id in canonical_ids]
        group = _group_sample(
            key_name="email",
            key_value=email,
            members=members,
            publication_counts=publication_counts,
        )
        group.update(
            {
                "classification": classification,
                "raw_person_ids": raw_ids,
                "canonical_person_ids": canonical_ids,
            }
        )
        groups.append(group)
    return groups


def _limit(values: Iterable[Any], samples: int) -> list[Any]:
    return list(values)[:samples]


def build_pi_quality_audit(
    conn: sqlite3.Connection,
    *,
    database: str | Path | None = None,
    samples: int = 3,
) -> dict[str, Any]:
    """Build a neutral, read-only quality report from a PI index database."""

    if samples < 1:
        raise ValueError("samples must be at least 1")
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "canonical_pi_records"):
            raise ValueError("Database has no canonical_pi_records table")

        records = [
            _record_from_row(row)
            for row in conn.execute("SELECT * FROM canonical_pi_records")
        ]
        active_records = sorted(
            (record for record in records if record["membership_status"] == "active"),
            key=_record_sort_key,
        )
        active_by_id = {
            record["person_id"]: record for record in active_records if record["person_id"]
        }
        aliases = _alias_map(conn)
        publication_counts = _meaningful_publications(conn, active_by_id, aliases)

        title_reasons = {
            record["person_id"]: suspicious_title_reasons(record["title"])
            for record in active_records
        }
        name_reasons = {
            record["person_id"]: incomplete_name_reasons(record["name"])
            for record in active_records
        }
        category_records: dict[str, list[dict[str, Any]]] = {
            "active": active_records,
            "no_email": [record for record in active_records if not record["emails"]],
            "no_title": [record for record in active_records if not record["title"]],
            "no_profile": [record for record in active_records if not record["profile"]],
            "no_research_areas": [
                record for record in active_records if not record["research_areas"]
            ],
            "meaningful_publication_people": [
                record for record in active_records if record["person_id"] in publication_counts
            ],
            "no_research_evidence": [
                record
                for record in active_records
                if not record["research_areas"]
                and record["person_id"] not in publication_counts
            ],
            "suspicious_title": [
                record for record in active_records if title_reasons[record["person_id"]]
            ],
            "incomplete_name": [
                record for record in active_records if name_reasons[record["person_id"]]
            ],
        }

        name_members: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for record in active_records:
            normalized_name = _normalize_exact_name(record["name"])
            if normalized_name:
                name_members[(record["institution_id"], normalized_name)].append(record)
        exact_name_groups: list[dict[str, Any]] = []
        for (institution_id, normalized_name), members in sorted(name_members.items()):
            if len({member["person_id"] for member in members}) < 2:
                continue
            group = _group_sample(
                key_name="normalized_name",
                key_value=normalized_name,
                members=members,
                publication_counts=publication_counts,
            )
            group["institution_id"] = institution_id
            group["school"] = members[0]["school"]
            exact_name_groups.append(group)

        shared_email_groups = _shared_email_groups(
            conn, active_by_id, aliases, publication_counts
        )

        schools: list[dict[str, Any]] = []
        school_keys = sorted(
            {(record["institution_id"], record["school"]) for record in active_records},
            key=lambda value: (value[1].casefold(), value[0]),
        )
        for institution_id, school_name in school_keys:
            by_category = {
                category: [
                    record
                    for record in category_records[category]
                    if record["institution_id"] == institution_id
                ]
                for category in RECORD_SAMPLE_CATEGORIES
            }
            school_name_groups = [
                group
                for group in exact_name_groups
                if group["institution_id"] == institution_id
            ]
            school_email_groups = [
                group
                for group in shared_email_groups
                if any(
                    active_by_id[person_id]["institution_id"] == institution_id
                    for person_id in group["canonical_person_ids"]
                )
            ]
            school_samples: dict[str, list[dict[str, Any]]] = {}
            for category, category_values in by_category.items():
                sample_values: list[dict[str, Any]] = []
                for record in _limit(category_values, samples):
                    reasons: Iterable[str] = ()
                    if category == "suspicious_title":
                        reasons = title_reasons[record["person_id"]]
                    elif category == "incomplete_name":
                        reasons = name_reasons[record["person_id"]]
                    sample_values.append(
                        _sample_record(
                            record,
                            publication_count=publication_counts.get(record["person_id"], 0),
                            reasons=reasons,
                        )
                    )
                school_samples[category] = sample_values
            school_samples["exact_same_name_groups"] = _limit(school_name_groups, samples)
            school_samples["shared_email_groups"] = _limit(school_email_groups, samples)
            schools.append(
                {
                    "institution_id": institution_id,
                    "institution_name": school_name,
                    "metrics": {
                        **{
                            category: len(values)
                            for category, values in by_category.items()
                        },
                        "exact_same_name_groups": len(school_name_groups),
                        "shared_email_groups": len(school_email_groups),
                        "shared_email_same_person_alias_groups": sum(
                            group["classification"] == "same_person_alias"
                            for group in school_email_groups
                        ),
                        "shared_email_cross_canonical_groups": sum(
                            group["classification"] == "cross_canonical"
                            for group in school_email_groups
                        ),
                    },
                    "samples": school_samples,
                }
            )

        global_samples: dict[str, list[dict[str, Any]]] = {}
        for category, category_values in category_records.items():
            values: list[dict[str, Any]] = []
            for record in _limit(category_values, samples):
                reasons: Iterable[str] = ()
                if category == "suspicious_title":
                    reasons = title_reasons[record["person_id"]]
                elif category == "incomplete_name":
                    reasons = name_reasons[record["person_id"]]
                values.append(
                    _sample_record(
                        record,
                        publication_count=publication_counts.get(record["person_id"], 0),
                        reasons=reasons,
                    )
                )
            global_samples[category] = values
        global_samples["exact_same_name_groups"] = _limit(exact_name_groups, samples)
        global_samples["shared_email_groups"] = _limit(shared_email_groups, samples)

        return {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "database": str(database) if database is not None else None,
            "sample_limit": samples,
            "definitions": {
                "active": "membership_status is exactly active; missing and inactive records are excluded",
                "meaningful_publication_people": (
                    "active people with at least one official fingerprint accepted by "
                    "parsers.publications.is_meaningful_publication_fingerprint"
                ),
                "no_research_evidence": (
                    "no structured research_areas and no meaningful official publication fingerprint"
                ),
                "suspicious_title": "overlong title or title text shaped like biography prose",
                "incomplete_name": "blank/generic/markup, single non-CJK token, or initials only",
                "exact_same_name_groups": (
                    "two or more active canonical records in one school with the same "
                    "NFKC, casefolded, whitespace-normalized name"
                ),
                "shared_email_groups": (
                    "current canonical email assignments; raw email evidence is consulted only "
                    "to distinguish same_person_alias from cross_canonical bindings"
                ),
            },
            "summary": {
                "schools": len(schools),
                **{
                    category: len(values)
                    for category, values in category_records.items()
                },
                "exact_same_name_groups": len(exact_name_groups),
                "shared_email_groups": len(shared_email_groups),
                "shared_email_same_person_alias_groups": sum(
                    group["classification"] == "same_person_alias"
                    for group in shared_email_groups
                ),
                "shared_email_cross_canonical_groups": sum(
                    group["classification"] == "cross_canonical"
                    for group in shared_email_groups
                ),
            },
            "schools": schools,
            "samples": global_samples,
            "exact_same_name_groups": {
                "total": len(exact_name_groups),
                "samples": _limit(exact_name_groups, samples),
            },
            "shared_email_groups": {
                "total": len(shared_email_groups),
                "same_person_alias": sum(
                    group["classification"] == "same_person_alias"
                    for group in shared_email_groups
                ),
                "cross_canonical": sum(
                    group["classification"] == "cross_canonical"
                    for group in shared_email_groups
                ),
                "samples": _limit(shared_email_groups, samples),
            },
        }
    finally:
        conn.row_factory = previous_row_factory


def _markdown_escape(value: Any) -> str:
    return _clean_text(value).replace("|", "\\|")


def _sample_markdown(sample: dict[str, Any]) -> str:
    source_ids = sample.get("source_ids") or {}
    external = source_ids.get("external_ids") or {}
    source_text = source_ids.get("canonical_person_id") or "-"
    if external:
        source_text += "; external=" + json.dumps(external, ensure_ascii=False, sort_keys=True)
    reasons = sample.get("reasons") or []
    reason_text = f"; reasons={','.join(reasons)}" if reasons else ""
    return (
        f"{_markdown_escape(sample.get('school'))} / {_markdown_escape(sample.get('name'))}"
        f" | title={_markdown_escape(sample.get('title') or '-')}"
        f" | email={_markdown_escape(sample.get('email') or '-')}"
        f" | profile={_markdown_escape(sample.get('profile') or '-')}"
        f" | source IDs={_markdown_escape(source_text)}{reason_text}"
    )


def render_pi_quality_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# PI data quality audit",
        "",
        f"Generated: `{report['generated_at']}`  ",
        f"Database: `{report.get('database') or '-'}`  ",
        f"Sample limit: `{report['sample_limit']}`",
        "",
        "## Counts by school",
        "",
        "| School | active | no_email | no_title | no_profile | no_research_areas | meaningful_publication_people | no_research_evidence | suspicious_title | incomplete_name | exact_same_name_groups | shared_email_groups |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for school in report["schools"]:
        metrics = school["metrics"]
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_escape(school["institution_name"]),
                    *(str(metrics[key]) for key in METRIC_CATEGORIES),
                ]
            )
            + " |"
        )

    lines.extend(["", "## Shared-email identity split", ""])
    shared = report["shared_email_groups"]
    lines.append(
        f"Total `{shared['total']}`; same-person alias `{shared['same_person_alias']}`; "
        f"cross-canonical `{shared['cross_canonical']}`."
    )

    lines.extend(["", "## Samples", ""])
    for category in METRIC_CATEGORIES:
        lines.extend([f"### {category}", ""])
        values = report["samples"].get(category) or []
        if not values:
            lines.extend(["- None", ""])
            continue
        if category == "exact_same_name_groups":
            for group in values:
                lines.append(
                    f"- `{_markdown_escape(group['normalized_name'])}` ({_markdown_escape(group['school'])})"
                )
                for member in group["members"]:
                    lines.append(f"  - {_sample_markdown(member)}")
        elif category == "shared_email_groups":
            for group in values:
                lines.append(
                    f"- `{_markdown_escape(group['email'])}`: `{group['classification']}`; "
                    f"canonical IDs={', '.join(group['canonical_person_ids'])}"
                )
                for member in group["members"]:
                    lines.append(f"  - {_sample_markdown(member)}")
        else:
            lines.extend(f"- {_sample_markdown(value)}" for value in values)
        lines.append("")

    lines.extend(["## Definitions", ""])
    for key, value in report["definitions"].items():
        lines.append(f"- `{key}`: {value}")
    lines.append("")
    return "\n".join(lines)


def _output_paths(output: str | Path) -> tuple[Path, Path]:
    path = Path(output)
    if path.suffix.casefold() == ".json":
        return path, path.with_suffix(".md")
    if path.suffix.casefold() == ".md":
        return path.with_suffix(".json"), path
    return path / f"{REPORT_BASENAME}.json", path / f"{REPORT_BASENAME}.md"


def write_pi_quality_audit(
    report: dict[str, Any], output: str | Path
) -> dict[str, str]:
    json_path, markdown_path = _output_paths(output)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_pi_quality_markdown(report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def run_pi_quality_audit(
    database: str | Path,
    output: str | Path,
    *,
    samples: int = 3,
) -> tuple[dict[str, Any], dict[str, str]]:
    database_path = Path(database)
    if not database_path.is_file():
        raise FileNotFoundError(f"PI index database not found: {database_path}")
    connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        report = build_pi_quality_audit(
            connection,
            database=str(database_path),
            samples=samples,
        )
    finally:
        connection.close()
    return report, write_pi_quality_audit(report, output)
