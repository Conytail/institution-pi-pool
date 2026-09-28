from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
import yaml

from pi_index.models import ParsedPerson
from pi_index.pipeline.ingest_institution import (
    _is_non_person_observation,
    _is_non_person_name,
    _sanitize_person_profile_url,
    ingest_institution,
)
from pi_index.storage import PIIndexStorage


@pytest.mark.parametrize(
    ("person", "expected"),
    [
        (
            ParsedPerson(
                name="Page Not Found",
                source_url="https://example.edu/former-profile",
                profile_url="https://example.edu/former-profile",
                source_type="official_profile",
                extraction_method="generic_html_profile",
                evidence_text="Page Not Found",
            ),
            True,
        ),
        (
            ParsedPerson(
                name="Plausible Person",
                source_url="https://example.edu/error/404?item=%2Fperson",
                profile_url="https://example.edu/error/404?item=%2Fperson",
                source_type="official_profile",
                extraction_method="generic_html_profile",
                evidence_text="Page Not Found",
            ),
            True,
        ),
        (
            ParsedPerson(
                name="Welcome to Lin Dai's HomePage",
                source_url="https://example.edu/~lindai/",
                profile_url="https://example.edu/~lindai/",
                source_type="official_profile",
                extraction_method="generic_html_profile",
                evidence_text="Welcome to Lin Dai's HomePage",
            ),
            True,
        ),
        (
            ParsedPerson(
                name="Lin Dai",
                source_url="https://example.edu/~lindai/",
                profile_url="https://example.edu/~lindai/",
                source_type="official_profile",
                extraction_method="source_specific_profile",
                evidence_text="Lin Dai",
            ),
            False,
        ),
    ],
)
def test_non_person_observations_are_filtered_at_ingestion_boundary(person, expected):
    assert _is_non_person_observation(person) is expected


@pytest.mark.parametrize(
    "name",
    [
        "Visiting Professors / Scholar",
        "Adjunct/Visiting Professors",
        "Honorary and Adjunct Professors",
        "Congratulations to Dr. M. A. A. Abdelgawad and Research Team on Patent Grant",
        "AWARDS AND HONORS",
        "SELECTED PUBLICATIONS",
        "WORK EXPERIENCE",
        "A Hub of Minds in a New Era",
        "A mist that appears for a little time and then vanishes",
        "Course Taught",
        "Courses Taught",
        "Dive into details",
        "COM's Scholarships & Awards",
        "Departmental Awards",
        "Scholarship Winners",
        "Select Publications (law/interdisciplinary)",
        "Staff Profile",
        "Link to profile",
        "Global Research Assistant Professors",
        "Harold Hwang. He",
        "Shao Qin Yao. She",
        "Vivian Yam. He",
        "Another CALAS Graduate at top Position – SJTU",
        "Guests visit CALAS and CityU Underwater Robotics Team",
        "Promotion of Dr. Ray C.C. Cheung",
        "Posted in",
        "Posted on",
        "Reset Filters",
        "Students and Alumni Success Stories",
        "Welcome Prof. Xie Zhiyao visiting CALAS group",
        "Gary, Yao, Winston, Gavin and Candice’s paper accepted in IEEE IoT-J →",
        "← Henry won the CityU EE symposium poster presentation",
    ],
)
def test_non_person_headings_and_news_are_filtered_globally(name):
    person = ParsedPerson(
        name=name,
        source_url="https://example.edu/people/page",
        profile_url="https://example.edu/people/page",
        source_type="official_profile",
        extraction_method="generic_html_profile",
        evidence_text=name,
    )

    assert _is_non_person_observation(person) is True


@pytest.mark.parametrize(
    "alias",
    [
        "Chi Wan Homepage",
        "Personal profile",
        "Political development",
        "Dedicated to",
        "Dept. of EE, City Univ. of Hong Kong",
    ],
)
def test_historical_non_person_aliases_use_the_same_name_boundary(alias):
    assert _is_non_person_name(alias) is True


