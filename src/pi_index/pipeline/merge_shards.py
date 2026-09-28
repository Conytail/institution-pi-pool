from __future__ import annotations

import glob
from pathlib import Path
import sqlite3

from ..storage import PIIndexStorage


UPSERT_TABLES = (
    "institutions",
    "canonical_pi_records",
    "contact_verdicts",
    "email_evidence",
    # Carry-forward can attach archived official evidence whose original run
    # is intentionally absent from the refreshed shard's ingestion_runs table.
    # Copy evidence by its stable primary key so canonical source_evidence_ids
    # never become dangling references in the regional database.
    "person_evidence",
    "official_publication_fingerprints",
    "pi_identity_aliases",
)

PUBLICATION_STATE_TABLES = (
    "official_publication_refresh_state",
    "official_publication_source_claims",
)

RUN_TABLES = (
    "ingestion_runs",
    "raw_sources",
    "pi_observations",
    "crawl_errors",
    "duplicates",
    "parse_metrics",
)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _copy_rows(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    *,
    run_ids: set[str] | None = None,
) -> int:
    if not _table_exists(source, table) or not _table_exists(target, table):
        return 0
    source_columns = _columns(source, table)
    target_columns = set(_columns(target, table))
    columns = [column for column in source_columns if column in target_columns and column != "id"]
    if not columns:
        return 0

    sql = f"SELECT {', '.join(columns)} FROM {table}"
    params: tuple[str, ...] = ()
    if run_ids is not None:
        if not run_ids or "run_id" not in columns:
            return 0
        placeholders = ", ".join("?" for _ in run_ids)
        sql += f" WHERE run_id IN ({placeholders})"
        params = tuple(sorted(run_ids))
    rows = source.execute(sql, params).fetchall()
    if not rows:
        return 0

    placeholders = ", ".join("?" for _ in columns)
    target.executemany(
        f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        [tuple(row[column] for column in columns) for row in rows],
    )
    return len(rows)


def merge_database_shards(
    target: PIIndexStorage,
    shard_paths: list[str | Path],
) -> dict:
    expanded_paths: list[Path] = []
    for value in shard_paths:
        matches = [Path(match) for match in glob.glob(str(value))]
        if not matches:
            candidate = Path(value)
            if not candidate.is_file():
                raise FileNotFoundError(f"Shard database not found: {candidate}")
            matches = [candidate]
        for match in matches:
            if match.resolve() == target.db_path.resolve():
                raise ValueError(f"Target database cannot also be a shard: {match}")
            if match not in expanded_paths:
                expanded_paths.append(match)

    existing_runs = {
        str(row[0])
        for row in target.conn.execute("SELECT run_id FROM ingestion_runs WHERE run_id IS NOT NULL")
    }
    existing_refresh_runs = {
        str(row[0])
        for row in target.conn.execute(
            "SELECT run_id FROM publication_refresh_runs WHERE run_id IS NOT NULL"
        )
    }
    results: list[dict] = []

    for shard_path in expanded_paths:
        source = sqlite3.connect(shard_path)
        source.row_factory = sqlite3.Row
        try:
            source_runs = {
                str(row[0])
                for row in source.execute("SELECT run_id FROM ingestion_runs WHERE run_id IS NOT NULL")
            }
            source_refresh_runs = (
                {
                    str(row[0])
                    for row in source.execute(
                        "SELECT run_id FROM publication_refresh_runs WHERE run_id IS NOT NULL"
                    )
                }
                if _table_exists(source, "publication_refresh_runs")
                else set()
            )
            new_runs = source_runs - existing_runs
            new_refresh_runs = source_refresh_runs - existing_refresh_runs
            all_new_runs = new_runs | new_refresh_runs
            copied: dict[str, int] = {}
            affected_refresh_people: set[str] = set()
            if new_refresh_runs and _table_exists(source, "official_publication_refresh_state"):
                placeholders = ",".join("?" for _ in new_refresh_runs)
                affected_refresh_people = {
                    str(row[0])
                    for row in source.execute(
                        f"""
                        SELECT DISTINCT person_id
                        FROM official_publication_refresh_state
                        WHERE last_run_id IN ({placeholders})
                        """,
                        tuple(sorted(new_refresh_runs)),
                    )
                }
            if all_new_runs:
                with target.conn:
                    for table in UPSERT_TABLES:
                        copied[table] = _copy_rows(source, target.conn, table)
                    for table in PUBLICATION_STATE_TABLES:
                        copied[table] = _copy_rows(source, target.conn, table)
                    copied["ingestion_runs"] = _copy_rows(
                        source,
                        target.conn,
                        "ingestion_runs",
                        run_ids=new_runs,
                    )
                    copied["publication_refresh_runs"] = _copy_rows(
                        source,
                        target.conn,
                        "publication_refresh_runs",
                        run_ids=new_refresh_runs,
                    )
                    for table in (value for value in RUN_TABLES if value != "ingestion_runs"):
                        copied[table] = _copy_rows(
                            source,
                            target.conn,
                            table,
                            run_ids=all_new_runs,
                        )
                existing_runs.update(new_runs)
                existing_refresh_runs.update(new_refresh_runs)
                for person_id in sorted(affected_refresh_people):
                    target.enqueue_vector_dirty(
                        "openalex_works_sync",
                        person_id,
                        "publication_refresh_merged",
                        run_id=sorted(new_refresh_runs)[-1],
                        person_id=person_id,
                    )
            results.append(
                {
                    "shard": str(shard_path),
                    "new_run_ids": sorted(all_new_runs),
                    "new_ingestion_run_ids": sorted(new_runs),
                    "new_publication_refresh_run_ids": sorted(new_refresh_runs),
                    "rows_copied": copied,
                    "status": "merged" if all_new_runs else "already_merged",
                }
            )
        finally:
            source.close()

    shards_merged = sum(item["status"] == "merged" for item in results)
    identity_keys_synced = (
        target.sync_identity_index(rebuild=True)
        if shards_merged
        else 0
    )
    return {
        "target_db": str(target.db_path),
        "shards_attempted": len(expanded_paths),
        "shards_merged": shards_merged,
        "identity_keys_synced": identity_keys_synced,
        "results": results,
    }
