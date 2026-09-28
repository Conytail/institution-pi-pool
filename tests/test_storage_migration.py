from __future__ import annotations

import json
import sqlite3

from pi_index.storage import PIIndexStorage


def _create_v01_database(path):
    legacy_pi = {
        "person_id": "pi_0123456789abcdef",
        "display_name": "Jane Doe",
        "given_name": "Jane",
        "family_name": "Doe",
        "aliases": [],
        "institution_id": "inst_example",
        "institution_name": "Example University",
        "ror_id": None,
        "department": "Computer Science",
        "title": "Professor",
        "profile_url": "https://example.edu/people/jane-doe",
        "lab_url": None,
        "emails": ["jane@example.edu"],
        "research_areas": ["machine learning"],
        "publications_summary": {},
        "external_ids": {},
        "supervision_signals": ["Professor"],
        "source_evidence_ids": ["ev-old"],
        "last_checked_at": "2026-01-01T00:00:00+00:00",
        "contact_confidence": "high",
        "pi_supervisor_confidence": "high",
        "topic_match_confidence": "unknown",
        "likely_supervisor_candidate": "true",
        "current_affiliation_confidence": "high",
    }
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE canonical_pi_records (
            person_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            institution_id TEXT NOT NULL,
            institution_name TEXT NOT NULL,
            title TEXT,
            department TEXT,
            profile_url TEXT,
            emails_json TEXT NOT NULL,
            research_areas_json TEXT NOT NULL,
            contact_confidence TEXT,
            pi_supervisor_confidence TEXT,
            topic_match_confidence TEXT,
            likely_supervisor_candidate TEXT,
            current_affiliation_confidence TEXT,
            dedupe_key TEXT,
            record_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE raw_sources (
            source_url TEXT NOT NULL,
            institution_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            source_type TEXT,
            http_status INTEGER,
            content_hash TEXT,
            parser_used TEXT,
            crawl_method TEXT,
            error_reason TEXT,
            record_json TEXT NOT NULL,
            PRIMARY KEY (source_url, institution_id, fetched_at)
        );
        CREATE TABLE ingestion_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            institution_id TEXT,
            institution_name TEXT,
            config_name TEXT,
            pages_attempted INTEGER,
            pages_successfully_fetched INTEGER,
            pages_failed INTEGER,
            people_extracted INTEGER,
            emails_extracted INTEGER,
            status TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO canonical_pi_records
        (person_id, display_name, institution_id, institution_name, title, department,
         profile_url, emails_json, research_areas_json, contact_confidence,
         pi_supervisor_confidence, topic_match_confidence, likely_supervisor_candidate,
         current_affiliation_confidence, dedupe_key, record_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            legacy_pi["person_id"],
            legacy_pi["display_name"],
            legacy_pi["institution_id"],
            legacy_pi["institution_name"],
            legacy_pi["title"],
            legacy_pi["department"],
            legacy_pi["profile_url"],
            json.dumps(legacy_pi["emails"]),
            json.dumps(legacy_pi["research_areas"]),
            legacy_pi["contact_confidence"],
            legacy_pi["pi_supervisor_confidence"],
            legacy_pi["topic_match_confidence"],
            legacy_pi["likely_supervisor_candidate"],
            legacy_pi["current_affiliation_confidence"],
            "legacy-key",
            json.dumps(legacy_pi),
            legacy_pi["last_checked_at"],
        ),
    )
    legacy_raw = {
        "source_url": "https://example.edu/faculty",
        "source_type": "official_directory",
        "institution_id": "inst_example",
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "http_status": 200,
        "content_hash": "abc",
        "parser_used": "faculty_directory",
        "crawl_method": "configured_seed",
        "error_reason": None,
    }
    conn.execute(
        """
        INSERT INTO raw_sources
        (source_url, institution_id, fetched_at, source_type, http_status, content_hash,
         parser_used, crawl_method, error_reason, record_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            legacy_raw["source_url"],
            legacy_raw["institution_id"],
            legacy_raw["fetched_at"],
            legacy_raw["source_type"],
            legacy_raw["http_status"],
            legacy_raw["content_hash"],
            legacy_raw["parser_used"],
            legacy_raw["crawl_method"],
            legacy_raw["error_reason"],
            json.dumps(legacy_raw),
        ),
    )
    conn.execute(
        """
        INSERT INTO ingestion_runs
        (institution_id, institution_name, config_name, pages_attempted,
         pages_successfully_fetched, pages_failed, people_extracted,
         emails_extracted, status, created_at)
        VALUES ('inst_example', 'Example University', 'legacy.yaml', 1, 1, 0, 1, 1, 'success',
                '2026-01-01T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()


def test_v01_database_migrates_in_place_without_losing_records(tmp_path):
    database = tmp_path / "legacy.db"
    _create_v01_database(database)

    storage = PIIndexStorage(database)

    canonical_columns = {
        row["name"] for row in storage.conn.execute("PRAGMA table_info(canonical_pi_records)")
    }
    raw_columns = {row["name"] for row in storage.conn.execute("PRAGMA table_info(raw_sources)")}
    run_columns = {row["name"] for row in storage.conn.execute("PRAGMA table_info(ingestion_runs)")}
    assert {
        "first_seen_at",
        "last_seen_at",
        "last_seen_run_id",
        "membership_status",
        "missing_streak",
        "pool_scope",
        "schema_version",
    }.issubset(canonical_columns)
    assert {
        "run_id",
        "final_url",
        "etag",
        "last_modified",
        "archive_key",
        "body_sha256",
        "not_modified",
    }.issubset(raw_columns)
    assert {"run_id", "config_sha256", "metrics_json", "started_at", "finished_at", "crawl_complete"}.issubset(
        run_columns
    )
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM pi_observations"
    ).fetchone()[0] == 0
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM official_publication_fingerprints"
    ).fetchone()[0] == 0
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM pi_identity_aliases"
    ).fetchone()[0] == 0

    record = storage.get_pi_record("pi_0123456789abcdef")
    assert record.display_name == "Jane Doe"
    assert record.membership_status == "active"
    assert record.missing_streak == 0
    migrated_json = json.loads(
        storage.conn.execute(
            "SELECT record_json FROM canonical_pi_records WHERE person_id=?",
            (record.person_id,),
        ).fetchone()["record_json"]
    )
    assert migrated_json["schema_version"] == 2
    assert migrated_json["first_seen_at"] == "2026-01-01T00:00:00+00:00"
    assert migrated_json["membership_status"] == "active"
    assert "supervision_signals" not in migrated_json
    assert "pi_supervisor_confidence" not in migrated_json
    assert "likely_supervisor_candidate" not in migrated_json
    assert storage.conn.execute(
        "SELECT value FROM schema_meta WHERE key='canonical_pi_record_schema'"
    ).fetchone()["value"] == "2-neutral-v1"
    storage.upsert_pi_record(record)
    migrated = storage.get_pi_record(record.person_id)
    assert migrated.first_seen_at == "2026-01-01T00:00:00+00:00"
    assert migrated.schema_version == 2

    storage.start_ingestion_run(
        "run-new",
        "inst_example",
        "Example University",
        "example-v2.yaml",
        "config-hash",
        "Computer Science",
    )
    metrics = {"pages_attempted": 1, "pages_succeeded": 1, "pages_failed": 0}
    storage.finish_ingestion_run("run-new", metrics, 1, 1, "success", True)
    run = storage.get_ingestion_run("run-new")
    assert run["crawl_complete"] == 1
    assert run["metrics"] == metrics
    assert storage.audit_counts()["canonical_pi_records_current"] == 1
    storage.close()