def test_valid_person_name_is_not_rejected_as_news_author():
    person = ParsedPerson(
        name="Gavin Li",
        source_url="https://example.edu/people/gavin-li",
        profile_url="https://example.edu/people/gavin-li",
        source_type="official_profile",
        extraction_method="generic_html_profile",
        evidence_text="Gavin Li",
    )
    news_author = ParsedPerson(
        name="Gavin Li",
        source_url="http://www4.ee.cityu.edu.hk/CALAS/?p=1826",
        profile_url="http://www4.ee.cityu.edu.hk/CALAS/?p=1826",
        source_type="official_page",
        extraction_method="cityu_federated_generic",
        evidence_text="Gavin Li",
    )

    assert _is_non_person_observation(person) is False
    assert _is_non_person_observation(news_author) is True


def test_initial_before_he_surname_is_not_a_biography_fragment():
    assert _is_non_person_name("Henry Y. HE") is False


def test_malformed_profile_link_is_stripped_without_dropping_person():
    person = ParsedPerson(
        name="LAM Hei Yuet Sabrina",
        source_url="https://example.edu/people",
        profile_url="mailt:sabrilam6@example.edu",
        source_type="official_directory",
        extraction_method="generic_directory",
        evidence_text="LAM Hei Yuet Sabrina",
    )

    assert _sanitize_person_profile_url(person) is True
    assert person.profile_url is None
    assert _is_non_person_observation(person) is False


def _directory_html(names, profile_overrides=None):
    profile_overrides = profile_overrides or {}
    rows = []
    for name in names:
        slug = name.lower().replace(" ", "-")
        local = name.lower().replace(" ", ".")
        profile_url = profile_overrides.get(name, f"https://example.edu/people/{slug}")
        rows.append(
            f"""
            <tr>
              <td><a href="{profile_url}">{name}</a></td>
              <td>Professor</td>
              <td><a href="mailto:{local}@example.edu">{local}@example.edu</a></td>
            </tr>
            """
        )
    return "<html><body><table><tr><th>Name</th><th>Title</th><th>Email</th></tr>" + "".join(rows) + "</table></body></html>"


def _write_config(tmp_path, directory_path):
    config = {
        "schema_version": 2,
        "config_version": 2,
        "institution": {
            "name": "Example University",
            "homepage_url": "https://example.edu",
            "official_domains": ["example.edu"],
            "allowed_email_domains": ["example.edu"],
        },
        "pool_scope": {
            "type": "department",
            "name": "Computer Science",
            "units": [{"name": "Computer Science", "seed_urls": [str(directory_path)]}],
        },
        "site": {"template_family": "test_faculty_directory_v1"},
        "crawl": {
            "seed_urls": [str(directory_path)],
            "max_depth": 0,
            "max_pages": 1,
            "crawl_delay_seconds": 0,
            "use_homepage_discovery": False,
            "allow_serp": False,
        },
        "parsing": {
            "preferred_adapters": ["faculty_directory"],
            "extract_publication_fingerprints": True,
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
            "maximum_duplicate_rate": 0.1,
            "minimum_profile_url_coverage": 1.0,
            "minimum_seed_url_coverage": 1.0,
            "minimum_unit_coverage": 1.0,
            "minimum_profile_fetch_coverage": 1.0,
            "minimum_profile_parse_coverage": 1.0,
            "require_pagination_complete": True,
        },
    }
    config_path = tmp_path / "institution.yaml"
    policy_path = tmp_path / "crawl_policy.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    policy_path.write_text(
        yaml.safe_dump(
            {
                "user_agent": "pi-index-test/1.0",
                "respect_robots_txt": False,
                "default_crawl_delay_seconds": 0,
                "common_official_paths": [],
            }
        ),
        encoding="utf-8",
    )
    return config_path, policy_path


