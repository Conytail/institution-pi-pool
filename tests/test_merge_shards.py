from __future__ import annotations

import json

from pi_index.models import CanonicalPIRecord, OfficialPublicationFingerprint
from pi_index.pipeline.merge_shards import merge_database_shards
from pi_index.storage import PIIndexStorage


def _seed_shard(storage: PIIndexStorage) -> None:
    storage.conn.execute(
        """
        INSERT INTO institutions (
            institution_id, name, aliases_json, official_domains_json, record_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ("inst_example", "Example University", "[]", '["example.edu"]', "{}"),
    )
    storage.conn.execute(
        """
        INSERT INTO ingestion_runs (
            institution_id, institution_name, status, created_at, run_id, started_at,
            crawl_complete
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "inst_example",
            "Example University",
            "success",
            "2026-07-13T00:00:00+00:00",
            "run-example",
            "2026-07-13T00:00:00+00:00",
            1,
        ),
    )
    record = json.dumps({"person_id": "pi_example", "display_name": "Jane Doe"})
    storage.conn.execute(
        """
        INSERT INTO canonical_pi_records (
            person_id, display_name, institution_id, institution_name, emails_json,
            research_areas_json, record_json, updated_at, last_seen_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "pi_example",
            "Jane Doe",
            "inst_example",
            "Example University",
            "[]",
            "[]",
            record,
            "2026-07-13T00:00:00+00:00",
            "run-example",
        ),
    )
    storage.conn.execute(
        """
        INSERT INTO raw_sources (
            source_url, institution_id, fetched_at, source_type, http_status,
            content_hash, record_json, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "https://example.edu/people",
            "inst_example",
            "2026-07-13T00:00:00+00:00",
            "official_page",
            200,
            "abc",
            "{}",
            "run-example",
        ),
    )
    storage.record_crawl_error(
        "inst_example",
        "https://example.edu/missing",
        "fetch",
        "http_404",
        "run-example",
    )
    storage.conn.commit()


def test_merge_database_shards_is_complete_and_idempotent(tmp_path):
    shard = PIIndexStorage(tmp_path / "shard.db")
    _seed_shard(shard)
    shard.close()
    target = PIIndexStorage(tmp_path / "target.db")

    first = merge_database_shards(target, [tmp_path / "shard*.db"])
    second = merge_database_shards(target, [tmp_path / "shard.db"])

    assert first["shards_merged"] == 1
    assert first["identity_keys_synced"] == 1
    assert second["shards_merged"] == 0
    assert target.conn.execute("SELECT COUNT(*) FROM institutions").fetchone()[0] == 1
    assert target.conn.execute("SELECT COUNT(*) FROM canonical_pi_records").fetchone()[0] == 1
    assert target.conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 1
    assert target.conn.execute("SELECT COUNT(*) FROM raw_sources").fetchone()[0] == 1
    assert target.conn.execute("SELECT COUNT(*) FROM crawl_errors").fetchone()[0] == 1
    assert target.conn.execute(
        "SELECT COUNT(*) FROM pi_identity_keys WHERE person_id='pi_example'"
    ).fetchone()[0] == 1
    target.close()


def test_merge_database_shards_rebuilds_strong_identity_keys(tmp_path):
    shard = PIIndexStorage(tmp_path / "identity-shard.db")
    _seed_shard(shard)
    record = CanonicalPIRecord(
        person_id="pi_orcid_source",
        display_name="Jane Doe",
        given_name="Jane",
        family_name="Doe",
        aliases=[],
        institution_id="inst_example",
        institution_name="Example University",
        ror_id=None,
        department=None,
        title="Professor",
        profile_url="https://example.edu/people/jane-doe",
        lab_url=None,
        emails=[],
        research_areas=[],
        publications_summary={},
        external_ids={"orcid": "0000-0001-2345-6789"},
        source_evidence_ids=[],
        last_checked_at="2026-07-13T00:00:00+00:00",
        profile_urls=["https://example.edu/people/jane-doe"],
    )
    shard.upsert_pi_record(record)
    shard.close()

    target = PIIndexStorage(tmp_path / "identity-target.db")
    result = merge_database_shards(target, [tmp_path / "identity-shard.db"])
    alias = CanonicalPIRecord(
        **{
            **record.__dict__,
            "person_id": "pi_orcid_alias",
            "display_name": "J. Doe",
            "profile_url": "https://example.edu/profiles/opaque-jane",
            "profile_urls": ["https://example.edu/profiles/opaque-jane"],
            "external_ids": {"orcid_url": "https://orcid.org/0000-0001-2345-6789"},
        }
    )

    assert result["identity_keys_synced"] == 2
    assert target.find_existing_duplicate(alias) == (record.person_id, "same_orcid")
    target.close()