def test_fresh_database_schema_has_no_eligibility_columns(tmp_path):
    storage = PIIndexStorage(tmp_path / "fresh.db")

    canonical_columns = {
        row["name"] for row in storage.conn.execute("PRAGMA table_info(canonical_pi_records)")
    }
    verdict_columns = {
        row["name"] for row in storage.conn.execute("PRAGMA table_info(contact_verdicts)")
    }
    match_columns = {
        row["name"] for row in storage.conn.execute("PRAGMA table_info(match_results)")
    }
    assert "pi_supervisor_confidence" not in canonical_columns | verdict_columns
    assert "likely_supervisor_candidate" not in canonical_columns | verdict_columns
    assert "supervisor_validity_score" not in match_columns
    assert "supervision_score" not in match_columns


def test_openalex_person_work_author_id_migrates_to_nullable_without_data_loss(
    tmp_path,
):
    database = tmp_path / "legacy-openalex.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE openalex_person_works (
            person_id TEXT NOT NULL,
            openalex_work_id TEXT NOT NULL,
            institution_id TEXT NOT NULL,
            openalex_author_id TEXT NOT NULL,
            relationship_status TEXT NOT NULL DEFAULT 'active',
            missing_streak INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_seen_run_id TEXT NOT NULL,
            last_checked_at TEXT NOT NULL,
            tombstoned_at TEXT,
            record_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (person_id, openalex_work_id)
        );
        INSERT INTO openalex_person_works VALUES
        ('pi_1','W1','inst_1','A1','active',0,'t','t','run','t',NULL,'{}'),
        ('pi_2','W2','inst_1','','active',0,'t','t','run','t',NULL,'{}');
        """
    )
    connection.commit()
    connection.close()

    storage = PIIndexStorage(database)
    try:
        author_column = next(
            row
            for row in storage.conn.execute("PRAGMA table_info(openalex_person_works)")
            if row["name"] == "openalex_author_id"
        )
        assert author_column["notnull"] == 0
        rows = storage.conn.execute(
            "SELECT person_id, openalex_author_id FROM openalex_person_works ORDER BY person_id"
        ).fetchall()
        assert [(row["person_id"], row["openalex_author_id"]) for row in rows] == [
            ("pi_1", "A1"),
            ("pi_2", None),
        ]
    finally:
        storage.close()


def test_neutral_export_removes_deprecated_supervisor_csvs_from_reused_directory(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    output = tmp_path / "exports"
    output.mkdir()
    deprecated = {
        "verified_supervisor_candidates.csv",
        "plausible_supervisor_review_queue.csv",
        "contactable_non_supervisor.csv",
        "supervisor_candidates.csv",
    }
    for filename in deprecated:
        (output / filename).write_text("stale legacy export", encoding="utf-8")

    storage.export(output)

    assert all(not (output / filename).exists() for filename in deprecated)
    assert (output / "active_research_pool.csv").is_file()
    storage.close()
