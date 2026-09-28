#!/usr/bin/env python3
"""Promote one accepted Faculty pilot enrichment into a production PI Pool.

The default mode is a strictly read-only preflight.  ``--apply`` requires an
explicit person allowlist and a new SQLite backup path.  The production
database is backed up first; all schema and data changes then happen inside one
``BEGIN IMMEDIATE`` transaction and are rolled back on any conflict.

Only publication/OpenAlex/vector enrichment is promoted.  Canonical PI rows,
identity aliases, contact fields and raw crawl archives are deliberately not
overwritten by this utility.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
from typing import Any, Iterable, Iterator, Mapping, Sequence


PAPER_REPRESENTATION = "publication_vector_256"
CAREER_REPRESENTATION = "career_vector_256"
PAPER_QUEUE_KIND = "paper_vector_256"
CAREER_QUEUE_KIND = "career_vector_256"
OPENALEX_QUEUE_KIND = "openalex_works_sync"
EXPECTED_ENCODER = "production_terms_v1"
EXPECTED_FEATURE_LIMIT = 256
# A promoted Faculty snapshot must be a settled baseline.  ``missing`` rows are
# deliberately excluded: they may be legitimate lifecycle history, but they
# may also be one-run remnants of the polluted pilot inventory.  The vector
# dependency checks below make a career vector that still includes such rows
# fail closed rather than silently promoting it.
PROMOTABLE_RELATIONSHIP_STATUSES = ("active",)
PROMOTION_SCHEMA_VERSION = "2"
HEADROOM_BYTES = 64 * 1024 * 1024
SQL_CHUNK = 400


MIGRATABLE_TABLES = (
    "publication_refresh_runs",
    "official_publication_fingerprints",
    "official_publication_refresh_state",
    "official_publication_source_claims",
    "vector_dirty_queue",
    "openalex_sync_runs",
    "openalex_author_links",
    "openalex_works",
    "openalex_person_works",
    "openalex_work_vectors",
    "pi_career_vectors",
)


class PromotionError(RuntimeError):
    """Raised when a preflight or transactional safety gate fails."""


def _quote_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise PromotionError(f"Unsafe SQLite identifier: {value!r}")
    return f'"{value}"'


def _fold(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _normalized_openalex_id(value: Any, prefix: str) -> str:
    match = re.search(rf"(?:openalex\.org/)?({prefix}\d+)\b", str(value or ""), flags=re.I)
    return match.group(1).upper() if match else ""


def _normalized_doi(value: Any) -> str:
    text = str(value or "").strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text


def _reviewed_link_contract(
    row: sqlite3.Row,
    *,
    person_id: str,
    institution_id: str,
) -> tuple[set[str], dict[str, Any], list[str]]:
    """Return the reviewed Author-ID set and fail-closed audit errors."""

    errors: list[str] = []
    evidence = _loads(row["evidence_json"] if "evidence_json" in row.keys() else None, {})
    reviewed = evidence.get("reviewed_identity") if isinstance(evidence, dict) else None
    if not isinstance(reviewed, dict) or reviewed.get("reviewed") is not True:
        return set(), {}, [f"{person_id}: OpenAlex identity is not human-reviewed"]
    if str(row["match_method"] or "") != "reviewed_openalex_identity_manifest_v1":
        errors.append(f"{person_id}: reviewed OpenAlex link has an unexpected match method")
    if reviewed.get("audit_type") != "reviewed_openalex_identity_manifest":
        errors.append(f"{person_id}: reviewed OpenAlex link has an invalid audit type")
    manifest_hash = str(reviewed.get("manifest_sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", manifest_hash, flags=re.I) is None:
        errors.append(f"{person_id}: reviewed OpenAlex link has no valid manifest hash")
    if str(reviewed.get("institution_id") or "") != institution_id:
        errors.append(f"{person_id}: reviewed OpenAlex link institution evidence differs")
    primary = _normalized_openalex_id(row["openalex_author_id"], "A")
    reviewed_primary = _normalized_openalex_id(
        reviewed.get("primary_openalex_author_id"), "A"
    )
    if not primary or reviewed_primary != primary:
        errors.append(f"{person_id}: reviewed primary Author ID differs from stored link")
    raw_confirmed = reviewed.get("confirmed_openalex_author_ids")
    if not isinstance(raw_confirmed, list):
        raw_confirmed = []
    allowed = {
        author_id
        for value in raw_confirmed
        if (author_id := _normalized_openalex_id(value, "A"))
    }
    top_level = evidence.get("confirmed_openalex_author_ids")
    if isinstance(top_level, list):
        persisted = {
            author_id
            for value in top_level
            if (author_id := _normalized_openalex_id(value, "A"))
        }
        if persisted != allowed:
            errors.append(f"{person_id}: persisted confirmed profile set differs from review")
    if primary not in allowed:
        errors.append(f"{person_id}: primary Author ID is absent from reviewed profile set")
    if len(allowed) != len(raw_confirmed):
        errors.append(f"{person_id}: reviewed profile set contains invalid or duplicate IDs")
    if reviewed.get("sync_mode") != "full_profile":
        errors.append(f"{person_id}: Author-linked review is not a full-profile decision")
    policy = reviewed.get("work_policy")
    policy_hash = reviewed.get("work_policy_sha256")
    if policy is None:
        if policy_hash not in (None, ""):
            errors.append(f"{person_id}: full-profile review has an unexpected policy hash")
    elif not isinstance(policy, dict) or policy.get("mode") not in {
        "field_allowlist",
        "exact_work_allowlist",
    }:
        errors.append(f"{person_id}: reviewed Work policy is invalid")
    else:
        if policy.get("mode") == "exact_work_allowlist":
            raw_work_ids = policy.get("work_ids")
            normalized_policy_work_ids = (
                [str(work_id or "") for work_id in raw_work_ids]
                if isinstance(raw_work_ids, list)
                else []
            )
            if (
                not isinstance(raw_work_ids, list)
                or not raw_work_ids
                or any(
                    re.fullmatch(r"W\d+", work_id) is None
                    for work_id in normalized_policy_work_ids
                )
                or len(set(normalized_policy_work_ids))
                != len(normalized_policy_work_ids)
            ):
                errors.append(
                    f"{person_id}: exact Work allowlist must contain unique OpenAlex Work IDs"
                )
        expected_hash = hashlib.sha256(
            _canonical_json(policy).encode("utf-8")
        ).hexdigest()
        if str(policy_hash or "") != expected_hash:
            errors.append(f"{person_id}: reviewed Work policy hash differs")
    return allowed, reviewed, errors


def _work_authorship_ids(row: sqlite3.Row) -> set[str]:
    raw = _loads(row["raw_json"] if "raw_json" in row.keys() else None, {})
    result: set[str] = set()
    if not isinstance(raw, dict):
        return result
    for authorship in raw.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") or {}
        if isinstance(author, dict):
            author_id = _normalized_openalex_id(author.get("id"), "A")
            if author_id:
                result.add(author_id)
    return result


def _persisted_link_author_ids(row: sqlite3.Row) -> set[str]:
    result = {
        author_id
        for value in (row["openalex_author_id"],)
        if (author_id := _normalized_openalex_id(value, "A"))
    }
    evidence = _loads(
        row["evidence_json"] if "evidence_json" in row.keys() else None,
        {},
    )
    if isinstance(evidence, dict):
        values = evidence.get("confirmed_openalex_author_ids") or []
        reviewed = evidence.get("reviewed_identity")
        if isinstance(reviewed, dict):
            values = [*values, *(reviewed.get("confirmed_openalex_author_ids") or [])]
        for value in values:
            author_id = _normalized_openalex_id(value, "A")
            if author_id:
                result.add(author_id)
    return result


def _safe_existing_db(path_value: str | Path, label: str) -> Path:
    requested = Path(path_value).expanduser()
    if requested.is_symlink():
        raise PromotionError(f"{label} database may not be a symlink: {requested}")
    path = requested.resolve(strict=True)
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise PromotionError(f"{label} database is not an ordinary file: {path}")
    return path


def _safe_backup_path(
    path_value: str | Path | None,
    *,
    source_db: Path,
    target_db: Path,
    required: bool,
) -> Path | None:
    if path_value is None:
        if required:
            raise PromotionError("--apply requires --backup-db")
        return None
    requested = Path(path_value).expanduser()
    if requested.exists() or requested.is_symlink():
        raise PromotionError(f"Backup path must not already exist: {requested}")
    parent = requested.parent
    while not parent.exists():
        if parent == parent.parent:
            raise PromotionError(f"Backup path has no existing ancestor: {requested}")
        parent = parent.parent
    if parent.is_symlink():
        raise PromotionError(f"Backup ancestor may not be a symlink: {parent}")
    resolved_parent = requested.parent.resolve(strict=False)
    backup = resolved_parent / requested.name
    if backup.resolve(strict=False) in {source_db, target_db}:
        raise PromotionError("Backup path must differ from both databases")
    return backup


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _open_writable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _table_info(connection: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    if not _table_exists(connection, table):
        return []
    return list(connection.execute(f"PRAGMA table_info({_quote_identifier(table)})"))


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in _table_info(connection, table)]


def _primary_key(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row["name"])
        for row in sorted(_table_info(connection, table), key=lambda row: int(row["pk"]))
        if int(row["pk"])
    ]


def _chunks(values: Iterable[str], size: int = SQL_CHUNK) -> Iterator[list[str]]:
    ordered = sorted({str(value) for value in values if str(value)})
    for offset in range(0, len(ordered), size):
        yield ordered[offset : offset + size]


def _rows_by_values(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    values: Iterable[str],
    *,
    extra_where: str | None = None,
    extra_params: Sequence[Any] = (),
) -> Iterator[sqlite3.Row]:
    if not _table_exists(connection, table) or column not in _columns(connection, table):
        return
    for chunk in _chunks(values):
        placeholders = ",".join("?" for _ in chunk)
        where = f"{_quote_identifier(column)} IN ({placeholders})"
        if extra_where:
            where = f"({where}) AND ({extra_where})"
        yield from connection.execute(
            f"SELECT * FROM {_quote_identifier(table)} WHERE {where}",
            [*chunk, *extra_params],
        )


def _load_allowlist(path_value: str | Path, expected_count: int) -> list[str]:
    path = Path(path_value).expanduser().resolve(strict=True)
    raw = path.read_text(encoding="utf-8-sig")
    if path.suffix.casefold() == ".json":
        payload = json.loads(raw)
        if isinstance(payload, dict):
            payload = payload.get("person_ids")
        if not isinstance(payload, list):
            raise PromotionError("JSON allowlist must be an array or {person_ids: [...]} object")
        values = [str(value).strip() for value in payload if str(value).strip()]
    else:
        values = [
            line.strip()
            for line in raw.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise PromotionError(f"Allowlist contains duplicate IDs: {duplicates[:5]}")
    if len(values) != int(expected_count):
        raise PromotionError(
            f"Allowlist must contain exactly {expected_count} unique people; found {len(values)}"
        )
    invalid = [value for value in values if not re.fullmatch(r"pi_[0-9a-f]{16}", value)]
    if invalid:
        raise PromotionError(f"Invalid person IDs in allowlist: {invalid[:5]}")
    return sorted(values)


def _type_affinity(type_name: str) -> str:
    value = str(type_name or "").upper()
    if "INT" in value:
        return "INTEGER"
    if any(token in value for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in value or not value:
        return "BLOB"
    if any(token in value for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _schema_plan(
    source: sqlite3.Connection, target: sqlite3.Connection
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for table in MIGRATABLE_TABLES:
        source_info = _table_info(source, table)
        if not source_info:
            conflicts.append(f"Pilot is missing required table {table}")
            continue
        if not _table_exists(target, table):
            row = source.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            ddl = str(row[0] or "") if row else ""
            if not ddl.upper().startswith("CREATE TABLE"):
                conflicts.append(f"Pilot has unusable DDL for {table}")
            else:
                actions.append({"kind": "create_table", "table": table, "sql": ddl})
            continue
        target_info = {str(row["name"]): row for row in _table_info(target, table)}
        for source_column in source_info:
            name = str(source_column["name"])
            target_column = target_info.get(name)
            if target_column is None:
                if int(source_column["pk"]):
                    conflicts.append(f"Target {table} is missing primary-key column {name}")
                    continue
                if int(source_column["notnull"]) and source_column["dflt_value"] is None:
                    conflicts.append(
                        f"Target {table} cannot add required column {name} without a default"
                    )
                    continue
                definition = f'{_quote_identifier(name)} {source_column["type"] or ""}'.strip()
                if int(source_column["notnull"]):
                    definition += " NOT NULL"
                if source_column["dflt_value"] is not None:
                    definition += f' DEFAULT {source_column["dflt_value"]}'
                actions.append(
                    {
                        "kind": "add_column",
                        "table": table,
                        "column": name,
                        "sql": f"ALTER TABLE {_quote_identifier(table)} ADD COLUMN {definition}",
                    }
                )
                continue
            if _type_affinity(source_column["type"]) != _type_affinity(target_column["type"]):
                conflicts.append(
                    f"Target {table}.{name} has incompatible type {target_column['type']!r}"
                )
            if int(source_column["pk"]) != int(target_column["pk"]):
                conflicts.append(f"Target {table}.{name} has incompatible primary-key position")
            if (
                table == "openalex_person_works"
                and name == "openalex_author_id"
                and not int(source_column["notnull"])
                and int(target_column["notnull"])
            ):
                actions.append(
                    {
                        "kind": "relax_openalex_person_work_author",
                        "table": table,
                        "column": name,
                    }
                )

    source_indexes = source.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master "
        "WHERE type='index' AND sql IS NOT NULL ORDER BY name"
    ).fetchall()
    target_indexes = {
        str(row["name"]): str(row["sql"] or "")
        for row in target.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        )
    }
    for row in source_indexes:
        table = str(row["tbl_name"])
        if table not in MIGRATABLE_TABLES:
            continue
        name = str(row["name"])
        ddl = str(row["sql"] or "")
        existing = target_indexes.get(name)
        if existing is None:
            actions.append({"kind": "create_index", "table": table, "index": name, "sql": ddl})
        elif " ".join(existing.split()).casefold() != " ".join(ddl.split()).casefold():
            conflicts.append(f"Target index {name} conflicts with pilot schema")
    return {
        "actions": actions,
        "conflicts": conflicts,
        "summary": {
            "create_tables": sum(action["kind"] == "create_table" for action in actions),
            "add_columns": sum(action["kind"] == "add_column" for action in actions),
            "create_indexes": sum(action["kind"] == "create_index" for action in actions),
            "relax_not_null": sum(
                action["kind"] == "relax_openalex_person_work_author"
                for action in actions
            ),
        },
    }


def _apply_schema_plan(target: sqlite3.Connection, plan: Mapping[str, Any]) -> None:
    if plan.get("conflicts"):
        raise PromotionError(f"Schema conflicts: {plan['conflicts']}")
    ordering = {
        "create_table": 0,
        "add_column": 1,
        "relax_openalex_person_work_author": 2,
        "create_index": 3,
    }
    for action in sorted(plan.get("actions") or [], key=lambda item: ordering[item["kind"]]):
        if action["kind"] != "relax_openalex_person_work_author":
            target.execute(str(action["sql"]))
            continue
        target.execute(
            "ALTER TABLE openalex_person_works RENAME TO openalex_person_works_v1"
        )
        target.execute(
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
        target.execute(
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
        target.execute("DROP TABLE openalex_person_works_v1")
        target.execute(
            "CREATE INDEX idx_openalex_person_works_current ON "
            "openalex_person_works(person_id, relationship_status, openalex_work_id)"
        )
        target.execute(
            "CREATE INDEX idx_openalex_person_works_work ON "
            "openalex_person_works(openalex_work_id, relationship_status, person_id)"
        )


def _orcid_values(row: sqlite3.Row) -> set[str]:
    payload = _loads(row["record_json"] if "record_json" in row.keys() else None, {})
    external = payload.get("external_ids") if isinstance(payload, dict) else None
    values: list[Any] = []
    if isinstance(external, dict):
        for key, value in external.items():
            if "orcid" in _fold(key):
                values.extend(value if isinstance(value, list) else [value])
    for key in ("orcid", "orcid_id"):
        if isinstance(payload, dict) and payload.get(key):
            values.append(payload[key])
    normalized: set[str] = set()
    for value in values:
        candidate = str(value or "").strip().rstrip("/").rsplit("/", 1)[-1].upper()
        if candidate:
            normalized.add(candidate)
    return normalized


def _records_by_person(
    connection: sqlite3.Connection, person_ids: Sequence[str]
) -> dict[str, sqlite3.Row]:
    if not _table_exists(connection, "canonical_pi_records"):
        raise PromotionError("Database is missing canonical_pi_records")
    return {
        str(row["person_id"]): row
        for row in _rows_by_values(
            connection, "canonical_pi_records", "person_id", person_ids
        )
    }


def _validate_identity(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    person_ids: Sequence[str],
    institution_id: str,
) -> list[str]:
    conflicts: list[str] = []
    source_rows = _records_by_person(source, person_ids)
    target_rows = _records_by_person(target, person_ids)
    missing_source = sorted(set(person_ids) - set(source_rows))
    missing_target = sorted(set(person_ids) - set(target_rows))
    if missing_source:
        conflicts.append(f"Pilot is missing allowlisted people: {missing_source[:5]}")
    if missing_target:
        conflicts.append(
            "Target is missing allowlisted people: "
            f"{missing_target[:5]}; apply the reviewed canonical identity split to "
            "the target first (enrichment promotion never creates canonical PI rows)"
        )
    for person_id in sorted(set(source_rows) & set(target_rows)):
        source_row = source_rows[person_id]
        target_row = target_rows[person_id]
        for label, row in (("pilot", source_row), ("target", target_row)):
            if str(row["institution_id"]) != institution_id:
                conflicts.append(
                    f"{person_id}: {label} institution is {row['institution_id']!r}, expected {institution_id!r}"
                )
        if _fold(source_row["display_name"]) != _fold(target_row["display_name"]):
            conflicts.append(
                f"{person_id}: canonical name differs ({source_row['display_name']!r} vs {target_row['display_name']!r})"
            )
        source_orcids = _orcid_values(source_row)
        target_orcids = _orcid_values(target_row)
        if source_orcids and target_orcids and source_orcids != target_orcids:
            conflicts.append(
                f"{person_id}: ORCID differs ({sorted(source_orcids)} vs {sorted(target_orcids)})"
            )
        if "membership_status" in target_row.keys() and _fold(
            target_row["membership_status"] or "active"
        ) != "active":
            conflicts.append(f"{person_id}: target canonical membership is not active")
    if _table_exists(target, "pi_identity_aliases"):
        aliases = list(
            _rows_by_values(target, "pi_identity_aliases", "alias_person_id", person_ids)
        )
        conflicts.extend(
            f"{row['alias_person_id']}: target treats allowlisted ID as alias of {row['canonical_person_id']}"
            for row in aliases
        )
    return conflicts


def _vector_payload(row: sqlite3.Row, label: str) -> tuple[dict[str, float], str]:
    payload = _loads(row["vector_json"], None)
    if not isinstance(payload, dict):
        raise PromotionError(f"{label}: vector_json is not an object")
    vector: dict[str, float] = {}
    for key, raw_value in payload.items():
        term = str(key)
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise PromotionError(f"{label}: nonnumeric vector value for {term!r}") from exc
        if not term or not math.isfinite(value):
            raise PromotionError(f"{label}: invalid vector feature {term!r}")
        vector[term] = value
    if len(vector) != int(row["feature_count"]):
        raise PromotionError(f"{label}: feature_count does not match vector_json")
    if len(vector) > int(row["feature_limit"]):
        raise PromotionError(f"{label}: feature_count exceeds feature_limit")
    canonical = _canonical_json({key: vector[key] for key in sorted(vector)})
    vector_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if vector_hash != str(row["vector_hash"]):
        raise PromotionError(f"{label}: vector_hash does not match vector_json")
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if vector and not math.isclose(norm, 1.0, rel_tol=1e-7, abs_tol=1e-7):
        raise PromotionError(f"{label}: non-empty vector is not L2 normalized")
    return vector, vector_hash


def _dependency_hash(
    encoder_id: str,
    feature_limit: int,
    dependencies: list[dict[str, str]],
) -> str:
    payload = {
        "encoder_id": encoder_id,
        "feature_limit": int(feature_limit),
        "paper_representation": PAPER_REPRESENTATION,
        "works": sorted(
            dependencies,
            key=lambda item: (item["openalex_work_id"], item["vector_hash"]),
        ),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _collect_scope(
    source: sqlite3.Connection, person_ids: Sequence[str]
) -> dict[str, Any]:
    fingerprint_rows = list(
        _rows_by_values(
            source, "official_publication_fingerprints", "person_id", person_ids
        )
    )
    fingerprint_ids = {str(row["fingerprint_id"]) for row in fingerprint_rows}
    link_rows = list(
        _rows_by_values(source, "openalex_author_links", "person_id", person_ids)
    )
    all_person_work_rows = list(
        _rows_by_values(source, "openalex_person_works", "person_id", person_ids)
    )
    person_work_rows = [
        row
        for row in all_person_work_rows
        if _fold(row["relationship_status"]) in PROMOTABLE_RELATIONSHIP_STATUSES
    ]
    current_work_ids = {
        str(row["openalex_work_id"])
        for row in person_work_rows
    }
    openalex_run_ids = {
        str(row["last_sync_run_id"])
        for row in link_rows
        if row["last_sync_run_id"]
    }
    openalex_run_ids.update(
        str(row["last_seen_run_id"])
        for row in person_work_rows
        if row["last_seen_run_id"]
    )
    state_rows = list(
        _rows_by_values(
            source, "official_publication_refresh_state", "person_id", person_ids
        )
    )
    claim_rows = list(
        _rows_by_values(
            source, "official_publication_source_claims", "person_id", person_ids
        )
    )
    publication_run_ids = {
        str(row["last_run_id"]) for row in state_rows if row["last_run_id"]
    }
    publication_run_ids.update(
        str(row["last_seen_run_id"]) for row in claim_rows if row["last_seen_run_id"]
    )
    return {
        "person_ids": set(person_ids),
        "fingerprint_ids": fingerprint_ids,
        "link_rows": link_rows,
        "person_work_rows": person_work_rows,
        "excluded_person_work_rows": [
            row
            for row in all_person_work_rows
            if _fold(row["relationship_status"])
            not in PROMOTABLE_RELATIONSHIP_STATUSES
        ],
        # The promotion payload is the accepted active graph, never the pilot's
        # orphan or historical Work inventory.
        "all_work_ids": current_work_ids,
        "current_work_ids": current_work_ids,
        "openalex_run_ids": openalex_run_ids,
        "publication_run_ids": publication_run_ids,
        "source_rows": {
            "official_publication_fingerprints": fingerprint_rows,
            "official_publication_refresh_state": state_rows,
            "official_publication_source_claims": claim_rows,
        },
    }


def _successful_run_for_person(run: sqlite3.Row, person_id: str) -> bool:
    status = _fold(run["status"])
    if status == "success":
        return True
    if status not in {"partial", "partial_success"}:
        return False
    person = _run_person_result(run, person_id)
    return bool(person and _fold(person.get("status")) == "resolved")


def _run_person_result(run: sqlite3.Row, person_id: str) -> dict[str, Any] | None:
    metrics = _loads(run["metrics_json"], {})
    people = metrics.get("people") if isinstance(metrics, dict) else None
    if not isinstance(people, list):
        return None
    return next(
        (
            dict(item)
            for item in people
            if isinstance(item, dict)
            and str(item.get("person_id") or "") == person_id
        ),
        None,
    )


def _validate_source_completion(
    source: sqlite3.Connection,
    scope: Mapping[str, Any],
    institution_id: str,
) -> dict[str, Any]:
    people = set(scope["person_ids"])
    errors: list[str] = []
    fingerprint_people = {
        str(row["person_id"])
        for row in scope["source_rows"]["official_publication_fingerprints"]
    }
    if fingerprint_people != people:
        missing = sorted(people - fingerprint_people)
        errors.append(f"People without official publication identity evidence: {missing[:10]}")

    links = {str(row["person_id"]): row for row in scope["link_rows"]}
    person_work_rows: list[sqlite3.Row] = list(scope["person_work_rows"])
    reviewed_work_only_people: dict[str, list[sqlite3.Row]] = defaultdict(list)
    work_only_review_by_person: dict[str, dict[str, Any]] = {}
    current_rows_by_person: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in person_work_rows:
        person_id = str(row["person_id"])
        current_rows_by_person[person_id].append(row)
        payload = _loads(row["record_json"], {})
        evidence = payload.get("relationship_evidence") if isinstance(payload, dict) else None
        if not isinstance(evidence, dict):
            continue
        if evidence.get("relationship_method") != "reviewed_official_evidence_only":
            continue
        reviewed_work_only_people[person_id].append(row)
        reviewed = evidence.get("reviewed_identity")
        if not isinstance(reviewed, dict):
            errors.append(f"{person_id}: official-work-only relationship has no review audit")
            continue
        previous_review = work_only_review_by_person.setdefault(person_id, reviewed)
        if _canonical_json(previous_review) != _canonical_json(reviewed):
            errors.append(
                f"{person_id}: official-work-only relationships use inconsistent reviews"
            )
        if (
            row["openalex_author_id"] is not None
            or evidence.get("identity_status") != "pending"
            or evidence.get("coverage_limit") != "reviewed_official_evidence_only"
            or reviewed.get("reviewed") is not True
            or reviewed.get("audit_type") != "reviewed_openalex_identity_manifest"
            or reviewed.get("sync_mode") != "official_evidence_only"
            or reviewed.get("primary_openalex_author_id") not in (None, "")
            or reviewed.get("confirmed_openalex_author_ids") not in ([], ())
            or re.fullmatch(
                r"[0-9a-f]{64}", str(reviewed.get("manifest_sha256") or ""), flags=re.I
            )
            is None
        ):
            errors.append(f"{person_id}: invalid reviewed identity-pending Work contract")
    people_without_active_works = sorted(people - set(current_rows_by_person))
    if people_without_active_works:
        errors.append(
            "People without active reviewed OpenAlex Works: "
            f"{people_without_active_works[:10]}"
        )
    for person_id, rows in reviewed_work_only_people.items():
        if len(rows) != len(current_rows_by_person[person_id]):
            errors.append(
                f"{person_id}: identity-pending PI has unaudited current Work relationships"
            )
    missing_links = sorted(people - set(links) - set(reviewed_work_only_people))
    if missing_links:
        errors.append(f"People without OpenAlex author links: {missing_links[:10]}")
    run_ids = {str(row["last_sync_run_id"]) for row in links.values() if row["last_sync_run_id"]}
    run_ids.update(
        str(row["last_seen_run_id"])
        for rows in reviewed_work_only_people.values()
        for row in rows
        if row["last_seen_run_id"]
    )
    runs = {
        str(row["run_id"]): row
        for row in _rows_by_values(source, "openalex_sync_runs", "run_id", run_ids)
    }
    allowed_author_ids: dict[str, set[str]] = {}
    reviewed_link_audits: dict[str, dict[str, Any]] = {}
    author_owners: dict[str, str] = {}
    for person_id, link in sorted(links.items()):
        if str(link["institution_id"]) != institution_id:
            errors.append(f"{person_id}: OpenAlex link institution mismatch")
        if _fold(link["link_status"]) != "confirmed":
            errors.append(f"{person_id}: OpenAlex link is not confirmed")
        if not re.fullmatch(r"A\d+", str(link["openalex_author_id"] or "")):
            errors.append(f"{person_id}: invalid OpenAlex Author ID")
        allowed, reviewed, review_errors = _reviewed_link_contract(
            link,
            person_id=person_id,
            institution_id=institution_id,
        )
        errors.extend(review_errors)
        allowed_author_ids[person_id] = allowed
        reviewed_link_audits[person_id] = reviewed
        for author_id in sorted(allowed):
            owner = author_owners.setdefault(author_id, person_id)
            if owner != person_id:
                errors.append(
                    f"Reviewed OpenAlex Author {author_id} is assigned to both {owner} and {person_id}"
                )
        if not link["last_successful_sync_at"] or not link["last_full_sync_at"]:
            errors.append(f"{person_id}: no successful full OpenAlex baseline watermark")
        run_id = str(link["last_sync_run_id"] or "")
        run = runs.get(run_id)
        if not run or not _successful_run_for_person(run, person_id):
            errors.append(f"{person_id}: last OpenAlex sync run is missing or unsuccessful")
            continue
        person_result = _run_person_result(run, person_id) or {}
        if person_result.get("resolution") != "reviewed_openalex_identity_manifest_v1":
            errors.append(f"{person_id}: successful sync did not use the reviewed identity")
        snapshot_audit = person_result.get("snapshot_audit")
        authorship_audit = (
            snapshot_audit.get("authorship_validation")
            if isinstance(snapshot_audit, dict)
            else None
        )
        if (
            not isinstance(authorship_audit, dict)
            or set(authorship_audit) != allowed
            or int(snapshot_audit.get("selected_union_work_count") or -1)
            != len(current_rows_by_person.get(person_id, []))
        ):
            errors.append(f"{person_id}: reviewed authorship snapshot audit is missing or stale")
        policy = reviewed.get("work_policy") if isinstance(reviewed, dict) else None
        if isinstance(policy, dict):
            policy_audit = (
                snapshot_audit.get("reviewed_work_policy")
                if isinstance(snapshot_audit, dict)
                else None
            )
            expected_hash = reviewed.get("work_policy_sha256")
            if (
                not isinstance(policy_audit, dict)
                or policy_audit.get("policy_sha256") != expected_hash
                or int(policy_audit.get("selected_work_count") or -1)
                != len(current_rows_by_person.get(person_id, []))
            ):
                errors.append(
                    f"{person_id}: reviewed Work-policy sync audit is missing or stale"
                )
            if policy.get("mode") == "exact_work_allowlist":
                expected_work_ids = {
                    str(work_id) for work_id in policy.get("work_ids") or []
                }
                current_work_ids = {
                    str(row["openalex_work_id"])
                    for row in current_rows_by_person.get(person_id, [])
                }
                if current_work_ids != expected_work_ids:
                    errors.append(
                        f"{person_id}: current Works differ from the reviewed exact Work allowlist"
                    )

    for person_id, rows in sorted(reviewed_work_only_people.items()):
        run_id = str(max(rows, key=lambda row: str(row["last_seen_at"]))["last_seen_run_id"])
        run = runs.get(run_id)
        if not run or not _successful_run_for_person(run, person_id):
            errors.append(
                f"{person_id}: reviewed official-work-only sync run is missing or unsuccessful"
            )
            continue
        person_result = _run_person_result(run, person_id) or {}
        if (
            person_result.get("resolution") != "reviewed_openalex_identity_manifest_v1"
            or person_result.get("sync_mode") != "official_evidence_only"
            or person_result.get("identity_status") != "pending"
            or int(person_result.get("works_selected") or 0) != len(rows)
        ):
            errors.append(f"{person_id}: official-work-only sync audit is missing or stale")

    current_by_person: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in person_work_rows:
        person_id = str(row["person_id"])
        if str(row["institution_id"]) != institution_id:
            errors.append(f"{person_id}: OpenAlex person-work institution mismatch")
        link = links.get(person_id)
        relationship_author_id = _normalized_openalex_id(row["openalex_author_id"], "A")
        if link and relationship_author_id not in allowed_author_ids.get(person_id, set()):
            errors.append(
                f"{person_id}: person-work Author ID is outside the reviewed profile set"
            )
        if not link and person_id not in reviewed_work_only_people:
            errors.append(f"{person_id}: person-work relationship has no reviewed identity")
        current_by_person[person_id].append(row)

    works = {
        str(row["openalex_work_id"]): row
        for row in _rows_by_values(
            source, "openalex_works", "openalex_work_id", scope["current_work_ids"]
        )
    }
    missing_works = sorted(set(scope["current_work_ids"]) - set(works))
    if missing_works:
        errors.append(f"Current relationships with missing OpenAlex Works: {missing_works[:10]}")
    for person_id, relationships in sorted(current_by_person.items()):
        reviewed_work_only = work_only_review_by_person.get(person_id)
        official_ids: set[str] = set()
        official_dois: set[str] = set()
        if reviewed_work_only:
            for reference in reviewed_work_only.get("official_works") or []:
                if not isinstance(reference, dict):
                    continue
                work_id = _normalized_openalex_id(reference.get("openalex_work_id"), "W")
                doi = _normalized_doi(reference.get("doi"))
                if work_id:
                    official_ids.add(work_id)
                if doi:
                    official_dois.add(doi)
        for relationship in relationships:
            work_id = str(relationship["openalex_work_id"])
            work = works.get(work_id)
            if work is None:
                continue
            relationship_author_id = _normalized_openalex_id(
                relationship["openalex_author_id"], "A"
            )
            if relationship_author_id:
                if relationship_author_id not in _work_authorship_ids(work):
                    errors.append(
                        f"{person_id}/{work_id}: per-Work Author provenance is absent "
                        "from the stored OpenAlex authorship"
                    )
            elif reviewed_work_only:
                work_doi = _normalized_doi(work["doi"])
                if work_id not in official_ids and (
                    not work_doi or work_doi not in official_dois
                ):
                    errors.append(
                        f"{person_id}/{work_id}: identity-pending Work is not in the "
                        "reviewed official evidence list"
                    )
            else:
                errors.append(f"{person_id}/{work_id}: current Work has no Author provenance")
    vectors = {
        str(row["openalex_work_id"]): row
        for row in _rows_by_values(
            source,
            "openalex_work_vectors",
            "openalex_work_id",
            scope["current_work_ids"],
            extra_where="representation=?",
            extra_params=(PAPER_REPRESENTATION,),
        )
    }
    missing_vectors = sorted(set(scope["current_work_ids"]) - set(vectors))
    if missing_vectors:
        errors.append(f"Current OpenAlex Works without paper vectors: {missing_vectors[:10]}")
    valid_vector_hashes: dict[str, str] = {}
    for work_id in sorted(set(works) & set(vectors)):
        work = works[work_id]
        vector = vectors[work_id]
        if str(vector["source_text_hash"] or "") != str(work["vector_text_hash"] or ""):
            errors.append(f"{work_id}: paper vector is stale relative to OpenAlex Work text")
            continue
        if (
            str(vector["encoder_id"]) != EXPECTED_ENCODER
            or int(vector["feature_limit"]) != EXPECTED_FEATURE_LIMIT
        ):
            errors.append(f"{work_id}: paper vector uses an unaccepted encoder contract")
            continue
        try:
            _payload, vector_hash = _vector_payload(vector, f"paper {work_id}")
            valid_vector_hashes[work_id] = vector_hash
        except PromotionError as exc:
            errors.append(str(exc))

    careers = {
        str(row["person_id"]): row
        for row in _rows_by_values(
            source,
            "pi_career_vectors",
            "person_id",
            people,
            extra_where="representation=?",
            extra_params=(CAREER_REPRESENTATION,),
        )
    }
    missing_careers = sorted(people - set(careers))
    if missing_careers:
        errors.append(f"People without career vectors: {missing_careers[:10]}")
    for person_id, career in sorted(careers.items()):
        relationships = current_by_person.get(person_id, [])
        work_ids = sorted({str(row["openalex_work_id"]) for row in relationships})
        if (
            str(career["encoder_id"]) != EXPECTED_ENCODER
            or int(career["feature_limit"]) != EXPECTED_FEATURE_LIMIT
        ):
            errors.append(f"{person_id}: career vector uses an unaccepted encoder contract")
            continue
        if int(career["work_count"]) != len(work_ids):
            errors.append(f"{person_id}: career work_count does not match current Work inventory")
        nonempty = sum(
            int(vectors[work_id]["feature_count"]) > 0
            for work_id in work_ids
            if work_id in vectors
        )
        if int(career["nonempty_work_count"]) != nonempty:
            errors.append(f"{person_id}: career nonempty_work_count is stale")
        try:
            _vector_payload(career, f"career {person_id}")
        except PromotionError as exc:
            errors.append(str(exc))
        if all(work_id in valid_vector_hashes for work_id in work_ids):
            dependencies = [
                {
                    "openalex_work_id": work_id,
                    "source_text_hash": str(vectors[work_id]["source_text_hash"]),
                    "vector_hash": valid_vector_hashes[work_id],
                }
                for work_id in work_ids
            ]
            expected = _dependency_hash(
                str(career["encoder_id"]), int(career["feature_limit"]), dependencies
            )
            if str(career["dependency_hash"]) != expected:
                errors.append(f"{person_id}: career dependency_hash is stale")

    if _table_exists(source, "openalex_sync_runs"):
        running = list(
            _rows_by_values(
                source,
                "openalex_sync_runs",
                "run_id",
                scope["openalex_run_ids"],
                extra_where="status='running'",
            )
        )[:5]
        if running:
            errors.append(f"Pilot still has running OpenAlex sync runs: {[row[0] for row in running]}")
    unfinished_vector_jobs: list[int] = []
    if _table_exists(source, "vector_dirty_queue"):
        for row in _relevant_queue_rows(source, scope, statuses=("pending", "processing")):
            if row["entity_kind"] in {PAPER_QUEUE_KIND, CAREER_QUEUE_KIND}:
                unfinished_vector_jobs.append(int(row["queue_id"]))
    if unfinished_vector_jobs:
        errors.append(f"Pilot still has unfinished vector jobs: {unfinished_vector_jobs[:10]}")
    return {
        "errors": errors,
        "counts": {
            "people": len(people),
            "official_fingerprints": len(scope["fingerprint_ids"]),
            "confirmed_author_links": sum(
                _fold(row["link_status"]) == "confirmed" for row in links.values()
            ),
            "reviewed_author_links": len(reviewed_link_audits),
            "identity_pending_work_only_people": len(reviewed_work_only_people),
            "promotable_person_work_relationships": len(person_work_rows),
            "excluded_historical_person_work_relationships": len(
                scope["excluded_person_work_rows"]
            ),
            "current_person_work_relationships": sum(map(len, current_by_person.values())),
            "distinct_current_works": len(scope["current_work_ids"]),
            "paper_vectors": len(vectors),
            "career_vectors": len(careers),
        },
    }


def _row_key(row: sqlite3.Row, columns: Sequence[str]) -> tuple[Any, ...]:
    return tuple(row[column] for column in columns)


def _reviewed_enrichment_delta(
    target: sqlite3.Connection,
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    """Describe an append-only change to the cohort's active PI-Work graph."""

    people = set(scope["person_ids"])
    source_rows = {
        (str(row["person_id"]), str(row["openalex_work_id"])): row
        for row in scope["person_work_rows"]
    }
    target_rows = {
        (str(row["person_id"]), str(row["openalex_work_id"])): row
        for row in _rows_by_values(
            target, "openalex_person_works", "person_id", people
        )
    } if _table_exists(target, "openalex_person_works") else {}
    target_current = {
        key
        for key, row in target_rows.items()
        if _fold(row["relationship_status"]) in PROMOTABLE_RELATIONSHIP_STATUSES
    }
    source_current = set(source_rows)
    added = sorted(source_current - target_current)
    removed = sorted(target_current - source_current)
    reactivated = sorted(key for key in added if key in target_rows)
    return {
        "mode": "append_only_reviewed_enrichment",
        "source_current_relationships": len(source_current),
        "target_current_relationships": len(target_current),
        "added_relationships": [list(key) for key in added],
        "removed_relationships": [list(key) for key in removed],
        "reactivated_relationships": [list(key) for key in reactivated],
        "affected_person_ids": sorted({person_id for person_id, _work_id in added}),
        "append_only": not removed,
    }


