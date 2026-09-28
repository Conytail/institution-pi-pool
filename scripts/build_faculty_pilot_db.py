#!/usr/bin/env python3
"""Build a small, self-contained Faculty pilot database from a PI pool.

The source is always opened read-only.  The target is built in a temporary
SQLite file beside the requested output, checked, and then atomically moved
into place.  Replacing an existing output requires ``--replace`` and is limited
to an ordinary (non-symlink) file.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import unicodedata
from typing import Any, Iterable
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from pi_index.storage import PIIndexStorage  # noqa: E402


PERSON_TABLES = (
    "contact_verdicts",
    "email_evidence",
    "pi_observations",
    "official_publication_fingerprints",
    "official_publication_refresh_state",
    "official_publication_source_claims",
    "pi_identity_keys",
    "openalex_author_links",
    "openalex_person_works",
    "pi_career_vectors",
)
REPORT_TABLES = (
    "institutions",
    "canonical_pi_records",
    "contact_verdicts",
    "email_evidence",
    "person_evidence",
    "pi_observations",
    "official_publication_fingerprints",
    "official_publication_source_claims",
    "official_publication_refresh_state",
    "publication_refresh_runs",
    "raw_sources",
    "pi_identity_aliases",
    "pi_identity_keys",
    "openalex_author_links",
    "openalex_works",
    "openalex_work_vectors",
    "openalex_person_works",
    "pi_career_vectors",
    "openalex_sync_runs",
)


def _fold(value: Any) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).casefold().split()
    )


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri_path = quote(path.as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    if not _table_exists(connection, table):
        return []
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]


def _safe_paths(
    source_db: str | Path,
    out_db: str | Path,
    *,
    replace: bool,
) -> tuple[Path, Path, Path]:
    source = Path(source_db).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"Source database is not a regular file: {source}")

    requested = Path(out_db).expanduser()
    requested.parent.mkdir(parents=True, exist_ok=True)
    parent = requested.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ValueError(f"Output parent is not a directory: {parent}")
    output = parent / requested.name
    if not output.name:
        raise ValueError("Output database filename is required")

    if output.exists() or output.is_symlink():
        if output.is_symlink():
            raise ValueError(f"Output may not be a symlink: {output}")
        resolved_output = output.resolve(strict=True)
        if resolved_output == source:
            raise ValueError("Output database must not be the source database")
        mode = output.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(f"Existing output is not an ordinary file: {output}")
        if not replace:
            raise FileExistsError(f"Output already exists (use --replace): {output}")
        if resolved_output.parent != parent:
            raise ValueError("Resolved output escapes its requested parent")
    else:
        resolved_candidate = output.resolve(strict=False)
        if resolved_candidate.parent != parent:
            raise ValueError("Resolved output escapes its requested parent")
        if resolved_candidate == source:
            raise ValueError("Output database must not be the source database")

    temporary = parent / f".{output.name}.pilot-{uuid4().hex}.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise FileExistsError(f"Temporary output already exists: {temporary}")
    return source, output, temporary


def _copy_rows(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    *,
    where: str = "",
    params: Iterable[Any] = (),
) -> int:
    source_columns = _columns(source, table)
    target_columns = set(_columns(target, table))
    columns = [
        column
        for column in source_columns
        if column in target_columns and column != "id"
    ]
    if not columns:
        return 0
    sql = f"SELECT {', '.join(columns)} FROM {table}"
    if where:
        sql += f" WHERE {where}"
    rows = source.execute(sql, tuple(params)).fetchall()
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in columns)
    target.executemany(
        f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        [tuple(row[column] for column in columns) for row in rows],
    )
    return len(rows)


def _copy_by_values(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    column: str,
    values: Iterable[str],
    *,
    extra_where: str | None = None,
    extra_params: Iterable[Any] = (),
) -> int:
    unique = sorted({str(value) for value in values if value})
    if not unique or column not in _columns(source, table):
        return 0
    copied = 0
    for offset in range(0, len(unique), 500):
        chunk = unique[offset : offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        where = f"{column} IN ({placeholders})"
        if extra_where:
            where = f"({where}) AND ({extra_where})"
        copied += _copy_rows(
            source,
            target,
            table,
            where=where,
            params=[*chunk, *extra_params],
        )
    return copied


def _institution(
    source: sqlite3.Connection,
    *,
    institution_id: str | None,
    institution_name: str | None,
) -> sqlite3.Row:
    if not _table_exists(source, "institutions"):
        raise ValueError("Source database has no institutions table")
    rows = source.execute("SELECT * FROM institutions").fetchall()
    if institution_id:
        matches = [row for row in rows if str(row["institution_id"]) == institution_id]
    else:
        wanted = _fold(institution_name)
        matches = []
        for row in rows:
            aliases = _loads(row["aliases_json"] if "aliases_json" in row.keys() else None, [])
            names = [row["name"], *(aliases if isinstance(aliases, list) else [])]
            if any(_fold(value) == wanted for value in names):
                matches.append(row)
    if len(matches) != 1:
        selector = institution_id or institution_name
        raise ValueError(f"Institution selector must match exactly one row: {selector!r}")
    return matches[0]


def _department_values(row: sqlite3.Row) -> list[str]:
    payload = _loads(row["record_json"] if "record_json" in row.keys() else None, {})
    values: list[Any] = []
    for key in ("department", "pool_scope"):
        if key in row.keys():
            values.append(row[key])
        if isinstance(payload, dict):
            values.append(payload.get(key))
    if isinstance(payload, dict):
        departments = payload.get("departments") or []
        values.extend(departments if isinstance(departments, list) else [departments])
    return list(dict.fromkeys(str(value) for value in values if str(value or "").strip()))


def _selected_people(
    source: sqlite3.Connection,
    institution_id: str,
    department_contains: Iterable[str],
) -> tuple[list[sqlite3.Row], dict[str, list[str]]]:
    patterns = [_fold(value) for value in department_contains if _fold(value)]
    if not patterns:
        raise ValueError("At least one non-empty --department-contains is required")
    columns = set(_columns(source, "canonical_pi_records"))
    rows = source.execute(
        "SELECT * FROM canonical_pi_records WHERE institution_id=?",
        (institution_id,),
    ).fetchall()
    selected: list[sqlite3.Row] = []
    matched_values: dict[str, list[str]] = {}
    for row in rows:
        payload = _loads(row["record_json"] if "record_json" in columns else None, {})
        membership = (
            row["membership_status"]
            if "membership_status" in columns
            else (payload.get("membership_status") if isinstance(payload, dict) else None)
        )
        if _fold(membership or "active") != "active":
            continue
        values = _department_values(row)
        hits = [value for value in values if any(pattern in _fold(value) for pattern in patterns)]
        if hits:
            selected.append(row)
            matched_values[str(row["person_id"])] = hits
    if not selected:
        raise ValueError("No active PI matched the requested institution and Faculty filters")
    return selected, matched_values


def _canonical_urls(target: sqlite3.Connection, person_ids: set[str]) -> set[str]:
    urls: set[str] = set()
    for row in target.execute("SELECT * FROM canonical_pi_records"):
        if str(row["person_id"]) not in person_ids:
            continue
        payload = _loads(row["record_json"], {})
        values = [row["profile_url"] if "profile_url" in row.keys() else None]
        if isinstance(payload, dict):
            values.extend(
                [
                    payload.get("profile_url"),
                    payload.get("lab_url"),
                    *(payload.get("profile_urls") or []),
                ]
            )
        for value in values:
            if not value:
                continue
            text = str(value)
            urls.add(text)
            parsed = urlsplit(text)
            if parsed.fragment:
                urls.add(urlunsplit(parsed._replace(fragment="")))
    for table in (
        "official_publication_fingerprints",
        "official_publication_source_claims",
        "official_publication_refresh_state",
        "pi_observations",
        "person_evidence",
        "email_evidence",
    ):
        columns = set(_columns(target, table))
        if "source_url" not in columns:
            continue
        for row in target.execute(f"SELECT source_url FROM {table}"):
            if row[0]:
                urls.add(str(row[0]))
    return urls


def _copy_raw_sources(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    institution_id: str,
    urls: set[str],
) -> int:
    if not urls or not _table_exists(source, "raw_sources"):
        return 0
    has_final_url = "final_url" in _columns(source, "raw_sources")
    copied = 0
    for offset in range(0, len(urls), 400):
        chunk = sorted(urls)[offset : offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        if has_final_url:
            where = (
                f"institution_id=? AND (source_url IN ({placeholders}) "
                f"OR final_url IN ({placeholders}))"
            )
            params = [institution_id, *chunk, *chunk]
        else:
            where = f"institution_id=? AND source_url IN ({placeholders})"
            params = [institution_id, *chunk]
        copied += _copy_rows(source, target, "raw_sources", where=where, params=params)
    return copied


def _backfill_legacy_publication_claims(target: sqlite3.Connection) -> dict[str, int]:
    before_claims = target.execute(
        "SELECT COUNT(*) FROM official_publication_source_claims"
    ).fetchone()[0]
    before_states = target.execute(
        "SELECT COUNT(*) FROM official_publication_refresh_state"
    ).fetchone()[0]
    target.execute(
        """
        INSERT OR IGNORE INTO official_publication_source_claims
        (fingerprint_id, person_id, institution_id, source_url, source_kind,
         claim_status, missing_streak, first_seen_at, last_seen_at,
         last_seen_run_id, last_checked_at, tombstoned_at, record_json)
        SELECT f.fingerprint_id, f.person_id, f.institution_id, f.source_url,
               'official_profile', 'active', 0, f.first_seen_at, f.last_seen_at,
               f.last_seen_run_id, f.last_seen_at, NULL, '{}'
        FROM official_publication_fingerprints f
        WHERE NOT EXISTS (
            SELECT 1 FROM official_publication_source_claims c
            WHERE c.fingerprint_id=f.fingerprint_id
              AND c.source_url=f.source_url
        )
        """
    )
    target.execute(
        """
        INSERT OR IGNORE INTO official_publication_refresh_state
        (person_id, institution_id, source_url, source_kind, final_url,
         etag, last_modified, body_sha256, checked_at, changed_at,
         last_success_at, parser_name, parser_version, config_hash,
         parse_status, parse_complete, publication_count, last_run_id,
         error_reason, record_json)
        SELECT f.person_id, f.institution_id, f.source_url, 'official_profile',
               f.source_url, NULL, NULL, NULL, MAX(f.last_seen_at), NULL,
               NULL, NULL, NULL, NULL, 'pilot_legacy_imported', 0,
               COUNT(*), NULL, NULL, '{}'
        FROM official_publication_fingerprints f
        WHERE NOT EXISTS (
            SELECT 1 FROM official_publication_refresh_state s
            WHERE s.person_id=f.person_id AND s.source_url=f.source_url
        )
        GROUP BY f.person_id, f.institution_id, f.source_url
        """
    )
    after_claims = target.execute(
        "SELECT COUNT(*) FROM official_publication_source_claims"
    ).fetchone()[0]
    after_states = target.execute(
        "SELECT COUNT(*) FROM official_publication_refresh_state"
    ).fetchone()[0]
    return {
        "claims_created": int(after_claims - before_claims),
        "incomplete_states_created": int(after_states - before_states),
    }


def _hash_archived_body(path: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    uncompressed_bytes = 0
    try:
        with gzip.open(path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                uncompressed_bytes += len(chunk)
    except (OSError, EOFError) as exc:
        raise ValueError(f"Invalid gzip archive blob: {path}") from exc
    return digest.hexdigest(), uncompressed_bytes, path.stat().st_size


def _archive_references(connection: sqlite3.Connection) -> dict[str, str]:
    columns = set(_columns(connection, "raw_sources"))
    if "archive_key" not in columns:
        return {}
    selected = ["archive_key"]
    for column in ("body_sha256", "content_hash"):
        if column in columns:
            selected.append(column)
    references: dict[str, str] = {}
    for row in connection.execute(
        f"SELECT {', '.join(selected)} FROM raw_sources WHERE archive_key IS NOT NULL"
    ):
        key = str(row["archive_key"] or "").strip()
        if not key:
            continue
        key_digest = Path(key).stem.casefold()
        candidates = {
            str(row[column]).strip().casefold()
            for column in ("body_sha256", "content_hash")
            if column in row.keys()
            and row[column]
            and len(str(row[column]).strip()) == 64
        }
        candidates.add(key_digest)
        if len(candidates) != 1 or len(key_digest) != 64 or any(
            character not in "0123456789abcdef" for character in key_digest
        ):
            raise ValueError(f"Archive metadata hash conflict: {key}")
        expected = candidates.pop()
        previous = references.get(key)
        if previous and previous != expected:
            raise ValueError(f"Archive key has conflicting hashes: {key}")
        references[key] = expected
    return references


def _archive_file(root: Path, archive_key: str, *, must_exist: bool) -> Path:
    if not archive_key or "\\" in archive_key:
        raise ValueError(f"Invalid archive key: {archive_key!r}")
    relative = Path(archive_key)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Archive key escapes root: {archive_key}")
    unresolved = root / relative
    if unresolved.is_symlink():
        raise ValueError(f"Archive blob may not be a symlink: {archive_key}")
    resolved = unresolved.resolve(strict=must_exist)
    if not resolved.is_relative_to(root):
        raise ValueError(f"Archive key escapes root: {archive_key}")
    if must_exist and not resolved.is_file():
        raise FileNotFoundError(f"Archive blob is missing: {resolved}")
    return resolved


def _copy_archive_blobs(
    connection: sqlite3.Connection,
    source_root_value: str | Path,
    target_root_value: str | Path,
    *,
    enabled: bool,
) -> dict[str, Any]:
    references = _archive_references(connection)
    source_requested = Path(source_root_value).expanduser()
    target_requested = Path(target_root_value).expanduser()
    base_report: dict[str, Any] = {
        "enabled": bool(enabled),
        "source_root": str(source_requested.resolve(strict=False)),
        "out_root": str(target_requested.resolve(strict=False)),
        "referenced_blobs": len(references),
        "copied_blobs": 0,
        "reused_blobs": 0,
        "referenced_compressed_bytes": 0,
        "copied_compressed_bytes": 0,
        "referenced_uncompressed_bytes": 0,
    }
    if not enabled or not references:
        base_report["self_contained"] = not references
        return base_report

    source_root = source_requested.resolve(strict=True)
    if not source_root.is_dir():
        raise ValueError(f"Source archive root is not a directory: {source_root}")
    if target_requested.exists() and target_requested.is_symlink():
        raise ValueError(f"Output archive root may not be a symlink: {target_requested}")
    target_requested.mkdir(parents=True, exist_ok=True)
    target_root = target_requested.resolve(strict=True)
    if not target_root.is_dir():
        raise ValueError(f"Output archive root is not a directory: {target_root}")
    if target_root != source_root and (
        target_root.is_relative_to(source_root) or source_root.is_relative_to(target_root)
    ):
        raise ValueError("Distinct source/output archive roots may not contain each other")
    base_report["source_root"] = str(source_root)
    base_report["out_root"] = str(target_root)

    for archive_key, expected_hash in sorted(references.items()):
        source_file = _archive_file(source_root, archive_key, must_exist=True)
        actual_hash, uncompressed_bytes, compressed_bytes = _hash_archived_body(source_file)
        if actual_hash != expected_hash:
            raise ValueError(f"Source archive content hash mismatch: {archive_key}")
        base_report["referenced_compressed_bytes"] += compressed_bytes
        base_report["referenced_uncompressed_bytes"] += uncompressed_bytes

        target_file = _archive_file(target_root, archive_key, must_exist=False)
        if target_file.exists() or target_file.is_symlink():
            if target_file.is_symlink() or not target_file.is_file():
                raise ValueError(f"Existing output archive is not an ordinary file: {target_file}")
            target_hash, _, _ = _hash_archived_body(target_file)
            if target_hash != expected_hash:
                raise ValueError(f"Existing output archive hash conflict: {archive_key}")
            base_report["reused_blobs"] += 1
            continue

        target_file.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = target_file.parent.resolve(strict=True)
        if not resolved_parent.is_relative_to(target_root):
            raise ValueError(f"Archive destination escapes root: {archive_key}")
        temporary = resolved_parent / f".{target_file.name}.{uuid4().hex}.tmp"
        try:
            shutil.copyfile(source_file, temporary)
            copied_hash, _, copied_bytes = _hash_archived_body(temporary)
            if copied_hash != expected_hash:
                raise ValueError(f"Copied archive content hash mismatch: {archive_key}")
            os.replace(temporary, target_file)
            base_report["copied_blobs"] += 1
            base_report["copied_compressed_bytes"] += copied_bytes
        finally:
            temporary.unlink(missing_ok=True)
    base_report["self_contained"] = True
    return base_report


def _integrity(connection: sqlite3.Connection, institution_id: str, person_ids: set[str]) -> dict[str, Any]:
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    quick = [row[0] for row in connection.execute("PRAGMA quick_check")]
    foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
    unrelated: dict[str, list[str]] = {}
    for table in REPORT_TABLES:
        if "institution_id" not in _columns(connection, table):
            continue
        values = sorted(
            str(row[0])
            for row in connection.execute(
                f"SELECT DISTINCT institution_id FROM {table} WHERE institution_id IS NOT NULL"
            )
            if str(row[0]) != institution_id
        )
        if values:
            unrelated[table] = values
    canonical_ids = {
        str(row[0]) for row in connection.execute("SELECT person_id FROM canonical_pi_records")
    }
    orphan_claims = connection.execute(
        """
        SELECT COUNT(*) FROM official_publication_source_claims c
        LEFT JOIN official_publication_fingerprints f USING (fingerprint_id)
        WHERE f.fingerprint_id IS NULL
        """
    ).fetchone()[0]
    orphan_works = connection.execute(
        """
        SELECT COUNT(*) FROM openalex_person_works pw
        LEFT JOIN openalex_works w USING (openalex_work_id)
        WHERE w.openalex_work_id IS NULL
        """
    ).fetchone()[0]
    ok = (
        integrity == ["ok"]
        and quick == ["ok"]
        and not foreign_keys
        and not unrelated
        and canonical_ids == person_ids
        and orphan_claims == 0
        and orphan_works == 0
    )
    return {
        "integrity_check": integrity,
        "quick_check": quick,
        "foreign_key_check": foreign_keys,
        "unrelated_institution_ids": unrelated,
        "canonical_person_ids_exact": canonical_ids == person_ids,
        "orphan_publication_claims": int(orphan_claims),
        "orphan_openalex_person_works": int(orphan_works),
        "ok": ok,
    }


def _cleanup_temporary(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.parent == path.parent and candidate.name.startswith(path.name):
            candidate.unlink(missing_ok=True)


def build_faculty_pilot_db(
    source_db: str | Path,
    out_db: str | Path,
    *,
    institution_id: str | None = None,
    institution_name: str | None = None,
    department_contains: Iterable[str],
    replace: bool = False,
    source_archive_root: str | Path | None = None,
    out_archive_root: str | Path | None = None,
    copy_archive: bool = True,
) -> dict[str, Any]:
    if bool(institution_id) == bool(institution_name):
        raise ValueError("Provide exactly one of institution_id or institution_name")
    department_contains = list(department_contains)
    source_path, output_path, temporary_path = _safe_paths(
        source_db, out_db, replace=replace
    )
    source = _open_read_only(source_path)
    target: PIIndexStorage | None = None
    try:
        institution = _institution(
            source,
            institution_id=institution_id,
            institution_name=institution_name,
        )
        selected, matched_values = _selected_people(
            source,
            str(institution["institution_id"]),
            department_contains,
        )
        person_ids = {str(row["person_id"]) for row in selected}

        target = PIIndexStorage(temporary_path)
        copied: dict[str, int] = {}
        with target.conn:
            copied["institutions"] = _copy_rows(
                source,
                target.conn,
                "institutions",
                where="institution_id=?",
                params=(institution["institution_id"],),
            )
            copied["canonical_pi_records"] = _copy_by_values(
                source, target.conn, "canonical_pi_records", "person_id", person_ids
            )
            for table in PERSON_TABLES:
                copied[table] = _copy_by_values(
                    source, target.conn, table, "person_id", person_ids
                )
            copied["pi_identity_aliases"] = _copy_by_values(
                source,
                target.conn,
                "pi_identity_aliases",
                "canonical_person_id",
                person_ids,
            )

            evidence_ids: set[str] = set()
            for row in selected:
                payload = _loads(row["record_json"], {})
                if isinstance(payload, dict):
                    evidence_ids.update(payload.get("source_evidence_ids") or [])
            copied["person_evidence"] = _copy_by_values(
                source, target.conn, "person_evidence", "evidence_id", evidence_ids
            )

            work_ids = {
                str(row[0])
                for row in target.conn.execute(
                    "SELECT openalex_work_id FROM openalex_person_works"
                )
            }
            copied["openalex_works"] = _copy_by_values(
                source, target.conn, "openalex_works", "openalex_work_id", work_ids
            )
            copied["openalex_work_vectors"] = _copy_by_values(
                source,
                target.conn,
                "openalex_work_vectors",
                "openalex_work_id",
                work_ids,
            )
            openalex_run_ids = {
                str(row[0])
                for row in target.conn.execute(
                    """
                    SELECT last_sync_run_id FROM openalex_author_links
                    WHERE last_sync_run_id IS NOT NULL
                    UNION
                    SELECT last_seen_run_id FROM openalex_person_works
                    WHERE last_seen_run_id IS NOT NULL
                    """
                )
            }
            copied["openalex_sync_runs"] = _copy_by_values(
                source, target.conn, "openalex_sync_runs", "run_id", openalex_run_ids
            )
            publication_run_ids = {
                str(row[0])
                for row in target.conn.execute(
                    """
                    SELECT last_run_id FROM official_publication_refresh_state
                    WHERE last_run_id IS NOT NULL
                    UNION
                    SELECT last_seen_run_id FROM official_publication_source_claims
                    WHERE last_seen_run_id IS NOT NULL
                    """
                )
            }
            copied["publication_refresh_runs"] = _copy_by_values(
                source,
                target.conn,
                "publication_refresh_runs",
                "run_id",
                publication_run_ids,
            )

            raw_urls = _canonical_urls(target.conn, person_ids)
            copied["raw_sources"] = _copy_raw_sources(
                source,
                target.conn,
                str(institution["institution_id"]),
                raw_urls,
            )
            legacy_backfill = _backfill_legacy_publication_claims(target.conn)

        for person_id in sorted(person_ids):
            record = target.get_pi_record(person_id)
            if record is not None:
                record.publications_summary = target.publication_summary(person_id)
                target.upsert_pi_record(record)
        target.sync_identity_index(rebuild=True)
        integrity = _integrity(
            target.conn, str(institution["institution_id"]), person_ids
        )
        if not integrity["ok"]:
            raise RuntimeError(f"Pilot database integrity failed: {integrity}")
        archive_report = _copy_archive_blobs(
            target.conn,
            source_archive_root or source_path.parent / "raw_sources",
            out_archive_root or output_path.parent / "raw_sources",
            enabled=copy_archive,
        )
        counts = {
            table: int(target.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in REPORT_TABLES
            if _table_exists(target.conn, table)
        }
        target.close()
        target = None
        os.replace(temporary_path, output_path)
        report = {
            "source_db": str(source_path),
            "out_db": str(output_path),
            "institution_id": str(institution["institution_id"]),
            "institution_name": str(institution["name"]),
            "department_contains": list(department_contains),
            "selected_person_ids": sorted(person_ids),
            "matched_department_values": matched_values,
            "copied_rows": copied,
            "legacy_publication_backfill": legacy_backfill,
            "archive": archive_report,
            "counts": counts,
            "integrity": integrity,
        }
        report["sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
        report["bytes"] = output_path.stat().st_size
        return report
    finally:
        if target is not None:
            target.close()
        source.close()
        _cleanup_temporary(temporary_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--out-db", required=True, type=Path)
    institution = parser.add_mutually_exclusive_group(required=True)
    institution.add_argument("--institution-id")
    institution.add_argument("--institution-name")
    parser.add_argument("--department-contains", action="append", required=True)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument(
        "--source-archive-root",
        default=None,
        help="Defaults to <source-db.parent>/raw_sources",
    )
    parser.add_argument(
        "--out-archive-root",
        default=None,
        help="Defaults to <out-db.parent>/raw_sources",
    )
    parser.add_argument(
        "--no-copy-archive",
        action="store_true",
        help="Skip archive blobs (the pilot will not be offline self-contained)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = build_faculty_pilot_db(
        args.source_db,
        args.out_db,
        institution_id=args.institution_id,
        institution_name=args.institution_name,
        department_contains=args.department_contains,
        replace=args.replace,
        source_archive_root=args.source_archive_root,
        out_archive_root=args.out_archive_root,
        copy_archive=not args.no_copy_archive,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
