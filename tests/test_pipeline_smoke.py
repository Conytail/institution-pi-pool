from pathlib import Path

import yaml

from pi_index.pipeline.ingest_institution import ingest_institution
from pi_index.storage import PIIndexStorage


def test_pipeline_smoke_with_local_html(tmp_path):
    html = tmp_path / "faculty.html"
    html.write_text(
        """
        <html><body>
        <table>
          <tr><th>Name</th><th>Title</th><th>Email</th><th>Research Areas</th><th>Publications</th></tr>
          <tr>
            <td><a href="https://example.edu/people/jane">Jane Doe</a></td>
            <td>Assistant Professor</td>
            <td><a href="mailto:jane@example.edu">jane@example.edu</a></td>
            <td><a href="/research-area/robotics">Robotics</a></td>
            <td><h4>Publications</h4><p><strong>Reliable Robot Learning</strong>, 2025.
                <a href="https://doi.org/10.5555/robot.1">doi</a></p></td>
          </tr>
        </table>
        </body></html>
        """,
        encoding="utf-8",
    )
    config = {
        "schema_version": 2,
        "config_version": 2,
        "institution": {
            "name": "Example University",
            "country": "United States",
            "homepage_url": "https://example.edu",
            "official_domains": ["example.edu"],
            "allowed_email_domains": ["example.edu"],
        },
        "pool_scope": {
            "type": "department",
            "name": "Example Department",
        },
        "site": {"template_family": "test_faculty_directory_v1"},
        "crawl": {
            "seed_urls": [str(html)],
            "max_depth": 0,
            "max_pages": 1,
            "crawl_delay_seconds": 0,
        },
        "parsing": {
            "preferred_adapters": ["faculty_directory"],
            "extract_publication_fingerprints": True,
        },
        "refresh": {
            "directory_interval_days": 30,
            "profile_interval_days": 90,
        },
        "capture": {
            "archive_enabled": True,
            "compression": "gzip",
            "conditional_requests": True,
            "missing_runs_before_inactive": 2,
        },
        "quality_gate": {
            "minimum_people": 1,
            "maximum_duplicate_rate": 0.1,
            "minimum_profile_url_coverage": 0.5,
            "minimum_seed_url_coverage": 1.0,
            "minimum_unit_coverage": 1.0,
            "minimum_profile_fetch_coverage": 0.9,
            "minimum_profile_parse_coverage": 0.0,
            "require_pagination_complete": True,
        },
    }
    config_path = tmp_path / "config.yaml"
    policy_path = tmp_path / "crawl_policy.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    policy_path.write_text(
        yaml.safe_dump(
            {
                "user_agent": "pi-index-test/0.1",
                "respect_robots_txt": True,
                "default_crawl_delay_seconds": 0,
                "common_official_paths": [],
            }
        ),
        encoding="utf-8",
    )
    storage = PIIndexStorage(tmp_path / "pi_index.db")
    result = ingest_institution(
        config_path,
        storage,
        policy_path,
        snapshot_root=tmp_path / "snapshots",
    )
    assert result["canonical_pi_records"] == 1
    assert result["emails"] == 1
    assert result["snapshot_quality_status"] == "pass"
    assert result["crawl_metrics"]["unit_coverage"] == 1.0
    assert result["crawl_metrics"]["crawl_complete"] is True
    assert result["crawl_metrics"]["http_requests_total"] == 1
    assert result["crawl_metrics"]["archived_responses"] == 1
    assert Path(result["snapshot_dir"]).is_dir()
    assert len(list(Path(result["archive_root"]).rglob("*.gz"))) == 1
    counts = storage.audit_counts()
    assert counts["high_confidence_contactable"] == 1
    assert counts["raw_archive_unique_blobs"] == 1
    assert counts["raw_archive_unique_compressed_bytes"] > 0
    assert counts["official_publication_fingerprints"] == 1
    run_id = result["run_id"]
    assert storage.get_ingestion_run(run_id)["status"] == "success"
    assert storage.conn.execute("SELECT COUNT(*) FROM raw_sources WHERE run_id=?", (run_id,)).fetchone()[0] == 1
    assert storage.conn.execute("SELECT COUNT(*) FROM person_evidence WHERE run_id=?", (run_id,)).fetchone()[0] > 0
    assert storage.conn.execute("SELECT COUNT(*) FROM pi_observations WHERE run_id=?", (run_id,)).fetchone()[0] == 1
    assert storage.conn.execute("SELECT COUNT(*) FROM official_publication_fingerprints").fetchone()[0] == 1
    pi = next(storage.iter_pi_records())
    assert pi.publications_summary == {
        "official_fingerprint_count": 1,
        "official_fingerprint_latest_year": 2025,
        "official_fingerprint_doi_count": 1,
    }