def _validate_target_conflicts(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    scope: Mapping[str, Any],
    institution_id: str,
    *,
    allow_reviewed_enrichment_update: bool = False,
) -> list[str]:
    conflicts: list[str] = []
    people = set(scope["person_ids"])
    update_delta = _reviewed_enrichment_delta(target, scope)
    affected_people = (
        set(update_delta["affected_person_ids"])
        if allow_reviewed_enrichment_update
        else set()
    )
    if allow_reviewed_enrichment_update and update_delta["removed_relationships"]:
        conflicts.append(
            "Reviewed enrichment update is not append-only; target active relationships "
            f"would be removed: {update_delta['removed_relationships'][:10]}"
        )
    source_links = {str(row["person_id"]): row for row in scope["link_rows"]}
    if _table_exists(target, "openalex_author_links"):
        for row in _rows_by_values(target, "openalex_author_links", "person_id", people):
            source_row = source_links.get(str(row["person_id"]))
            if source_row and (
                str(row["openalex_author_id"]) != str(source_row["openalex_author_id"])
                or str(row["institution_id"]) != institution_id
            ):
                conflicts.append(f"{row['person_id']}: target has a different OpenAlex Author link")
        source_author_to_person: dict[str, str] = {}
        for source_row in source_links.values():
            person_id = str(source_row["person_id"])
            for author_id in _persisted_link_author_ids(source_row):
                previous = source_author_to_person.setdefault(author_id, person_id)
                if previous != person_id:
                    conflicts.append(
                        f"Source reviewed Author {author_id} belongs to both {previous} and {person_id}"
                    )
        for row in target.execute(
            "SELECT * FROM openalex_author_links "
            "WHERE institution_id=? AND link_status='confirmed'",
            (institution_id,),
        ):
            target_person = str(row["person_id"])
            for author_id in _persisted_link_author_ids(row):
                expected_person = source_author_to_person.get(author_id)
                if expected_person is not None and target_person != expected_person:
                    conflicts.append(
                        f"OpenAlex Author {author_id} is already confirmed for {target_person}"
                    )

    source_person_works = {
        (str(row["person_id"]), str(row["openalex_work_id"])): row
        for row in scope["person_work_rows"]
    }
    if _table_exists(target, "openalex_person_works"):
        for row in _rows_by_values(target, "openalex_person_works", "person_id", people):
            key = (str(row["person_id"]), str(row["openalex_work_id"]))
            source_row = source_person_works.get(key)
            if source_row is None:
                conflicts.append(f"Target has an extra historical person-work relation: {key}")
            elif (
                str(row["openalex_author_id"]) != str(source_row["openalex_author_id"])
                or str(row["institution_id"]) != institution_id
            ):
                conflicts.append(f"Target person-work identity conflict: {key}")

    source_works = {
        str(row["openalex_work_id"]): row
        for row in _rows_by_values(
            source, "openalex_works", "openalex_work_id", scope["all_work_ids"]
        )
    }
    if _table_exists(target, "openalex_works"):
        for row in _rows_by_values(
            target, "openalex_works", "openalex_work_id", scope["all_work_ids"]
        ):
            source_row = source_works[str(row["openalex_work_id"])]
            if str(row["vector_text_hash"] or "") != str(
                source_row["vector_text_hash"] or ""
            ):
                conflicts.append(f"{row['openalex_work_id']}: target Work text hash differs")
            target_doi = _normalized_doi(row["doi"])
            source_doi = _normalized_doi(source_row["doi"])
            if target_doi and source_doi and target_doi != source_doi:
                conflicts.append(f"{row['openalex_work_id']}: target Work DOI differs")

    source_vectors = {
        (str(row["openalex_work_id"]), str(row["representation"])): row
        for row in _rows_by_values(
            source,
            "openalex_work_vectors",
            "openalex_work_id",
            scope["all_work_ids"],
            extra_where="representation=?",
            extra_params=(PAPER_REPRESENTATION,),
        )
    }
    if _table_exists(target, "openalex_work_vectors"):
        for row in _rows_by_values(
            target,
            "openalex_work_vectors",
            "openalex_work_id",
            scope["all_work_ids"],
            extra_where="representation=?",
            extra_params=(PAPER_REPRESENTATION,),
        ):
            key = (str(row["openalex_work_id"]), str(row["representation"]))
            source_row = source_vectors.get(key)
            if source_row and (
                str(row["vector_hash"]) != str(source_row["vector_hash"])
                or str(row["source_text_hash"]) != str(source_row["source_text_hash"])
                or str(row["encoder_id"]) != str(source_row["encoder_id"])
            ):
                conflicts.append(f"{key[0]}: target paper vector differs")

    source_careers = {
        (str(row["person_id"]), str(row["representation"])): row
        for row in _rows_by_values(
            source,
            "pi_career_vectors",
            "person_id",
            people,
            extra_where="representation=?",
            extra_params=(CAREER_REPRESENTATION,),
        )
    }
    if _table_exists(target, "pi_career_vectors"):
        for row in _rows_by_values(
            target,
            "pi_career_vectors",
            "person_id",
            people,
            extra_where="representation=?",
            extra_params=(CAREER_REPRESENTATION,),
        ):
            key = (str(row["person_id"]), str(row["representation"]))
            source_row = source_careers.get(key)
            if source_row and (
                str(row["vector_hash"]) != str(source_row["vector_hash"])
                or str(row["dependency_hash"]) != str(source_row["dependency_hash"])
                or str(row["encoder_id"]) != str(source_row["encoder_id"])
            ) and not (
                allow_reviewed_enrichment_update and key[0] in affected_people
            ):
                conflicts.append(f"{key[0]}: target career vector differs")

    if _table_exists(target, "official_publication_fingerprints"):
        source_fingerprints = {
            str(row["fingerprint_id"]): row
            for row in scope["source_rows"]["official_publication_fingerprints"]
        }
        for row in _rows_by_values(
            target,
            "official_publication_fingerprints",
            "fingerprint_id",
            scope["fingerprint_ids"],
        ):
            source_row = source_fingerprints[str(row["fingerprint_id"])]
            if (
                str(row["person_id"]) != str(source_row["person_id"])
                or str(row["institution_id"]) != institution_id
            ):
                conflicts.append(f"{row['fingerprint_id']}: publication fingerprint identity conflict")

    if _table_exists(target, "vector_dirty_queue"):
        processing = list(_relevant_queue_rows(target, scope, statuses=("processing",)))
        if processing:
            conflicts.append(
                "Target has leased jobs for promoted entities: "
                + str([int(row["queue_id"]) for row in processing[:10]])
            )

    for table, ids in (
        ("openalex_sync_runs", scope["openalex_run_ids"]),
        ("publication_refresh_runs", scope["publication_run_ids"]),
    ):
        if not ids or not _table_exists(target, table):
            continue
        key = "run_id"
        source_rows = {
            str(row[key]): row for row in _rows_by_values(source, table, key, ids)
        }
        target_columns = set(_columns(target, table))
        for row in _rows_by_values(target, table, key, ids):
            source_row = source_rows.get(str(row[key]))
            if source_row is None:
                continue
            common = [column for column in source_row.keys() if column in target_columns]
            if any(row[column] != source_row[column] for column in common):
                conflicts.append(f"{table} run ID collision: {row[key]}")
    return conflicts


