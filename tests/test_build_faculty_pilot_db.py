from __future__ import annotations

import gzip
import hashlib
import importlib.util
from pathlib import Path
import sqlite3

import pytest

from pi_index.models import (
    CanonicalPIRecord,
    InstitutionRecord,
    OfficialPublicationFingerprint,
    RawSourceRecord,
)
from pi_index.crawl.archive import ContentArchive
from pi_index.storage import PIIndexStorage


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_faculty_pilot_db.py"
SPEC = importlib.util.spec_from_file_location("build_faculty_pilot_db", SCRIPT_PATH)
assert SPEC and SPEC.loader
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


def _pi(
    person_id: str,
    institution_id: str,
    institution_name: str,
    department: str,
    *,
    active: bool = True,
) -> CanonicalPIRecord:
    profile = f"https://{institution_id}.example.edu/people/{person_id}"
    return CanonicalPIRecord(
        person_id=person_id,
        display_name=person_id.replace("_", " ").title(),
        given_name=None,
        family_name=None,
        aliases=[],
        institution_id=institution_id,
        institution_name=institution_name,
        ror_id=None,
        department=department,
        title="Professor",
        profile_url=profile,
        lab_url=None,
        emails=[],
        research_areas=[],
        publications_summary={},
        external_ids={},
        source_evidence_ids=[],
        last_checked_at="2026-07-15T00:00:00+00:00",
        first_seen_at="2026-07-15T00:00:00+00:00",
        last_seen_at="2026-07-15T00:00:00+00:00",
        last_seen_run_id="ingest-1",
        membership_status="active" if active else "inactive",
        profile_urls=[profile],
        departments=[department],
        pool_scope="Target Faculty",
    )


def _source_database(path: Path) -> tuple[str, bytes]:
    storage = PIIndexStorage(path)
    archived_body = b"<html><h1>Alice Engineering</h1><h2>Publications</h2></html>"
    archive_entry = ContentArchive(path.parent / "raw_sources").store(archived_body)
    storage.upsert_institution(
        InstitutionRecord(
            institution_id="inst_target",
            name="Target University",
            aliases=["Target U"],
            official_domains=["inst_target.example.edu"],
        )
    )
    storage.upsert_institution(
        InstitutionRecord(
            institution_id="inst_other",
            name="Other University",
            official_domains=["inst_other.example.edu"],
        )
    )
    people = [
        _pi("alice_eng", "inst_target", "Target University", "Faculty of Engineering"),
        _pi("bob_compute", "inst_target", "Target University", "School of Computing"),
        _pi("carol_business", "inst_target", "Target University", "Business School"),
        _pi(
            "dan_inactive",
            "inst_target",
            "Target University",
            "Faculty of Engineering",
            active=False,
        ),
        _pi("erin_other", "inst_other", "Other University", "Faculty of Engineering"),
    ]
    for record in people:
        storage.upsert_pi_record(record)
        is_archived = record.person_id == "alice_eng"
        storage.insert_raw_source(
            RawSourceRecord(
                source_url=record.profile_url,
                final_url=record.profile_url,
                source_type="official_profile",
                institution_id=record.institution_id,
                fetched_at="2026-07-15T00:00:00+00:00",
                http_status=200,
                content_hash=(
                    archive_entry.body_sha256
                    if is_archived
                    else f"hash-{record.person_id}"
                ),
                body_sha256=archive_entry.body_sha256 if is_archived else None,
                archive_key=archive_entry.archive_key if is_archived else None,
                uncompressed_bytes=archive_entry.uncompressed_bytes if is_archived else 0,
                compressed_bytes=archive_entry.compressed_bytes if is_archived else 0,
            )
        )

    alice = people[0]
    storage.upsert_publication_fingerprint(
        OfficialPublicationFingerprint(
            fingerprint_id="pubfp_alice",
            person_id=alice.person_id,
            institution_id=alice.institution_id,
            title="Reliable Engineering Systems for Faculty Pilots",
            citation_text="Reliable Engineering Systems for Faculty Pilots, 2025.",
            source_url=alice.profile_url,
            run_id="ingest-1",
            first_seen_at="2026-07-15T00:00:00+00:00",
            last_seen_at="2026-07-15T00:00:00+00:00",
            last_seen_run_id="ingest-1",
            publication_year=2025,
        )
    )
    storage.start_openalex_sync_run(
        "oa-run-1", "inst_target", sync_mode="full", full_snapshot=True
    )
    storage.upsert_openalex_author_link(
        alice.person_id,
        alice.institution_id,
        "A123",
        confidence=0.99,
        match_method="test",
        run_id="oa-run-1",
        last_successful_sync_at="2026-07-15T00:00:00+00:00",
    )
    storage.upsert_openalex_work(
        {
            "id": "W123",
            "title": "Complete OpenAlex Work",
            "publication_year": 2025,
            "abstract_inverted_index": {"Complete": [0], "abstract": [1]},
        },
        "oa-run-1",
        enqueue_vectors=False,
    )
    storage.reconcile_openalex_person_works(
        alice.person_id,
        alice.institution_id,
        "A123",
        "oa-run-1",
        ["W123"],
        full_snapshot=True,
        enqueue_vectors=False,
    )
    storage.finish_openalex_sync_run("oa-run-1", "success")
    storage.close()
    return archive_entry.archive_key, archived_body


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def test_build_faculty_pilot_is_scoped_complete_and_source_read_only(tmp_path):
    source = tmp_path / "regional.db"
    output = tmp_path / "pilot" / "engineering-pilot.db"
    archive_key, archived_body = _source_database(source)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    report = pilot.build_faculty_pilot_db(
        source,
        output,
        institution_name="Target U",
        department_contains=["engineering", "computing"],
    )

    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    assert report["selected_person_ids"] == ["alice_eng", "bob_compute"]
    assert report["counts"]["institutions"] == 1
    assert report["counts"]["canonical_pi_records"] == 2
    assert report["counts"]["raw_sources"] == 2
    assert report["legacy_publication_backfill"] == {
        "claims_created": 1,
        "incomplete_states_created": 1,
    }
    assert report["integrity"]["ok"] is True
    assert report["archive"]["self_contained"] is True
    assert report["archive"]["referenced_blobs"] == 1
    assert report["archive"]["copied_blobs"] == 1
    assert report["archive"]["reused_blobs"] == 0
    assert report["archive"]["copied_compressed_bytes"] > 0
    assert ContentArchive(output.parent / "raw_sources").read(archive_key) == archived_body

    connection = _read_only(output)
    try:
        assert {
            row[0] for row in connection.execute("SELECT institution_id FROM institutions")
        } == {"inst_target"}
        assert {
            row[0]
            for row in connection.execute("SELECT person_id FROM canonical_pi_records")
        } == {"alice_eng", "bob_compute"}
        assert connection.execute(
            "SELECT COUNT(*) FROM official_publication_fingerprints"
        ).fetchone()[0] == 1
        claim = connection.execute(
            "SELECT claim_status FROM official_publication_source_claims"
        ).fetchone()
        state = connection.execute(
            "SELECT parse_status, parse_complete FROM official_publication_refresh_state"
        ).fetchone()
        assert claim["claim_status"] == "active"
        assert dict(state) == {
            "parse_status": "pilot_legacy_imported",
            "parse_complete": 0,
        }
        assert tuple(connection.execute(
            "SELECT person_id, openalex_author_id FROM openalex_author_links"
        ).fetchone()) == ("alice_eng", "A123")
        assert connection.execute(
            "SELECT openalex_work_id FROM openalex_works"
        ).fetchone()[0] == "W123"
        assert tuple(connection.execute(
            "SELECT person_id, openalex_work_id FROM openalex_person_works"
        ).fetchone()) == ("alice_eng", "W123")
        assert {
            row[0] for row in connection.execute("SELECT source_url FROM raw_sources")
        } == {
            "https://inst_target.example.edu/people/alice_eng",
            "https://inst_target.example.edu/people/bob_compute",
        }
    finally:
        connection.close()

    reused = pilot.build_faculty_pilot_db(
        source,
        output.with_name("engineering-pilot-reused.db"),
        institution_name="Target University",
        department_contains=["engineering"],
    )
    assert reused["archive"]["copied_blobs"] == 0
    assert reused["archive"]["reused_blobs"] == 1


