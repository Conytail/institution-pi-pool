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
          <tr><th>Name</th><th>Title</th><th>Email</th><th>Research Areas</th></tr>
          <tr>
            <td><a href="https://example.edu/people/jane">Jane Doe</a></td>
            <td>Assistant Professor</td>
            <td><a href="mailto:jane@example.edu">jane@example.edu</a></td>
            <td><a href="/research-area/robotics">Robotics</a></td>
          </tr>
        </table>
        </body></html>
        """,
        encoding="utf-8",
    )
    config = {
        "schema_version": 1,
        "config_version": 1,
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
        "parsing": {"preferred_adapters": ["faculty_directory"]},
        "pi_detection": {
            "positive_title_patterns": ["Assistant Professor"],
            "negative_title_patterns": ["Emeritus"],
        },
        "refresh": {
            "directory_interval_days": 30,
            "profile_interval_days": 90,
        },
        "quality_gate": {
            "minimum_people": 1,
            "maximum_duplicate_rate": 0.1,
            "minimum_profile_url_coverage": 0.5,
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
    assert Path(result["snapshot_dir"]).is_dir()
    counts = storage.audit_counts()
    assert counts["high_confidence_contactable"] == 1