def _relevant_queue_rows(
    connection: sqlite3.Connection,
    scope: Mapping[str, Any],
    *,
    statuses: Sequence[str],
) -> Iterator[sqlite3.Row]:
    if not _table_exists(connection, "vector_dirty_queue"):
        return
    status_placeholders = ",".join("?" for _ in statuses)
    people = set(scope["person_ids"])
    works = set(scope["all_work_ids"])
    for chunk in _chunks(people):
        placeholders = ",".join("?" for _ in chunk)
        yield from connection.execute(
            "SELECT * FROM vector_dirty_queue "
            f"WHERE status IN ({status_placeholders}) AND ("
            f"(entity_kind IN (?, ?) AND entity_id IN ({placeholders})) OR "
            f"person_id IN ({placeholders}))",
            [*statuses, CAREER_QUEUE_KIND, OPENALEX_QUEUE_KIND, *chunk, *chunk],
        )
    for chunk in _chunks(works):
        placeholders = ",".join("?" for _ in chunk)
        yield from connection.execute(
            "SELECT * FROM vector_dirty_queue "
            f"WHERE status IN ({status_placeholders}) "
            f"AND entity_kind=? AND entity_id IN ({placeholders})",
            [*statuses, PAPER_QUEUE_KIND, *chunk],
        )


