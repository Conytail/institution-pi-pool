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
) -> dict:
    import_institutions_csv(institution_list, storage)
    attempted = 0
    results = []
    with Path(institution_list).open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            config_path = row.get("config_path")
            if not config_path:
                continue
            path = Path(config_path)
            if not path.exists():
                storage.record_crawl_error(None, config_path, "batch_config", "config_path_missing")
                continue
            results.append(
                {
                    "config": config_path,
                    **ingest_institution(path, storage, snapshot_root=snapshot_root),
                }
            )
            attempted += 1
            if limit is not None and attempted >= limit:
                break
    return {"attempted_configs": attempted, "results": results}
