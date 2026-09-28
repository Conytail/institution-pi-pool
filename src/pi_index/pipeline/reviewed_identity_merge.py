from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from ..models import utc_now_iso
from ..storage import PIIndexStorage


@dataclass(frozen=True)
class _ReviewedPair:
    pair_index: int
    display_name: str | None
    recommended_keep_person_id: str
    merge_person_id: str
    evidence: str


@dataclass(frozen=True)
class _PreflightPair:
    pair: _ReviewedPair
    resolved_keep_person_id: str
    resolved_merge_person_id: str
    institution_id: str
    already_merged: bool


def _same_file(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return left.samefile(right)
    except OSError:
        return False


def _require_string(value: Any, *, field: str, pair_index: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"safe_merge_pairs[{pair_index}].{field} must be a non-empty string"
        )
    return value.strip()


def _load_review(review_file: Path) -> tuple[dict[str, Any], list[_ReviewedPair]]:
    try:
        payload = json.loads(review_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Identity review is not valid JSON: {review_file}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Identity review root must be a JSON object")
    raw_pairs = payload.get("safe_merge_pairs")
    if not isinstance(raw_pairs, list) or not raw_pairs:
        raise ValueError("Identity review safe_merge_pairs must be a non-empty list")

    pairs: list[_ReviewedPair] = []
    seen_pairs: set[tuple[str, str]] = set()
    for index, raw_pair in enumerate(raw_pairs):
        if not isinstance(raw_pair, dict):
            raise ValueError(f"safe_merge_pairs[{index}] must be a JSON object")
        keep_id = _require_string(
            raw_pair.get("recommended_keep_person_id"),
            field="recommended_keep_person_id",
            pair_index=index,
        )
        merge_id = _require_string(
            raw_pair.get("merge_person_id"),
            field="merge_person_id",
            pair_index=index,
        )
        evidence = _require_string(
            raw_pair.get("evidence"),
            field="evidence",
            pair_index=index,
        )
        if keep_id == merge_id:
            raise ValueError(
                f"safe_merge_pairs[{index}] must name two different person IDs"
            )
        pair_key = (keep_id, merge_id)
        if pair_key in seen_pairs:
            raise ValueError(
                f"safe_merge_pairs[{index}] duplicates an earlier reviewed pair: "
                f"{keep_id} <- {merge_id}"
            )
        seen_pairs.add(pair_key)
        display_name = raw_pair.get("display_name")
        if display_name is not None and not isinstance(display_name, str):
            raise ValueError(
                f"safe_merge_pairs[{index}].display_name must be a string when present"
            )
        pairs.append(
            _ReviewedPair(
                pair_index=index,
                display_name=display_name.strip() if display_name else None,
                recommended_keep_person_id=keep_id,
                merge_person_id=merge_id,
                evidence=evidence,
            )
        )
    return payload, pairs


def _verify_pi_index_database(database_file: Path) -> None:
    try:
        connection = sqlite3.connect(
            database_file.as_uri() + "?mode=ro",
            uri=True,
        )
    except sqlite3.Error as exc:
        raise ValueError(f"Target is not a readable SQLite database: {database_file}") from exc
    try:
        row = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='canonical_pi_records'
            """
        ).fetchone()
        if row is None:
            raise ValueError(
                f"Target is not an initialized PI index database: {database_file}"
            )
    except sqlite3.Error as exc:
        raise ValueError(f"Target is not a readable SQLite database: {database_file}") from exc
    finally:
        connection.close()


def _preflight_pairs(
    storage: PIIndexStorage,
    pairs: list[_ReviewedPair],
) -> list[_PreflightPair]:
    preflight: list[_PreflightPair] = []
    pending_edges: dict[str, str] = {}
    for pair in pairs:
        keep_id = storage.resolve_person_id(pair.recommended_keep_person_id)
        merge_id = storage.resolve_person_id(pair.merge_person_id)
        keep_record = storage.get_pi_record(keep_id)
        if keep_record is None:
            raise ValueError(
                f"safe_merge_pairs[{pair.pair_index}] recommended keep ID resolves to "
                f"a missing canonical record: {keep_id}"
            )

        if keep_id == merge_id:
            preflight.append(
                _PreflightPair(
                    pair=pair,
                    resolved_keep_person_id=keep_id,
                    resolved_merge_person_id=merge_id,
                    institution_id=keep_record.institution_id,
                    already_merged=True,
                )
            )
            continue

        merge_record = storage.get_pi_record(merge_id)
        if merge_record is None:
            raise ValueError(
                f"safe_merge_pairs[{pair.pair_index}] merge ID resolves to a missing "
                f"canonical record: {merge_id}"
            )
        if keep_record.institution_id != merge_record.institution_id:
            raise ValueError(
                f"safe_merge_pairs[{pair.pair_index}] crosses institutions: "
                f"{keep_record.institution_id} != {merge_record.institution_id}"
            )

        existing_target = pending_edges.get(merge_id)
        if existing_target is not None and existing_target != keep_id:
            raise ValueError(
                f"Reviewed merge ID {merge_id} has conflicting recommended keep IDs: "
                f"{existing_target} and {keep_id}"
            )
        pending_edges[merge_id] = keep_id
        preflight.append(
            _PreflightPair(
                pair=pair,
                resolved_keep_person_id=keep_id,
                resolved_merge_person_id=merge_id,
                institution_id=keep_record.institution_id,
                already_merged=False,
            )
        )

    # A cycle expresses contradictory reviewer instructions.  Reject it before
    # the first consolidation rather than letting call order choose a winner.
    for start in pending_edges:
        seen: set[str] = set()
        current = start
        while current in pending_edges:
            if current in seen:
                raise ValueError("Reviewed identity merge instructions contain a cycle")
            seen.add(current)
            current = pending_edges[current]
    return preflight


def apply_reviewed_identity_merges(
    database_path: str | Path,
    review_path: str | Path,
    *,
    report_path: str | Path,
    run_id: str | None = None,
    reason: str = "reviewed_identity_merge",
) -> dict[str, Any]:
    """Apply only explicitly reviewed identity pairs to an existing PI index.

    ``display_name`` is copied into the report for readability but is never used
    to infer an identity.  Every pending pair is resolved and validated before
    the first database write.  Reapplying a review is safe: aliases that already
    resolve to one canonical person are reported as ``already_merged``.
    """

    database_file = Path(database_path).resolve()
    review_file = Path(review_path).resolve()
    output_file = Path(report_path).resolve()
    if not database_file.is_file():
        raise FileNotFoundError(f"Target PI index database does not exist: {database_file}")
    if not review_file.is_file():
        raise FileNotFoundError(f"Identity review file does not exist: {review_file}")
    if _same_file(database_file, review_file):
        raise ValueError("Identity review input must not be the target database")
    if _same_file(output_file, database_file) or _same_file(output_file, review_file):
        raise ValueError(
            "Identity merge report must not overwrite the target database or review input"
        )
    if output_file.exists() and not output_file.is_file():
        raise ValueError(f"Identity merge report path is not a file: {output_file}")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError("run_id must be a non-empty string when provided")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")

    _verify_pi_index_database(database_file)
    review_payload, pairs = _load_review(review_file)
    review_bytes = review_file.read_bytes()
    effective_run_id = run_id.strip() if run_id is not None else (
        f"reviewed-identity-merge:{utc_now_iso()}"
    )
    effective_reason = reason.strip()

    storage = PIIndexStorage(database_file)
    temporary: Path | None = None
    try:
        preflight = _preflight_pairs(storage, pairs)

        # Establish a writable, independent report destination before changing
        # identities so ordinary path/permission errors cannot cause an
        # otherwise avoidable unaudited partial application.
        output_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_file.with_name(f".{output_file.name}.{uuid4().hex}.tmp")
        temporary.write_text("", encoding="utf-8")

        storage.conn.execute("BEGIN IMMEDIATE")
        try:
            records: list[dict[str, Any]] = []
            merged_count = 0
            already_merged_count = 0
            for item in preflight:
                pair = item.pair
                if item.already_merged:
                    canonical_id = item.resolved_keep_person_id
                    status = "already_merged"
                    already_merged_count += 1
                else:
                    # Resolve again because an earlier reviewed pair in the same
                    # acyclic component may have made one ID an alias.
                    keep_id = storage.resolve_person_id(pair.recommended_keep_person_id)
                    merge_id = storage.resolve_person_id(pair.merge_person_id)
                    if keep_id == merge_id:
                        canonical_id = keep_id
                        status = "already_merged_in_request"
                        already_merged_count += 1
                    else:
                        canonical_id = storage.consolidate_person_ids(
                            merge_id,
                            keep_id,
                            item.institution_id,
                            effective_reason,
                            effective_run_id,
                            commit=False,
                        )
                        duplicate_group_key = (
                            f"{item.institution_id}|identity|{canonical_id}"
                        )
                        storage.record_duplicate(
                            item.institution_id,
                            duplicate_group_key,
                            canonical_id,
                            merge_id,
                            effective_reason,
                            effective_run_id,
                            commit=False,
                        )
                        status = "merged"
                        merged_count += 1
                records.append(
                    {
                        "pair_index": pair.pair_index,
                        "display_name": pair.display_name,
                        "recommended_keep_person_id": pair.recommended_keep_person_id,
                        "merge_person_id": pair.merge_person_id,
                        "resolved_keep_person_id_before": item.resolved_keep_person_id,
                        "resolved_merge_person_id_before": item.resolved_merge_person_id,
                        "canonical_person_id_after": canonical_id,
                        "institution_id": item.institution_id,
                        "evidence": pair.evidence,
                        "status": status,
                        "duplicate_history_group_key": (
                            duplicate_group_key if status == "merged" else None
                        ),
                    }
                )

            # A reviewed batch may contain an acyclic chain (C -> B -> A).
            # Resolve every result only after all pairs have executed so early
            # report entries point at A rather than an intermediate alias B.
            for record in records:
                record["canonical_person_id_after"] = storage.resolve_person_id(
                    record["canonical_person_id_after"]
                )

            integrity_check = storage.conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity_check != "ok":
                raise RuntimeError(
                    "Target database failed integrity_check after identity merges: "
                    f"{integrity_check}"
                )
            report: dict[str, Any] = {
                "schema_version": 1,
                "operation": "apply_reviewed_identity_merges",
                "generated_at": utc_now_iso(),
                "database": str(database_file),
                "review_file": str(review_file),
                "review_sha256": hashlib.sha256(review_bytes).hexdigest(),
                "review_audit_type": review_payload.get("audit_type"),
                "run_id": effective_run_id,
                "reason": effective_reason,
                "requested_pair_count": len(preflight),
                "merged_count": merged_count,
                "already_merged_count": already_merged_count,
                "integrity_check": integrity_check,
                "records": records,
            }
            storage.conn.commit()
        except BaseException:
            storage.conn.rollback()
            raise
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(output_file)
        temporary = None
        return report
    finally:
        storage.close()
        if temporary is not None and temporary.exists():
            temporary.unlink()