def test_merge_database_shards_keeps_carried_evidence_from_archived_run(tmp_path):
    shard = PIIndexStorage(tmp_path / "carry-shard.db")
    _seed_shard(shard)
    shard.conn.execute(
        """
        INSERT INTO person_evidence (
            evidence_id, person_temp_id, institution_id, field_name, field_value,
            source_url, source_type, extraction_method, extracted_at, confidence,
            record_json, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ev_archived",
            "pi_example",
            "inst_example",
            "title",
            "Professor",
            "https://example.edu/archived-profile",
            "official_profile",
            "carry_forward",
            "2025-01-01T00:00:00+00:00",
            0.95,
            "{}",
            # This archived run is deliberately not present in ingestion_runs.
            "run-archived-not-replayed",
        ),
    )
    shard.conn.commit()
    shard.close()

    target = PIIndexStorage(tmp_path / "carry-target.db")
    result = merge_database_shards(target, [tmp_path / "carry-shard.db"])

    assert result["results"][0]["rows_copied"]["person_evidence"] == 1
    assert target.conn.execute(
        "SELECT COUNT(*) FROM person_evidence WHERE evidence_id='ev_archived'"
    ).fetchone()[0] == 1
    target.close()


def test_merge_database_shards_applies_publication_only_refresh_once(tmp_path):
    """A publication refresh must merge even without a new ingestion run."""

    source_url = "https://example.edu/people/jane-doe"
    fingerprint_id = "pubfp_example"
    shard_path = tmp_path / "publication-shard.db"
    shard = PIIndexStorage(shard_path)
    _seed_shard(shard)
    shard.upsert_pi_record(
        CanonicalPIRecord(
            person_id="pi_example",
            display_name="Jane Doe",
            given_name="Jane",
            family_name="Doe",
            aliases=[],
            institution_id="inst_example",
            institution_name="Example University",
            ror_id=None,
            department="Computer Science",
            title="Professor",
            profile_url=source_url,
            lab_url=None,
            emails=[],
            research_areas=[],
            publications_summary={},
            external_ids={},
            source_evidence_ids=[],
            last_checked_at="2026-07-13T00:00:00+00:00",
            first_seen_at="2026-07-13T00:00:00+00:00",
            last_seen_at="2026-07-13T00:00:00+00:00",
            last_seen_run_id="run-example",
            profile_urls=[source_url],
        )
    )
    shard.upsert_publication_fingerprint(
        OfficialPublicationFingerprint(
            fingerprint_id=fingerprint_id,
            person_id="pi_example",
            institution_id="inst_example",
            title="A Reliable Publication Maintenance Workflow",
            citation_text="A Reliable Publication Maintenance Workflow, 2025.",
            source_url=source_url,
            run_id="run-example",
            first_seen_at="2026-07-13T00:00:00+00:00",
            last_seen_at="2026-07-13T00:00:00+00:00",
            last_seen_run_id="run-example",
            publication_year=2025,
        )
    )
    shard.reconcile_official_publication_claims(
        person_id="pi_example",
        institution_id="inst_example",
        source_url=source_url,
        run_id="run-example",
        observed_fingerprint_ids=[fingerprint_id],
        complete=True,
        enqueue_vectors=False,
    )

    target = PIIndexStorage(tmp_path / "publication-target.db")
    baseline = merge_database_shards(target, [shard_path])
    assert baseline["results"][0]["new_ingestion_run_ids"] == ["run-example"]
    assert target.publication_summary("pi_example")["official_fingerprint_count"] == 1

    refresh_run_id = "pubrefresh-inst-example-1"
    shard.start_publication_refresh_run(
        refresh_run_id,
        "inst_example",
        source_kind="official_profile",
        started_at="2026-07-14T00:00:00+00:00",
    )
    shard.reconcile_official_publication_claims(
        person_id="pi_example",
        institution_id="inst_example",
        source_url=source_url,
        run_id=refresh_run_id,
        observed_fingerprint_ids=[],
        complete=True,
        missing_runs_before_tombstone=1,
        observed_at="2026-07-14T00:00:00+00:00",
    )
    shard.upsert_publication_refresh_state(
        person_id="pi_example",
        institution_id="inst_example",
        source_url=source_url,
        body_sha256="changed-body",
        checked_at="2026-07-14T00:00:00+00:00",
        parser_name="publication-parser",
        parser_version="1",
        config_hash="config-v1",
        parse_status="success",
        parse_complete=True,
        publication_count=0,
        last_run_id=refresh_run_id,
    )
    shard.finish_publication_refresh_run(
        refresh_run_id,
        "success",
        {"tombstoned": 1},
        finished_at="2026-07-14T00:01:00+00:00",
    )
    shard.close()

    refreshed = merge_database_shards(target, [shard_path])
    repeated = merge_database_shards(target, [shard_path])

    result = refreshed["results"][0]
    assert result["new_ingestion_run_ids"] == []
    assert result["new_publication_refresh_run_ids"] == [refresh_run_id]
    assert result["rows_copied"]["publication_refresh_runs"] == 1
    assert result["rows_copied"]["official_publication_refresh_state"] == 1
    assert result["rows_copied"]["official_publication_source_claims"] == 1
    assert target.get_official_publication_source_claims(
        fingerprint_id=fingerprint_id
    )[0]["claim_status"] == "tombstoned"
    assert target.get_publication_refresh_state(
        "pi_example", source_url
    )["last_run_id"] == refresh_run_id
    assert target.publication_summary("pi_example")["official_fingerprint_count"] == 0

    jobs = target.iter_vector_dirty_queue()
    assert [(job["entity_kind"], job["entity_id"], job["reason"]) for job in jobs] == [
        ("openalex_works_sync", "pi_example", "publication_refresh_merged")
    ]
    assert repeated["shards_merged"] == 0
    assert repeated["results"][0]["status"] == "already_merged"
    assert target.conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 1
    assert target.conn.execute("SELECT COUNT(*) FROM publication_refresh_runs").fetchone()[0] == 1
    assert target.conn.execute("SELECT COUNT(*) FROM vector_dirty_queue").fetchone()[0] == 1
    target.close()