def test_repeated_ingestion_is_incremental_and_tracks_membership_without_blob_duplication(tmp_path):
    directory = tmp_path / "faculty.html"
    changed_directory = _directory_html(
        ["Jane Doe", "John Smith"],
        {"John Smith": "https://profiles.example.edu/faculty/john-smith"},
    )
    directory.write_text(_directory_html(["Jane Doe", "John Smith"]), encoding="utf-8")
    config_path, policy_path = _write_config(tmp_path, directory)
    storage = PIIndexStorage(tmp_path / "pool.db")
    archive_root = tmp_path / "archive"
    snapshot_root = tmp_path / "snapshots"

    first = ingest_institution(
        config_path,
        storage,
        policy_path,
        snapshot_root=snapshot_root,
        archive_root=archive_root,
    )
    first_records = {record.display_name: record for record in storage.iter_pi_records()}
    john_id = first_records["John Smith"].person_id
    john_first_seen = first_records["John Smith"].first_seen_at
    assert first["canonical_pi_records"] == 2
    assert first["crawl_complete"] is True

    directory.write_text(_directory_html(["Jane Doe"]), encoding="utf-8")
    second = ingest_institution(
        config_path,
        storage,
        policy_path,
        snapshot_root=snapshot_root,
        archive_root=archive_root,
    )
    john = storage.get_pi_record(john_id)
    assert second["crawl_complete"] is True
    assert john.membership_status == "missing"
    assert john.missing_streak == 1
    assert john.first_seen_at == john_first_seen
    assert storage.get_contact_verdict_records()[john_id].current_affiliation_confidence == "low"
    assert storage.audit_counts()["high_confidence_contactable"] == 1

    third = ingest_institution(
        config_path,
        storage,
        policy_path,
        snapshot_root=snapshot_root,
        archive_root=archive_root,
    )
    john = storage.get_pi_record(john_id)
    assert third["crawl_complete"] is True
    assert john.membership_status == "inactive"
    assert john.missing_streak == 2
    assert storage.get_contact_verdict_records()[john_id].current_affiliation_confidence == "none"
    assert "John Smith" not in {record.display_name for record in storage.iter_pi_records()}

    directory.write_text(changed_directory, encoding="utf-8")
    fourth = ingest_institution(
        config_path,
        storage,
        policy_path,
        snapshot_root=snapshot_root,
        archive_root=archive_root,
    )
    john = storage.get_pi_record(john_id)
    assert fourth["crawl_complete"] is True
    assert fourth["snapshot_quality_status"] == "pass"
    assert john.membership_status == "active"
    assert john.missing_streak == 0
    assert john.first_seen_at == john_first_seen
    assert john.last_seen_run_id == fourth["run_id"]
    assert storage.get_contact_verdict_records()[john_id].current_affiliation_confidence == "high"

    fifth = ingest_institution(
        config_path,
        storage,
        policy_path,
        snapshot_root=snapshot_root,
        archive_root=archive_root,
    )
    alias = storage.conn.execute(
        "SELECT canonical_person_id, last_seen_run_id FROM pi_identity_aliases"
    ).fetchone()
    assert alias["canonical_person_id"] == john_id
    assert alias["last_seen_run_id"] == fifth["run_id"]
    assert storage.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 0
    assert storage.conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 5
    assert storage.conn.execute("SELECT COUNT(*) FROM raw_sources").fetchone()[0] == 5
    assert storage.conn.execute("SELECT COUNT(*) FROM pi_observations").fetchone()[0] == 8
    assert len(list(archive_root.rglob("*.gz"))) == 3
    current_pointer = json.loads(
        (snapshot_root / first["institution_id"] / "current.json").read_text(encoding="utf-8")
    )
    assert current_pointer["run_id"] == fifth["run_id"]
    assert Path(fifth["snapshot_dir"]).is_dir()
    storage.close()


