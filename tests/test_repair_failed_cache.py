from pathlib import Path

import yaml

from pi_index.normalize.institution import institution_from_config
from pi_index.pipeline.repair_failed_cache import repair_failed_cache
from pi_index.storage import PIIndexStorage


def test_repair_failed_cache_refetches_only_failed_urls(tmp_path):
    page = tmp_path / "person.html"
    page.write_text("<html><body>Jane Doe</body></html>", encoding="utf-8")
    config_path = tmp_path / "institution.yaml"
    config = {
                "schema_version": 2,
                "config_version": 2,
                "institution": {
                    "name": "Example University",
                    "homepage_url": "https://example.edu",
                    "official_domains": ["example.edu"],
                },
                "pool_scope": {"type": "institution", "name": "Whole institution"},
                "site": {"template_family": "test"},
                "crawl": {
                    "seed_urls": [str(page)],
                    "max_depth": 0,
                    "max_pages": 1,
                    "respect_robots_txt": False,
                },
                "parsing": {
                    "extract_publication_fingerprints": True,
                    "preferred_adapters": ["generic_html"],
                },
                "capture": {
                    "archive_enabled": True,
                    "compression": "gzip",
                    "conditional_requests": True,
                    "missing_runs_before_inactive": 2,
                },
                "refresh": {"directory_interval_days": 30, "profile_interval_days": 90},
                "quality_gate": {
                    "minimum_people": 1,
                    "maximum_duplicate_rate": 0.05,
                    "minimum_profile_url_coverage": 0.0,
                    "minimum_seed_url_coverage": 1.0,
                    "minimum_unit_coverage": 1.0,
                    "minimum_profile_fetch_coverage": 0.0,
                    "minimum_profile_parse_coverage": 0.0,
                    "require_pagination_complete": True,
                },
            }
    config_path.write_text(
        yaml.safe_dump(config),
        encoding="utf-8",
    )
    storage = PIIndexStorage(tmp_path / "pool.db")
    institution_id = institution_from_config(config).institution_id
    run_id = "failed-run"
    storage.start_ingestion_run(run_id, institution_id, "Example University", str(config_path), "sha", "Whole institution")
    storage.record_crawl_error(institution_id, str(page), "fetch", "temporary failure", run_id)
    storage.finish_ingestion_run(run_id, {"pages_failed": 1}, 0, 0, "partial", False)

    result = repair_failed_cache(
        config_path,
        storage,
        run_id,
        archive_root=tmp_path / "archive",
    )

    assert result["attempted"] == 1
    assert result["succeeded"] == 1
    assert result["offline_replay_ready"] is True
    cached = storage.get_latest_raw_source(institution_id, str(page))
    assert cached is not None
    assert cached.archive_key
    assert (tmp_path / "archive" / cached.archive_key).exists()