def _identity_pending_work_only_people(scope: Mapping[str, Any]) -> set[str]:
    people: set[str] = set()
    for row in scope["person_work_rows"]:
        payload = _loads(row["record_json"], {})
        evidence = payload.get("relationship_evidence") if isinstance(payload, dict) else None
        if (
            isinstance(evidence, dict)
            and evidence.get("relationship_method") == "reviewed_official_evidence_only"
            and evidence.get("identity_status") == "pending"
            and row["openalex_author_id"] is None
        ):
            people.add(str(row["person_id"]))
    return people


def _selected_rows(
    source: sqlite3.Connection, table: str, scope: Mapping[str, Any]
) -> list[sqlite3.Row]:
    people = scope["person_ids"]
    if table in {
        "official_publication_fingerprints",
        "official_publication_refresh_state",
        "official_publication_source_claims",
        "openalex_author_links",
    }:
        return list(_rows_by_values(source, table, "person_id", people))
    if table == "openalex_person_works":
        return list(scope["person_work_rows"])
    if table == "openalex_works":
        return list(_rows_by_values(source, table, "openalex_work_id", scope["all_work_ids"]))
    if table == "openalex_work_vectors":
        return list(
            _rows_by_values(
                source,
                table,
                "openalex_work_id",
                scope["all_work_ids"],
                extra_where="representation=?",
                extra_params=(PAPER_REPRESENTATION,),
            )
        )
    if table == "pi_career_vectors":
        return list(
            _rows_by_values(
                source,
                table,
                "person_id",
                people,
                extra_where="representation=?",
                extra_params=(CAREER_REPRESENTATION,),
            )
        )
    if table == "openalex_sync_runs":
        return list(_rows_by_values(source, table, "run_id", scope["openalex_run_ids"]))
    if table == "publication_refresh_runs":
        return list(
            _rows_by_values(source, table, "run_id", scope["publication_run_ids"])
        )
    return []


