from __future__ import annotations

import csv

from pi_index.pipeline import ingest_qs_batch
from pi_index.storage import PIIndexStorage


def test_batch_records_each_failure_and_continues(tmp_path, monkeypatch):
    good = tmp_path / "good.yaml"
    bad = tmp_path / "bad.yaml"
    good.write_text("placeholder", encoding="utf-8")
    bad.write_text("placeholder", encoding="utf-8")
    missing = tmp_path / "missing.yaml"
    batch = tmp_path / "batch.csv"
    with batch.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "config_path"])
        writer.writeheader()
        writer.writerow({"name": "Good University", "config_path": str(good)})
        writer.writerow({"name": "Bad University", "config_path": str(bad)})
        writer.writerow({"name": "Missing University", "config_path": str(missing)})

    monkeypatch.setattr(ingest_qs_batch, "import_institutions_csv", lambda *_args: 3)

    def fake_ingest(path, *_args, **_kwargs):
        if path == bad:
            raise RuntimeError("site unavailable")
        return {"institution_name": "Good University", "canonical_pi_records": 10}

    monkeypatch.setattr(ingest_qs_batch, "ingest_institution", fake_ingest)
    storage = PIIndexStorage(tmp_path / "pool.db")
    try:
        result = ingest_qs_batch.ingest_batch(batch, storage)
    finally:
        storage.close()

    assert result["attempted_configs"] == 3
    assert result["completed_configs"] == 1
    assert result["failed_configs"] == 2
    assert [item["status"] for item in result["results"]] == ["completed", "failed", "failed"]
    assert result["results"][1]["error"] == "RuntimeError: site unavailable"
    assert result["results"][2]["error"] == "config_path_missing"
