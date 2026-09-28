from __future__ import annotations

import csv
from pathlib import Path

from ..storage import PIIndexStorage
from .ingest_institution import import_institutions_csv, ingest_institution


def ingest_batch(
    institution_list: str | Path,
    storage: PIIndexStorage,
    limit: int | None = None,
    snapshot_root: str | Path | None = None,
    archive_root: str | Path | None = None,
    offline: bool = False,
) -> dict:
    import_institutions_csv(institution_list, storage)
    attempted = 0
    results = []
    failed = 0
    with Path(institution_list).open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            config_path = row.get("config_path")
            if not config_path:
                continue
            attempted += 1
            path = Path(config_path)
            if not path.exists():
                storage.record_crawl_error(None, config_path, "batch_config", "config_path_missing")
                results.append(
                    {
                        "config": config_path,
                        "institution_name": row.get("name"),
                        "status": "failed",
                        "error": "config_path_missing",
                    }
                )
                failed += 1
                if limit is not None and attempted >= limit:
                    break
                continue
            try:
                result = ingest_institution(
                    path,
                    storage,
                    snapshot_root=snapshot_root,
                    archive_root=archive_root,
                    offline=offline,
                )
                results.append({"config": config_path, "status": "completed", **result})
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                storage.record_crawl_error(None, config_path, "batch_pipeline", error)
                results.append(
                    {
                        "config": config_path,
                        "institution_name": row.get("name"),
                        "status": "failed",
                        "error": error,
                    }
                )
                failed += 1
            if limit is not None and attempted >= limit:
                break
    return {
        "attempted_configs": attempted,
        "completed_configs": attempted - failed,
        "failed_configs": failed,
        "results": results,
    }