def _estimate_payload(source: sqlite3.Connection, scope: Mapping[str, Any]) -> dict[str, Any]:
    table_bytes: dict[str, int] = {}
    table_rows: dict[str, int] = {}
    for table in MIGRATABLE_TABLES:
        if table == "vector_dirty_queue":
            rows = list(_relevant_queue_rows(source, scope, statuses=("completed",)))
            pending_people = _identity_pending_work_only_people(scope)
            rows.extend(
                row
                for row in _relevant_queue_rows(source, scope, statuses=("pending",))
                if row["entity_kind"] == OPENALEX_QUEUE_KIND
                and str(row["person_id"] or row["entity_id"] or "") in pending_people
            )
        else:
            rows = _selected_rows(source, table, scope)
        table_rows[table] = len(rows)
        table_bytes[table] = sum(
            sum(len(str(value).encode("utf-8")) for value in tuple(row) if value is not None)
            for row in rows
        )
    return {
        "estimated_row_payload_bytes": sum(table_bytes.values()),
        "rows_by_table": table_rows,
        "estimated_bytes_by_table": table_bytes,
    }


def _disk_plan(target_db: Path, backup_db: Path | None, payload_bytes: int) -> dict[str, Any]:
    target_parent = target_db.parent
    target_volume = target_db.anchor.casefold() or str(target_parent.anchor).casefold()
    requirements: dict[str, int] = defaultdict(int)
    requirements[target_volume] += max(2 * int(payload_bytes), HEADROOM_BYTES)
    locations: dict[str, Path] = {target_volume: target_parent}
    if backup_db is not None:
        backup_parent = backup_db.parent
        while not backup_parent.exists():
            backup_parent = backup_parent.parent
        backup_volume = backup_db.anchor.casefold() or str(backup_parent.anchor).casefold()
        requirements[backup_volume] += target_db.stat().st_size + HEADROOM_BYTES
        locations[backup_volume] = backup_parent
    volumes: dict[str, Any] = {}
    enough = True
    for volume, required in requirements.items():
        usage = shutil.disk_usage(locations[volume])
        available = int(usage.free)
        volumes[volume or str(locations[volume])] = {
            "available_bytes": available,
            "required_bytes": int(required),
            "headroom_after_bytes": available - int(required),
            "enough": available >= int(required),
        }
        enough = enough and available >= int(required)
    return {"enough": enough, "volumes": volumes}