def test_fatal_ingestion_error_finalizes_the_new_run_as_failed(tmp_path, monkeypatch):
    directory = tmp_path / "faculty.html"
    directory.write_text(_directory_html(["Jane Doe"]), encoding="utf-8")
    config_path, policy_path = _write_config(tmp_path, directory)
    storage = PIIndexStorage(tmp_path / "pool.db")
    module = importlib.import_module("pi_index.pipeline.ingest_institution")

    def fail_crawl(_self):
        raise RuntimeError("parser process stopped")

    monkeypatch.setattr(module.ConfigDrivenInstitutionAdapter, "crawl_and_parse", fail_crawl)

    with pytest.raises(RuntimeError, match="parser process stopped"):
        ingest_institution(config_path, storage, policy_path)

    run = storage.conn.execute(
        "SELECT run_id, status, crawl_complete, metrics_json FROM ingestion_runs"
    ).fetchone()
    assert run["status"] == "failed"
    assert run["crawl_complete"] == 0
    assert json.loads(run["metrics_json"])["fatal_error"] == "RuntimeError: parser process stopped"
    error = storage.conn.execute(
        "SELECT run_id, stage, reason FROM crawl_errors"
    ).fetchone()
    assert dict(error) == {
        "run_id": run["run_id"],
        "stage": "pipeline",
        "reason": "RuntimeError: parser process stopped",
    }
    storage.close()


def test_failed_profile_url_quality_gate_does_not_advance_missing_lifecycle(tmp_path):
    directory = tmp_path / "faculty.html"
    directory.write_text(_directory_html(["Jane Doe", "John Smith"]), encoding="utf-8")
    config_path, policy_path = _write_config(tmp_path, directory)
    storage = PIIndexStorage(tmp_path / "pool.db")
    first = ingest_institution(config_path, storage, policy_path)
    john_id = next(
        record.person_id for record in storage.iter_pi_records() if record.display_name == "John Smith"
    )
    assert first["crawl_complete"] is True

    directory.write_text(
        """
        <html><body><table>
          <tr><th>Name</th><th>Title</th><th>Email</th></tr>
          <tr><td>Jane Doe</td><td>Professor</td>
              <td><a href="mailto:jane.doe@example.edu">jane.doe@example.edu</a></td></tr>
        </table></body></html>
        """,
        encoding="utf-8",
    )
    second = ingest_institution(config_path, storage, policy_path)

    assert second["crawl_complete"] is False
    assert second["crawl_metrics"]["observed_profile_url_coverage"] == 0.0
    john = storage.get_pi_record(john_id)
    assert john.membership_status == "active"
    assert john.missing_streak == 0
    storage.close()


def test_pipeline_can_reparse_from_archive_with_source_file_removed(tmp_path):
    directory = tmp_path / "faculty.html"
    directory.write_text(_directory_html(["Jane Doe"]), encoding="utf-8")
    config_path, policy_path = _write_config(tmp_path, directory)
    storage = PIIndexStorage(tmp_path / "pool.db")
    archive_root = tmp_path / "archive"

    first = ingest_institution(
        config_path,
        storage,
        policy_path,
        archive_root=archive_root,
    )
    directory.unlink()
    second = ingest_institution(
        config_path,
        storage,
        policy_path,
        archive_root=archive_root,
        offline=True,
    )

    assert first["canonical_pi_records"] == 1
    assert second["canonical_pi_records"] == 1
    assert second["crawl_complete"] is True
    assert second["crawl_metrics"]["pages_not_modified"] == 1
    assert second["crawl_metrics"]["network_bytes"] == 0
    raw = storage.conn.execute(
        "SELECT http_status, not_modified, archive_key FROM raw_sources ORDER BY fetched_at"
    ).fetchall()
    assert [row["http_status"] for row in raw] == [200, 304]
    assert [row["not_modified"] for row in raw] == [0, 1]
    assert raw[0]["archive_key"] == raw[1]["archive_key"]
    storage.close()