def test_output_replacement_is_explicit_and_never_replaces_source(tmp_path):
    source = tmp_path / "regional.db"
    output = tmp_path / "pilot.db"
    _source_database(source)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    output.write_text("keep me", encoding="utf-8")

    with pytest.raises(FileExistsError):
        pilot.build_faculty_pilot_db(
            source,
            output,
            institution_id="inst_target",
            department_contains=["engineering"],
        )
    assert output.read_text(encoding="utf-8") == "keep me"

    report = pilot.build_faculty_pilot_db(
        source,
        output,
        institution_id="inst_target",
        department_contains=["engineering"],
        replace=True,
    )
    assert report["integrity"]["ok"] is True
    connection = _read_only(output)
    try:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        connection.close()

    with pytest.raises(ValueError, match="must not be the source"):
        pilot.build_faculty_pilot_db(
            source,
            source,
            institution_id="inst_target",
            department_contains=["engineering"],
            replace=True,
        )
    assert source.exists()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash

    directory = tmp_path / "not-a-file.db"
    directory.mkdir()
    with pytest.raises(ValueError, match="ordinary file"):
        pilot.build_faculty_pilot_db(
            source,
            directory,
            institution_id="inst_target",
            department_contains=["engineering"],
            replace=True,
        )


def test_no_matching_faculty_does_not_create_output(tmp_path):
    source = tmp_path / "regional.db"
    output = tmp_path / "empty.db"
    _source_database(source)

    with pytest.raises(ValueError, match="No active PI matched"):
        pilot.build_faculty_pilot_db(
            source,
            output,
            institution_id="inst_target",
            department_contains=["Faculty of Dentistry"],
        )

    assert not output.exists()


def test_archive_hash_conflict_fails_before_pilot_database_replacement(tmp_path):
    source = tmp_path / "regional.db"
    archive_key, _ = _source_database(source)
    output = tmp_path / "conflict" / "pilot.db"
    output.parent.mkdir()
    output.write_text("old pilot", encoding="utf-8")
    target_archive = output.parent / "raw_sources"
    conflicting_blob = target_archive / archive_key
    conflicting_blob.parent.mkdir(parents=True)
    conflicting_blob.write_bytes(gzip.compress(b"wrong body", mtime=0))

    with pytest.raises(ValueError, match="hash conflict"):
        pilot.build_faculty_pilot_db(
            source,
            output,
            institution_id="inst_target",
            department_contains=["engineering"],
            replace=True,
        )

    assert output.read_text(encoding="utf-8") == "old pilot"