def _preflight_connections(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    source_path: Path,
    target_path: Path,
    backup_path: Path | None,
    person_ids: Sequence[str],
    institution_id: str,
    allow_reviewed_enrichment_update: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_quick = [str(row[0]) for row in source.execute("PRAGMA quick_check")]
    target_quick = [str(row[0]) for row in target.execute("PRAGMA quick_check")]
    schema = _schema_plan(source, target)
    scope = _collect_scope(source, person_ids)
    identity_conflicts = _validate_identity(
        source, target, person_ids, institution_id
    )
    completion = _validate_source_completion(source, scope, institution_id)
    target_baseline = None
    target_baseline_errors: list[str] = []
    if allow_reviewed_enrichment_update:
        target_scope = _collect_scope(target, person_ids)
        target_baseline = _validate_source_completion(
            target, target_scope, institution_id
        )
        target_baseline_errors = [
            f"Target baseline: {error}" for error in target_baseline["errors"]
        ]
    target_conflicts = _validate_target_conflicts(
        source,
        target,
        scope,
        institution_id,
        allow_reviewed_enrichment_update=allow_reviewed_enrichment_update,
    )
    update_delta = _reviewed_enrichment_delta(target, scope)
    payload = _estimate_payload(source, scope)
    disk = _disk_plan(
        target_path, backup_path, int(payload["estimated_row_payload_bytes"])
    )
    errors = [
        *( [] if source_quick == ["ok"] else [f"Pilot quick_check failed: {source_quick}"] ),
        *( [] if target_quick == ["ok"] else [f"Target quick_check failed: {target_quick}"] ),
        *schema["conflicts"],
        *identity_conflicts,
        *completion["errors"],
        *target_baseline_errors,
        *target_conflicts,
    ]
    report = {
        "status": "ready" if not errors else "blocked",
        "source_db": str(source_path),
        "target_db": str(target_path),
        "backup_db": str(backup_path) if backup_path else None,
        "institution_id": institution_id,
        "allowlist_count": len(person_ids),
        "selected_person_ids": list(person_ids),
        "quick_check": {"pilot": source_quick, "target": target_quick},
        "schema_migration": {
            "summary": schema["summary"],
            "actions": [
                {key: value for key, value in action.items() if key != "sql"}
                for action in schema["actions"]
            ],
        },
        "completion": completion["counts"],
        "reviewed_enrichment_update": {
            "enabled": bool(allow_reviewed_enrichment_update),
            **update_delta,
            "target_baseline_pass": (
                not target_baseline_errors
                if allow_reviewed_enrichment_update
                else None
            ),
            "target_baseline_completion": (
                target_baseline["counts"] if target_baseline is not None else None
            ),
        },
        "payload": payload,
        "operational_tables_excluded": {
            "openalex_identity_probe_cache": (
                "request/probe cache is not a serving artifact and can contain "
                "non-cohort or negative lookups"
            ),
            "raw_sources": "raw crawl archives remain in the pilot archive",
        },
        "disk": disk,
        "canonical_rows_to_modify": 0,
        "raw_archives_to_copy": 0,
        "errors": errors,
    }
    return report, scope


def preflight_promotion(
    pilot_db: str | Path,
    target_db: str | Path,
    allowlist: str | Path,
    *,
    institution_id: str,
    expected_count: int = 124,
    backup_db: str | Path | None = None,
    allow_reviewed_enrichment_update: bool = False,
) -> dict[str, Any]:
    source_path = _safe_existing_db(pilot_db, "Pilot")
    target_path = _safe_existing_db(target_db, "Target")
    if source_path == target_path:
        raise PromotionError("Pilot and target databases must differ")
    person_ids = _load_allowlist(allowlist, expected_count)
    backup_path = _safe_backup_path(
        backup_db,
        source_db=source_path,
        target_db=target_path,
        required=False,
    )
    source_stat = source_path.stat()
    target_stat = target_path.stat()
    source = _open_read_only(source_path)
    target = _open_read_only(target_path)
    try:
        source.execute("BEGIN")
        target.execute("BEGIN")
        report, _scope = _preflight_connections(
            source,
            target,
            source_path=source_path,
            target_path=target_path,
            backup_path=backup_path,
            person_ids=person_ids,
            institution_id=institution_id,
            allow_reviewed_enrichment_update=allow_reviewed_enrichment_update,
        )
    finally:
        source.rollback()
        target.rollback()
        source.close()
        target.close()
    report["dry_run"] = True
    report["read_only_verified"] = {
        "pilot_unchanged": (
            source_path.stat().st_size == source_stat.st_size
            and source_path.stat().st_mtime_ns == source_stat.st_mtime_ns
        ),
        "target_unchanged": (
            target_path.stat().st_size == target_stat.st_size
            and target_path.stat().st_mtime_ns == target_stat.st_mtime_ns
        ),
    }
    return report


def audit_faculty_enrichment(
    database: str | Path,
    allowlist: str | Path,
    *,
    institution_id: str,
    expected_count: int = 124,
) -> dict[str, Any]:
    """Read-only exact-cohort acceptance without a promotion target."""

    database_path = _safe_existing_db(database, "Faculty enrichment")
    person_ids = _load_allowlist(allowlist, expected_count)
    before = database_path.stat()
    connection = _open_read_only(database_path)
    try:
        connection.execute("BEGIN")
        quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
        scope = _collect_scope(connection, person_ids)
        identity_errors = _validate_identity(
            connection,
            connection,
            person_ids,
            institution_id,
        )
        completion = _validate_source_completion(connection, scope, institution_id)
        errors = [
            *( [] if quick == ["ok"] else [f"Database quick_check failed: {quick}"] ),
            *( [] if not foreign_keys else [f"Database foreign_key_check failed: {foreign_keys[:5]}"] ),
            *identity_errors,
            *completion["errors"],
        ]
    finally:
        connection.rollback()
        connection.close()
    after = database_path.stat()
    checks = {
        "sqlite_integrity": quick == ["ok"] and not foreign_keys,
        "exact_allowlist_count": len(person_ids) == int(expected_count),
        "canonical_identity_scope": not identity_errors,
        "official_publication_evidence_complete": not any(
            "official publication identity evidence" in error for error in errors
        ),
        "reviewed_identity_policy_complete": not any(
            any(
                token in error
                for token in (
                    "human-reviewed",
                    "reviewed OpenAlex",
                    "reviewed identity",
                    "reviewed profile",
                    "Work contract",
                    "Work policy",
                    "field-allowlist",
                    "official-work-only",
                )
            )
            for error in errors
        ),
        "active_work_graph_complete": not any(
            any(
                token in error
                for token in (
                    "relationships with missing",
                    "person-work",
                    "without active reviewed OpenAlex Works",
                )
            )
            for error in errors
        ),
        "per_work_author_provenance_valid": not any(
            "provenance" in error or "Author ID is outside" in error for error in errors
        ),
        "paper_vectors_complete_and_fresh": not any(
            error.startswith("W") and ("paper vector" in error or "vector" in error)
            or "Works without paper vectors" in error
            for error in errors
        ),
        "career_vectors_complete_and_fresh": not any(
            "career" in error or "People without career vectors" in error
            for error in errors
        ),
        "scoped_vector_queue_settled": not any(
            "unfinished vector jobs" in error for error in errors
        ),
    }
    return {
        "status": "pass" if not errors else "blocked",
        "pass": not errors,
        "read_only": True,
        "database": str(database_path),
        "institution_id": institution_id,
        "expected_count": int(expected_count),
        "allowlist_count": len(person_ids),
        "person_ids": person_ids,
        "checks": checks,
        "completion": completion["counts"],
        "scope_exclusions": {
            "historical_person_work_relationships": len(
                scope["excluded_person_work_rows"]
            ),
            "openalex_identity_probe_cache": "not a serving/promotion artifact",
        },
        "quick_check": quick,
        "foreign_key_check": foreign_keys,
        "errors": errors,
        "read_only_verified": (
            before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
        ),
    }


def _insert_rows(
    target: sqlite3.Connection,
    table: str,
    rows: Sequence[sqlite3.Row | Mapping[str, Any]],
    *,
    update_existing: bool,
    omit_columns: Sequence[str] = (),
) -> int:
    if not rows:
        return 0
    target_columns = set(_columns(target, table))
    row_keys = list(rows[0].keys())
    columns = [
        column
        for column in row_keys
        if column in target_columns and column not in set(omit_columns)
    ]
    if not columns:
        return 0
    primary = [column for column in _primary_key(target, table) if column in columns]
    placeholders = ",".join("?" for _ in columns)
    quoted_columns = ",".join(_quote_identifier(column) for column in columns)
    sql = (
        f"INSERT INTO {_quote_identifier(table)} ({quoted_columns}) "
        f"VALUES ({placeholders})"
    )
    if primary:
        conflict = ",".join(_quote_identifier(column) for column in primary)
        updates = [column for column in columns if column not in primary]
        if update_existing and updates:
            assignments = ",".join(
                f"{_quote_identifier(column)}=excluded.{_quote_identifier(column)}"
                for column in updates
            )
            sql += f" ON CONFLICT ({conflict}) DO UPDATE SET {assignments}"
        else:
            sql += f" ON CONFLICT ({conflict}) DO NOTHING"
    target.executemany(
        sql,
        [tuple(row[column] for column in columns) for row in rows],
    )
    return len(rows)


def _insert_completed_queue_rows(
    target: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
) -> int:
    inserted = 0
    target_columns = set(_columns(target, "vector_dirty_queue"))
    for row in rows:
        signature = target.execute(
            "SELECT 1 FROM vector_dirty_queue "
            "WHERE entity_kind=? AND entity_id=? AND status='completed' "
            "AND COALESCE(run_id,'')=COALESCE(?,'') "
            "AND COALESCE(processed_at,'')=COALESCE(?,'') LIMIT 1",
            (row["entity_kind"], row["entity_id"], row["run_id"], row["processed_at"]),
        ).fetchone()
        if signature:
            continue
        payload = dict(row)
        payload.pop("queue_id", None)
        for column in ("claim_token", "claim_owner", "lease_expires_at"):
            if column in target_columns:
                payload[column] = None
        _insert_rows(
            target,
            "vector_dirty_queue",
            [payload],
            update_existing=False,
        )
        inserted += 1
    return inserted


def _insert_pending_identity_refresh_rows(
    target: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
    identity_pending_people: set[str],
) -> int:
    inserted = 0
    target_columns = set(_columns(target, "vector_dirty_queue"))
    for row in rows:
        person_id = str(row["person_id"] or row["entity_id"] or "")
        if (
            row["entity_kind"] != OPENALEX_QUEUE_KIND
            or person_id not in identity_pending_people
        ):
            continue
        signature = target.execute(
            "SELECT 1 FROM vector_dirty_queue "
            "WHERE entity_kind=? AND entity_id=? AND status='pending' "
            "AND COALESCE(person_id,'')=COALESCE(?,'') LIMIT 1",
            (row["entity_kind"], row["entity_id"], row["person_id"]),
        ).fetchone()
        if signature:
            continue
        payload = dict(row)
        payload.pop("queue_id", None)
        payload["status"] = "pending"
        payload["processed_at"] = None
        payload["last_error"] = None
        for column in ("claim_token", "claim_owner", "lease_expires_at"):
            if column in target_columns:
                payload[column] = None
        _insert_rows(target, "vector_dirty_queue", [payload], update_existing=False)
        inserted += 1
    return inserted


def _resolve_target_pending_queue(
    target: sqlite3.Connection,
    scope: Mapping[str, Any],
    promoted_at: str,
    identity_pending_people: set[str],
) -> int:
    rows = list(_relevant_queue_rows(target, scope, statuses=("pending",)))
    count = 0
    for row in rows:
        person_id = str(row["person_id"] or row["entity_id"] or "")
        if (
            row["entity_kind"] == OPENALEX_QUEUE_KIND
            and person_id in identity_pending_people
        ):
            continue
        payload = _loads(row["payload_json"], {})
        if not isinstance(payload, dict):
            payload = {}
        payload["promotion"] = {
            "validated_artifact": True,
            "schema_version": PROMOTION_SCHEMA_VERSION,
        }
        target.execute(
            "UPDATE vector_dirty_queue SET status='completed', claim_token=NULL, "
            "claim_owner=NULL, lease_expires_at=NULL, updated_at=?, processed_at=?, "
            "last_error=NULL, payload_json=? WHERE queue_id=? AND status='pending'",
            (promoted_at, promoted_at, _canonical_json(payload), int(row["queue_id"])),
        )
        count += 1
    return count


def _apply_rows(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    scope: Mapping[str, Any],
    promoted_at: str,
) -> dict[str, int]:
    copied: dict[str, int] = {}
    # Run rows are immutable audit identifiers.  Collision equality was checked
    # during preflight, so existing rows are retained.
    for table in ("publication_refresh_runs", "openalex_sync_runs"):
        copied[table] = _insert_rows(
            target, table, _selected_rows(source, table, scope), update_existing=False
        )
    # Official profile evidence from the pilot may be fresher than the original
    # pool, and is scoped by the explicit person list.
    for table in (
        "official_publication_fingerprints",
        "official_publication_refresh_state",
        "official_publication_source_claims",
        "openalex_author_links",
    ):
        copied[table] = _insert_rows(
            target, table, _selected_rows(source, table, scope), update_existing=True
        )
    # Global Works/vectors are content-address checked.  Retain an already
    # matching global row to avoid regressing metadata collected elsewhere.
    for table in ("openalex_works", "openalex_work_vectors"):
        copied[table] = _insert_rows(
            target, table, _selected_rows(source, table, scope), update_existing=False
        )
    for table in ("openalex_person_works", "pi_career_vectors"):
        copied[table] = _insert_rows(
            target, table, _selected_rows(source, table, scope), update_existing=True
        )
    completed_rows = list(_relevant_queue_rows(source, scope, statuses=("completed",)))
    copied["vector_dirty_queue_completed_inserted"] = _insert_completed_queue_rows(
        target, completed_rows
    )
    identity_pending_people = _identity_pending_work_only_people(scope)
    pending_rows = list(_relevant_queue_rows(source, scope, statuses=("pending",)))
    copied["identity_refresh_pending_inserted"] = _insert_pending_identity_refresh_rows(
        target,
        pending_rows,
        identity_pending_people,
    )
    copied["target_pending_jobs_resolved"] = _resolve_target_pending_queue(
        target,
        scope,
        promoted_at,
        identity_pending_people,
    )
    target.execute(
        "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("faculty_pilot_promotion_schema", PROMOTION_SCHEMA_VERSION),
    )
    return copied


def _verify_promoted_scope(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    scope: Mapping[str, Any],
    institution_id: str,
) -> dict[str, Any]:
    target_scope = _collect_scope(target, sorted(scope["person_ids"]))
    completion = _validate_source_completion(target, target_scope, institution_id)
    if completion["errors"]:
        raise PromotionError(f"Post-write completion validation failed: {completion['errors'][:10]}")
    conflicts = _validate_target_conflicts(source, target, scope, institution_id)
    if conflicts:
        raise PromotionError(f"Post-write conflict validation failed: {conflicts[:10]}")
    foreign_keys = [list(row) for row in target.execute("PRAGMA foreign_key_check")]
    quick = [str(row[0]) for row in target.execute("PRAGMA quick_check")]
    if foreign_keys or quick != ["ok"]:
        raise PromotionError(
            f"Post-write SQLite validation failed: quick={quick}, foreign_keys={foreign_keys[:5]}"
        )
    return {
        "completion": completion["counts"],
        "quick_check": quick,
        "foreign_key_check": foreign_keys,
    }


def _create_backup(target_db: Path, backup_db: Path) -> dict[str, Any]:
    backup_db.parent.mkdir(parents=True, exist_ok=True)
    if backup_db.exists() or backup_db.is_symlink():
        raise PromotionError(f"Backup path appeared before creation: {backup_db}")
    source = _open_read_only(target_db)
    backup: sqlite3.Connection | None = None
    try:
        backup = sqlite3.connect(backup_db)
        source.backup(backup)
        quick = [str(row[0]) for row in backup.execute("PRAGMA quick_check")]
        if quick != ["ok"]:
            raise PromotionError(f"Backup quick_check failed: {quick}")
        backup.commit()
    except Exception:
        if backup is not None:
            backup.close()
            backup = None
        backup_db.unlink(missing_ok=True)
        raise
    finally:
        if backup is not None:
            backup.close()
        source.close()
    return {"path": str(backup_db), "bytes": backup_db.stat().st_size, "quick_check": ["ok"]}


def promote_faculty_pilot(
    pilot_db: str | Path,
    target_db: str | Path,
    allowlist: str | Path,
    *,
    institution_id: str,
    expected_count: int = 124,
    backup_db: str | Path,
    allow_reviewed_enrichment_update: bool = False,
) -> dict[str, Any]:
    source_path = _safe_existing_db(pilot_db, "Pilot")
    target_path = _safe_existing_db(target_db, "Target")
    if source_path == target_path:
        raise PromotionError("Pilot and target databases must differ")
    person_ids = _load_allowlist(allowlist, expected_count)
    backup_path = _safe_backup_path(
        backup_db,
        source_db=source_path,
        target_db=target_path,
        required=True,
    )
    assert backup_path is not None

    dry_report = preflight_promotion(
        source_path,
        target_path,
        allowlist,
        institution_id=institution_id,
        expected_count=expected_count,
        backup_db=backup_path,
        allow_reviewed_enrichment_update=allow_reviewed_enrichment_update,
    )
    if dry_report["status"] != "ready":
        raise PromotionError(f"Preflight blocked promotion: {dry_report['errors'][:10]}")
    if not dry_report["disk"]["enough"]:
        raise PromotionError(f"Insufficient disk space: {dry_report['disk']}")

    backup_report = _create_backup(target_path, backup_path)
    source = _open_read_only(source_path)
    target = _open_writable(target_path)
    committed = False
    try:
        source.execute("BEGIN")
        target.execute("BEGIN IMMEDIATE")
        schema = _schema_plan(source, target)
        _apply_schema_plan(target, schema)
        transaction_report, scope = _preflight_connections(
            source,
            target,
            source_path=source_path,
            target_path=target_path,
            backup_path=backup_path,
            person_ids=person_ids,
            institution_id=institution_id,
            allow_reviewed_enrichment_update=allow_reviewed_enrichment_update,
        )
        if transaction_report["status"] != "ready":
            raise PromotionError(
                f"In-transaction preflight blocked promotion: {transaction_report['errors'][:10]}"
            )
        from datetime import datetime, timezone

        promoted_at = datetime.now(timezone.utc).isoformat()
        copied = _apply_rows(source, target, scope, promoted_at)
        verification = _verify_promoted_scope(source, target, scope, institution_id)
        target.commit()
        committed = True
    except Exception:
        if target.in_transaction:
            target.rollback()
        raise
    finally:
        source.rollback()
        source.close()
        target.close()

    post = _open_read_only(target_path)
    try:
        post_quick = [str(row[0]) for row in post.execute("PRAGMA quick_check")]
    finally:
        post.close()
    if post_quick != ["ok"]:
        raise PromotionError(
            f"Promotion committed but post-commit quick_check failed; restore {backup_path}"
        )
    return {
        "status": "committed" if committed else "rolled_back",
        "dry_run": False,
        "source_db": str(source_path),
        "target_db": str(target_path),
        "backup": backup_report,
        "institution_id": institution_id,
        "allowlist_count": len(person_ids),
        "copied_rows": copied,
        "verification": {**verification, "post_commit_quick_check": post_quick},
        "single_transaction": True,
        "canonical_rows_modified": 0,
        "raw_archives_copied": 0,
        "reviewed_enrichment_update": dry_report.get("reviewed_enrichment_update"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-db", required=True, type=Path)
    parser.add_argument("--target-db", required=True, type=Path)
    parser.add_argument("--person-allowlist", required=True, type=Path)
    parser.add_argument("--institution-id", required=True)
    parser.add_argument("--expected-count", type=int, default=124)
    parser.add_argument(
        "--out",
        type=Path,
        help="Optional UTF-8 JSON path for the complete preflight/apply audit",
    )
    parser.add_argument(
        "--backup-db",
        type=Path,
        help="Required with --apply; must be a new path",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create the backup and commit one guarded production transaction",
    )
    parser.add_argument(
        "--allow-reviewed-enrichment-update",
        action="store_true",
        help=(
            "Permit only an already-accepted cohort's reviewed append-only PI-Work "
            "enrichment update; removals and unrelated vector changes still fail closed"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows often inherits a legacy GBK console even when paths/names contain
    # characters outside that code page.  JSON is an interchange artifact, so
    # make the CLI deterministic and lossless.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    args = _parser().parse_args(argv)
    try:
        if args.apply:
            report = promote_faculty_pilot(
                args.pilot_db,
                args.target_db,
                args.person_allowlist,
                institution_id=args.institution_id,
                expected_count=args.expected_count,
                backup_db=args.backup_db,
                allow_reviewed_enrichment_update=args.allow_reviewed_enrichment_update,
            )
        else:
            report = preflight_promotion(
                args.pilot_db,
                args.target_db,
                args.person_allowlist,
                institution_id=args.institution_id,
                expected_count=args.expected_count,
                backup_db=args.backup_db,
                allow_reviewed_enrichment_update=args.allow_reviewed_enrichment_update,
            )
    except (PromotionError, FileNotFoundError, json.JSONDecodeError, sqlite3.Error) as exc:
        report = {"status": "blocked", "error": f"{type(exc).__name__}: {exc}"}
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in {"ready", "committed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
