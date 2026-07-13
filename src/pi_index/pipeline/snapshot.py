from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from ..config import INSTITUTION_CONFIG_SCHEMA_VERSION
from ..models import utc_now_iso
from ..storage import PIIndexStorage


SNAPSHOT_SCHEMA_VERSION = 1
PI_RECORD_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SnapshotResult:
    run_id: str
    snapshot_dir: Path
    manifest: dict[str, Any]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    _write_json(temporary, value)
    temporary.replace(path)


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(_canonical_json(record) + "\n")
            count += 1
    return count


def _json_records(storage: PIIndexStorage, sql: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
    rows = storage.conn.execute(sql, parameters).fetchall()
    return [json.loads(row["record_json"]) for row in rows]


def _current_pi_records(storage: PIIndexStorage, institution_id: str) -> list[dict[str, Any]]:
    return _json_records(
        storage,
        """
        SELECT record_json
        FROM canonical_pi_records
        WHERE institution_id=?
        ORDER BY display_name COLLATE NOCASE, person_id
        """,
        (institution_id,),
    )


def _current_contact_verdicts(storage: PIIndexStorage, institution_id: str) -> list[dict[str, Any]]:
    return _json_records(
        storage,
        """
        SELECT c.record_json
        FROM contact_verdicts c
        JOIN canonical_pi_records p ON p.person_id=c.person_id
        WHERE p.institution_id=?
        ORDER BY c.person_id
        """,
        (institution_id,),
    )


def _current_evidence(storage: PIIndexStorage, institution_id: str) -> list[dict[str, Any]]:
    return _json_records(
        storage,
        """
        SELECT record_json
        FROM person_evidence
        WHERE institution_id=?
        ORDER BY evidence_id
        """,
        (institution_id,),
    )


def _latest_raw_sources(storage: PIIndexStorage, institution_id: str) -> list[dict[str, Any]]:
    return _json_records(
        storage,
        """
        SELECT r.record_json
        FROM raw_sources r
        WHERE r.institution_id=?
          AND r.fetched_at=(
              SELECT MAX(r2.fetched_at)
              FROM raw_sources r2
              WHERE r2.institution_id=r.institution_id AND r2.source_url=r.source_url
          )
        ORDER BY r.source_url
        """,
        (institution_id,),
    )


def _failures(storage: PIIndexStorage, institution_id: str) -> list[dict[str, Any]]:
    rows = storage.conn.execute(
        """
        SELECT id, institution_id, source_url, stage, reason, created_at
        FROM crawl_errors
        WHERE institution_id=?
        ORDER BY id
        """,
        (institution_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _factual_record(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    result.pop("last_checked_at", None)
    result.pop("source_evidence_ids", None)
    return result


def _load_previous_records(institution_root: Path) -> tuple[str | None, list[dict[str, Any]]]:
    pointer_path = institution_root / "latest.json"
    if not pointer_path.exists():
        pointer_path = institution_root / "current.json"
    if not pointer_path.exists():
        return None, []
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    previous_run_id = pointer.get("run_id")
    records_path = institution_root / str(previous_run_id) / "pi_records.jsonl"
    if not previous_run_id or not records_path.exists():
        return None, []
    records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines() if line]
    return str(previous_run_id), records


def _changes(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_by_id = {record["person_id"]: record for record in previous}
    new_by_id = {record["person_id"]: record for record in current}
    changes: list[dict[str, Any]] = []
    for person_id in sorted(new_by_id.keys() - old_by_id.keys()):
        changes.append({"change_type": "added", "person_id": person_id, "changed_fields": []})
    for person_id in sorted(old_by_id.keys() - new_by_id.keys()):
        changes.append({"change_type": "removed", "person_id": person_id, "changed_fields": []})
    for person_id in sorted(old_by_id.keys() & new_by_id.keys()):
        old = _factual_record(old_by_id[person_id])
        new = _factual_record(new_by_id[person_id])
        changed_fields = sorted(key for key in old.keys() | new.keys() if old.get(key) != new.get(key))
        if changed_fields:
            changes.append(
                {
                    "change_type": "changed",
                    "person_id": person_id,
                    "changed_fields": changed_fields,
                }
            )
    return changes


def _coverage(records: list[dict[str, Any]], field: str) -> float:
    if not records:
        return 0.0
    return sum(bool(record.get(field)) for record in records) / len(records)


def _quality_report(
    storage: PIIndexStorage,
    institution_id: str,
    records: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    quality_gate: dict[str, Any],
) -> dict[str, Any]:
    duplicate_count = int(
        storage.conn.execute(
            "SELECT COUNT(DISTINCT duplicate_person_id) FROM duplicates WHERE institution_id=?",
            (institution_id,),
        ).fetchone()[0]
        or 0
    )
    population = len(records) + duplicate_count
    duplicate_rate = duplicate_count / population if population else 0.0
    metrics = {
        "people": len(records),
        "duplicate_count": duplicate_count,
        "duplicate_rate": duplicate_rate,
        "profile_url_coverage": _coverage(records, "profile_url"),
        "email_coverage": _coverage(records, "emails"),
        "title_coverage": _coverage(records, "title"),
        "supervisor_candidate_count": sum(
            record.get("likely_supervisor_candidate") == "true" for record in records
        ),
        "failure_count": len(failures),
    }
    checks = {
        "minimum_people": metrics["people"] >= int(quality_gate.get("minimum_people", 1)),
        "maximum_duplicate_rate": metrics["duplicate_rate"]
        <= float(quality_gate.get("maximum_duplicate_rate", 0.0)),
        "minimum_profile_url_coverage": metrics["profile_url_coverage"]
        >= float(quality_gate.get("minimum_profile_url_coverage", 0.0)),
    }
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "status": "pass" if all(checks.values()) else "fail",
        "metrics": metrics,
        "thresholds": quality_gate,
        "checks": checks,
    }


def _file_metadata(path: Path, rows: int | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "sha256": _sha256_bytes(path.read_bytes()),
        "bytes": path.stat().st_size,
    }
    if rows is not None:
        value["rows"] = rows
    return value


def create_institution_snapshot(
    storage: PIIndexStorage,
    institution_id: str,
    output_root: str | Path = "snapshots",
    *,
    config: dict[str, Any],
    config_path: str | Path | None = None,
    run_id: str | None = None,
) -> SnapshotResult:
    institution_row = storage.conn.execute(
        "SELECT record_json FROM institutions WHERE institution_id=?",
        (institution_id,),
    ).fetchone()
    if institution_row is None:
        raise ValueError(f"Unknown institution_id: {institution_id}")
    institution = json.loads(institution_row["record_json"])

    if run_id is None:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    institution_root = Path(output_root) / institution_id
    snapshot_dir = institution_root / run_id
    if snapshot_dir.exists():
        raise FileExistsError(f"Snapshot already exists: {snapshot_dir}")
    snapshot_dir.mkdir(parents=True)

    previous_run_id, previous_records = _load_previous_records(institution_root)
    records = _current_pi_records(storage, institution_id)
    verdicts = _current_contact_verdicts(storage, institution_id)
    evidence = _current_evidence(storage, institution_id)
    raw_sources = _latest_raw_sources(storage, institution_id)
    failures = _failures(storage, institution_id)
    changes = _changes(previous_records, records)
    quality = _quality_report(
        storage,
        institution_id,
        records,
        failures,
        config.get("quality_gate") or {},
    )

    row_counts = {
        "pi_records.jsonl": _write_jsonl(snapshot_dir / "pi_records.jsonl", records),
        "contact_verdicts.jsonl": _write_jsonl(snapshot_dir / "contact_verdicts.jsonl", verdicts),
        "evidence.jsonl": _write_jsonl(snapshot_dir / "evidence.jsonl", evidence),
        "raw_sources.jsonl": _write_jsonl(snapshot_dir / "raw_sources.jsonl", raw_sources),
        "failures.jsonl": _write_jsonl(snapshot_dir / "failures.jsonl", failures),
        "changes.jsonl": _write_jsonl(snapshot_dir / "changes.jsonl", changes),
    }
    _write_json(snapshot_dir / "quality_report.json", quality)

    files = {
        name: _file_metadata(snapshot_dir / name, rows)
        for name, rows in row_counts.items()
    }
    files["quality_report.json"] = _file_metadata(snapshot_dir / "quality_report.json")
    manifest = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "pi_record_schema_version": PI_RECORD_SCHEMA_VERSION,
        "institution_config_schema_version": INSTITUTION_CONFIG_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": utc_now_iso(),
        "institution_id": institution_id,
        "institution_name": institution.get("name"),
        "ror_id": institution.get("ror_id"),
        "pool_scope": config.get("pool_scope"),
        "template_family": (config.get("site") or {}).get("template_family"),
        "config_path": str(config_path) if config_path is not None else None,
        "config_sha256": _sha256_json(config),
        "previous_run_id": previous_run_id,
        "quality_status": quality["status"],
        "counts": {name.removesuffix(".jsonl"): count for name, count in row_counts.items()},
        "files": files,
    }
    _write_json(snapshot_dir / "manifest.json", manifest)
    pointer = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "institution_id": institution_id,
        "run_id": run_id,
        "manifest_sha256": _sha256_bytes((snapshot_dir / "manifest.json").read_bytes()),
        "quality_status": quality["status"],
    }
    _write_json_atomic(institution_root / "latest.json", pointer)
    if quality["status"] == "pass":
        _write_json_atomic(institution_root / "current.json", pointer)
    return SnapshotResult(run_id=run_id, snapshot_dir=snapshot_dir, manifest=manifest)
