from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from pi_index.models import CanonicalPIRecord
from pi_index.pipeline.build_research_vectors import process_vector_queue
from pi_index.storage import PIIndexStorage


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "promote_faculty_pilot.py"
SPEC = importlib.util.spec_from_file_location("promote_faculty_pilot", SCRIPT)
assert SPEC and SPEC.loader
promotion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(promotion)


INSTITUTION_ID = "inst_1234567890abcdef"


def _pi(person_id: str, name: str) -> CanonicalPIRecord:
    return CanonicalPIRecord(
        person_id=person_id,
        display_name=name,
        given_name=None,
        family_name=None,
        aliases=[],
        institution_id=INSTITUTION_ID,
        institution_name="Example University",
        ror_id="https://ror.org/012345678",
        department="Business School",
        title="Professor",
        profile_url=f"https://example.edu/{person_id}",
        lab_url=None,
        emails=[f"{person_id}@example.edu"],
        research_areas=[],
        publications_summary={},
        external_ids={"orcid": f"0000-0000-0000-{person_id[-4:]}"},
        source_evidence_ids=[],
        last_checked_at="2026-07-15T00:00:00+00:00",
    )


def _build_pair(tmp_path: Path) -> tuple[Path, Path, Path, list[str]]:
    pilot_path = tmp_path / "pilot.db"
    target_path = tmp_path / "target.db"
    person_ids = ["pi_0000000000000001", "pi_0000000000000002"]
    pilot = PIIndexStorage(pilot_path)
    target = PIIndexStorage(target_path)
    try:
        for index, person_id in enumerate(person_ids, start=1):
            record = _pi(person_id, f"Researcher {index}")
            pilot.upsert_pi_record(record)
            target.upsert_pi_record(record)
            pilot.conn.execute(
                """
                INSERT INTO official_publication_fingerprints
                (fingerprint_id, person_id, institution_id, title, citation_text,
                 publication_year, doi, publication_url, source_url, confidence,
                 first_seen_at, last_seen_at, last_seen_run_id, record_json)
                VALUES (?, ?, ?, ?, ?, 2025, ?, ?, ?, 0.99, ?, ?, ?, '{}')
                """,
                (
                    f"fp_{index}",
                    person_id,
                    INSTITUTION_ID,
                    f"Official Paper {index}",
                    f"Official Paper {index}",
                    f"10.1000/{index}",
                    f"https://doi.org/10.1000/{index}",
                    f"https://example.edu/{person_id}",
                    "2026-07-15T00:00:00+00:00",
                    "2026-07-15T00:00:00+00:00",
                    "official-run",
                ),
            )
        pilot.conn.commit()
        pilot.start_openalex_sync_run(
            "oa-run",
            INSTITUTION_ID,
            sync_mode="selected_full",
            full_snapshot=True,
        )
        for index, person_id in enumerate(person_ids, start=1):
            author_id = f"A100{index}"
            work_id = f"W100{index}"
            pilot.upsert_openalex_author_link(
                person_id,
                INSTITUTION_ID,
                author_id,
                run_id="oa-run",
                last_successful_sync_at="2026-07-15T00:10:00+00:00",
                last_full_sync_at="2026-07-15T00:10:00+00:00",
                match_method="reviewed_openalex_identity_manifest_v1",
                evidence={
                    "official_fingerprint_id": f"fp_{index}",
                    "confirmed_openalex_author_ids": [author_id],
                    "reviewed_identity": {
                        "reviewed": True,
                        "audit_type": "reviewed_openalex_identity_manifest",
                        "schema_version": 1,
                        "manifest_sha256": "a" * 64,
                        "reason": "unit_test_review",
                        "reviewed_at": "2026-07-15T00:00:00+00:00",
                        "expected_display_name": f"Researcher {index}",
                        "institution_id": INSTITUTION_ID,
                        "primary_openalex_author_id": author_id,
                        "confirmed_openalex_author_ids": [author_id],
                        "sync_mode": "full_profile",
                        "coverage_limit": None,
                        "official_works": [],
                        "work_policy": None,
                        "work_policy_sha256": None,
                    },
                },
            )
            pilot.upsert_openalex_work(
                {
                    "id": work_id,
                    "doi": f"https://doi.org/10.1000/{index}",
                    "title": f"Official Paper {index}",
                    "abstract_inverted_index": {
                        "sustainable": [0],
                        "finance": [1],
                        str(index): [2],
                    },
                    "topics": [{"display_name": "Sustainable Finance"}],
                    "authorships": [
                        {
                            "author": {
                                "id": f"https://openalex.org/{author_id}",
                                "display_name": f"Researcher {index}",
                            },
                            "raw_author_name": f"Researcher {index}",
                        }
                    ],
                },
                "oa-run",
            )
            pilot.reconcile_openalex_person_works(
                person_id,
                INSTITUTION_ID,
                author_id,
                "oa-run",
                [work_id],
                full_snapshot=True,
            )
        pilot.finish_openalex_sync_run(
            "oa-run",
            "success",
            {
                "people": [
                    {
                        "person_id": person_id,
                        "status": "resolved",
                        "resolution": "reviewed_openalex_identity_manifest_v1",
                        "snapshot_audit": {
                            "authorship_validation": {
                                f"A100{index}": {
                                    "raw_work_count": 1,
                                    "accepted_work_count": 1,
                                    "rejected_work_count": 0,
                                    "rejected_fraction": 0.0,
                                    "rejected_reasons": {},
                                }
                            },
                            "compatible_union_work_count": 1,
                            "selected_union_work_count": 1,
                        },
                    }
                    for index, person_id in enumerate(person_ids, start=1)
                ]
            },
        )
        vector_report = process_vector_queue(pilot)
        assert vector_report["status"] == "success"
        assert vector_report["pending"] == 0
    finally:
        pilot.close()
        target.close()
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("\n".join(person_ids) + "\n", encoding="utf-8")
    return pilot_path, target_path, allowlist, person_ids


def _snapshot(path: Path) -> tuple[int, int, bytes]:
    return path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes()


def _set_reviewed_work_policy(
    database: Path,
    person_id: str,
    policy: dict,
    *,
    audit_policy_sha256: str | None = None,
    audit_selected_work_count: int = 1,
) -> str:
    policy_sha256 = promotion.hashlib.sha256(
        promotion._canonical_json(policy).encode("utf-8")
    ).hexdigest()
    connection = sqlite3.connect(database)
    try:
        evidence = json.loads(
            connection.execute(
                "SELECT evidence_json FROM openalex_author_links WHERE person_id=?",
                (person_id,),
            ).fetchone()[0]
        )
        evidence["reviewed_identity"]["work_policy"] = policy
        evidence["reviewed_identity"]["work_policy_sha256"] = policy_sha256
        connection.execute(
            "UPDATE openalex_author_links SET evidence_json=? WHERE person_id=?",
            (json.dumps(evidence, sort_keys=True), person_id),
        )
        metrics = json.loads(
            connection.execute(
                "SELECT metrics_json FROM openalex_sync_runs WHERE run_id='oa-run'"
            ).fetchone()[0]
        )
        person_result = next(
            person
            for person in metrics["people"]
            if person["person_id"] == person_id
        )
        person_result["snapshot_audit"]["reviewed_work_policy"] = {
            "mode": policy.get("mode"),
            "policy_sha256": audit_policy_sha256 or policy_sha256,
            "work_ids": list(policy.get("work_ids") or []),
            "raw_work_count": 1,
            "selected_work_count": audit_selected_work_count,
            "excluded_work_count": 0,
            "missing_work_ids": [],
        }
        connection.execute(
            "UPDATE openalex_sync_runs SET metrics_json=? WHERE run_id='oa-run'",
            (json.dumps(metrics, sort_keys=True),),
        )
        connection.commit()
    finally:
        connection.close()
    return policy_sha256


def _append_reviewed_work(database: Path, person_id: str) -> None:
    storage = PIIndexStorage(database)
    try:
        author_id = "A1001"
        run_id = "oa-run-update"
        work_id = "W2001"
        evidence = json.loads(
            storage.conn.execute(
                "SELECT evidence_json FROM openalex_author_links WHERE person_id=?",
                (person_id,),
            ).fetchone()[0]
        )
        storage.start_openalex_sync_run(
            run_id,
            INSTITUTION_ID,
            sync_mode="selected_full",
            full_snapshot=True,
        )
        storage.upsert_openalex_author_link(
            person_id,
            INSTITUTION_ID,
            author_id,
            run_id=run_id,
            last_successful_sync_at="2026-07-15T01:00:00+00:00",
            last_full_sync_at="2026-07-15T01:00:00+00:00",
            match_method="reviewed_openalex_identity_manifest_v1",
            evidence=evidence,
        )
        storage.upsert_openalex_work(
            {
                "id": work_id,
                "doi": "https://doi.org/10.1000/update",
                "title": "Reviewed Incremental Paper",
                "abstract_inverted_index": {
                    "incremental": [0],
                    "finance": [1],
                },
                "topics": [{"display_name": "Sustainable Finance"}],
                "authorships": [
                    {
                        "author": {
                            "id": f"https://openalex.org/{author_id}",
                            "display_name": "Researcher 1",
                        },
                        "raw_author_name": "Researcher 1",
                    }
                ],
            },
            run_id,
        )
        storage.reconcile_openalex_person_works(
            person_id,
            INSTITUTION_ID,
            author_id,
            run_id,
            ["W1001", work_id],
            full_snapshot=True,
        )
        storage.finish_openalex_sync_run(
            run_id,
            "success",
            {
                "people": [
                    {
                        "person_id": person_id,
                        "status": "resolved",
                        "resolution": "reviewed_openalex_identity_manifest_v1",
                        "snapshot_audit": {
                            "authorship_validation": {
                                author_id: {
                                    "raw_work_count": 2,
                                    "accepted_work_count": 2,
                                    "rejected_work_count": 0,
                                    "rejected_fraction": 0.0,
                                    "rejected_reasons": {},
                                }
                            },
                            "compatible_union_work_count": 2,
                            "selected_union_work_count": 2,
                        },
                    }
                ]
            },
        )
        report = process_vector_queue(storage)
        assert report["status"] == "success"
        assert report["pending"] == 0
    finally:
        storage.close()


def test_read_only_preflight_is_ready_and_does_not_touch_either_db(tmp_path: Path) -> None:
    pilot, target, allowlist, _people = _build_pair(tmp_path)
    before_pilot = _snapshot(pilot)
    before_target = _snapshot(target)

    report = promotion.preflight_promotion(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
        backup_db=tmp_path / "future-backup.db",
    )

    assert report["status"] == "ready"
    assert report["dry_run"] is True
    assert report["completion"]["people"] == 2
    assert report["completion"]["paper_vectors"] == 2
    assert report["completion"]["career_vectors"] == 2
    assert report["canonical_rows_to_modify"] == 0
    assert report["read_only_verified"] == {
        "pilot_unchanged": True,
        "target_unchanged": True,
    }
    assert _snapshot(pilot) == before_pilot
    assert _snapshot(target) == before_target

    exact = promotion.audit_faculty_enrichment(
        pilot,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
    )
    assert exact["pass"] is True
    assert exact["checks"]["reviewed_identity_policy_complete"] is True
    assert exact["checks"]["paper_vectors_complete_and_fresh"] is True
    assert exact["checks"]["career_vectors_complete_and_fresh"] is True
    assert exact["read_only_verified"] is True


def test_reviewed_exact_work_allowlist_is_promotable(tmp_path: Path) -> None:
    pilot, target, allowlist, people = _build_pair(tmp_path)
    _set_reviewed_work_policy(
        pilot,
        people[0],
        {"mode": "exact_work_allowlist", "work_ids": ["W1001"]},
    )

    report = promotion.preflight_promotion(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
    )

    assert report["status"] == "ready"
    exact = promotion.audit_faculty_enrichment(
        pilot,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
    )
    assert exact["pass"] is True
    assert exact["checks"]["reviewed_identity_policy_complete"] is True


@pytest.mark.parametrize(
    "work_ids",
    ([], ["A1001"], ["W1001", "W1001"]),
)
def test_invalid_reviewed_exact_work_allowlist_blocks(
    tmp_path: Path,
    work_ids: list[str],
) -> None:
    pilot, target, allowlist, people = _build_pair(tmp_path)
    _set_reviewed_work_policy(
        pilot,
        people[0],
        {"mode": "exact_work_allowlist", "work_ids": work_ids},
    )

    report = promotion.preflight_promotion(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
    )

    assert report["status"] == "blocked"
    assert any(
        "exact Work allowlist must contain unique OpenAlex Work IDs" in error
        for error in report["errors"]
    )


@pytest.mark.parametrize(
    ("audit_policy_sha256", "audit_selected_work_count"),
    (("f" * 64, 1), (None, 2)),
)
def test_any_reviewed_work_policy_requires_matching_snapshot_audit(
    tmp_path: Path,
    audit_policy_sha256: str | None,
    audit_selected_work_count: int,
) -> None:
    pilot, target, allowlist, people = _build_pair(tmp_path)
    _set_reviewed_work_policy(
        pilot,
        people[0],
        {"mode": "exact_work_allowlist", "work_ids": ["W1001"]},
        audit_policy_sha256=audit_policy_sha256,
        audit_selected_work_count=audit_selected_work_count,
    )

    report = promotion.preflight_promotion(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
    )

    assert report["status"] == "blocked"
    assert any(
        "reviewed Work-policy sync audit is missing or stale" in error
        for error in report["errors"]
    )


def test_apply_creates_backup_migrates_and_promotes_scoped_enrichment(tmp_path: Path) -> None:
    pilot, target, allowlist, people = _build_pair(tmp_path)
    original_target = target.read_bytes()
    # Exercise transactional schema creation, not only data UPSERTs.
    connection = sqlite3.connect(target)
    connection.execute("DROP TABLE openalex_work_vectors")
    connection.execute("DROP TABLE pi_career_vectors")
    connection.commit()
    connection.close()
    backup = tmp_path / "backups" / "target-before-promotion.db"

    report = promotion.promote_faculty_pilot(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
        backup_db=backup,
    )

    assert report["status"] == "committed"
    assert report["single_transaction"] is True
    assert report["canonical_rows_modified"] == 0
    assert backup.exists()
    backup_connection = sqlite3.connect(backup)
    try:
        assert backup_connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert backup_connection.execute(
            "SELECT COUNT(*) FROM canonical_pi_records"
        ).fetchone()[0] == 2
        assert backup_connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='openalex_work_vectors'"
        ).fetchone() is None
        assert backup_connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pi_career_vectors'"
        ).fetchone() is None
    finally:
        backup_connection.close()
    assert target.read_bytes() != original_target
    promoted = sqlite3.connect(target)
    try:
        assert promoted.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert promoted.execute("SELECT COUNT(*) FROM openalex_author_links").fetchone()[0] == 2
        assert promoted.execute("SELECT COUNT(*) FROM openalex_works").fetchone()[0] == 2
        assert promoted.execute("SELECT COUNT(*) FROM openalex_work_vectors").fetchone()[0] == 2
        assert promoted.execute("SELECT COUNT(*) FROM pi_career_vectors").fetchone()[0] == 2
        assert promoted.execute(
            "SELECT COUNT(*) FROM canonical_pi_records WHERE person_id IN (?, ?)", people
        ).fetchone()[0] == 2
        assert promoted.execute(
            "SELECT value FROM schema_meta WHERE key='faculty_pilot_promotion_schema'"
        ).fetchone()[0] == "2"
    finally:
        promoted.close()


def test_reviewed_append_only_enrichment_update_requires_explicit_flag(
    tmp_path: Path,
) -> None:
    pilot, target, allowlist, people = _build_pair(tmp_path)
    first = promotion.promote_faculty_pilot(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
        backup_db=tmp_path / "before-initial.db",
    )
    assert first["status"] == "committed"

    _append_reviewed_work(pilot, people[0])

    strict = promotion.preflight_promotion(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
    )
    assert strict["status"] == "blocked"
    assert any("target career vector differs" in error for error in strict["errors"])

    reviewed_update = promotion.preflight_promotion(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
        allow_reviewed_enrichment_update=True,
    )
    assert reviewed_update["status"] == "ready"
    assert reviewed_update["reviewed_enrichment_update"]["append_only"] is True
    assert reviewed_update["reviewed_enrichment_update"]["added_relationships"] == [
        [people[0], "W2001"]
    ]
    assert reviewed_update["reviewed_enrichment_update"]["affected_person_ids"] == [
        people[0]
    ]
    assert reviewed_update["reviewed_enrichment_update"]["target_baseline_pass"] is True

    second = promotion.promote_faculty_pilot(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
        backup_db=tmp_path / "before-update.db",
        allow_reviewed_enrichment_update=True,
    )
    assert second["status"] == "committed"
    connection = sqlite3.connect(target)
    source = sqlite3.connect(pilot)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM openalex_person_works "
            "WHERE person_id=? AND relationship_status='active'",
            (people[0],),
        ).fetchone()[0] == 2
        source_career = source.execute(
            "SELECT vector_hash, dependency_hash FROM pi_career_vectors WHERE person_id=?",
            (people[0],),
        ).fetchone()
        target_career = connection.execute(
            "SELECT vector_hash, dependency_hash FROM pi_career_vectors WHERE person_id=?",
            (people[0],),
        ).fetchone()
        assert target_career == source_career
    finally:
        source.close()
        connection.close()


def test_reviewed_official_work_only_identity_pending_is_promotable(tmp_path: Path) -> None:
    pilot, target, allowlist, people = _build_pair(tmp_path)
    pending_person = people[1]
    connection = sqlite3.connect(pilot)
    reviewed_identity = {
        "reviewed": True,
        "audit_type": "reviewed_openalex_identity_manifest",
        "schema_version": 1,
        "manifest_sha256": "b" * 64,
        "reason": "unit_test_official_work_only",
        "reviewed_at": "2026-07-15T00:00:00+00:00",
        "expected_display_name": "Researcher 2",
        "institution_id": INSTITUTION_ID,
        "primary_openalex_author_id": None,
        "confirmed_openalex_author_ids": [],
        "sync_mode": "official_evidence_only",
        "coverage_limit": "reviewed_official_evidence_only",
        "official_works": [
            {
                "openalex_work_id": "W1002",
                "doi": "10.1000/2",
                "expected_title": "Official Paper 2",
                "expected_openalex_title": "Official Paper 2",
            }
        ],
        "work_policy": None,
        "work_policy_sha256": None,
    }
    relationship_evidence = {
        "relationship_method": "reviewed_official_evidence_only",
        "identity_status": "pending",
        "coverage_limit": "reviewed_official_evidence_only",
        "reviewed_identity": reviewed_identity,
    }
    relationship = {
        "person_id": pending_person,
        "openalex_work_id": "W1002",
        "institution_id": INSTITUTION_ID,
        "openalex_author_id": None,
        "relationship_status": "active",
        "missing_streak": 0,
        "first_seen_at": "2026-07-15T00:10:00+00:00",
        "last_seen_at": "2026-07-15T00:10:00+00:00",
        "last_seen_run_id": "oa-run",
        "last_checked_at": "2026-07-15T00:10:00+00:00",
        "tombstoned_at": None,
        "relationship_evidence": relationship_evidence,
    }
    connection.execute("DELETE FROM openalex_author_links WHERE person_id=?", (pending_person,))
    connection.execute(
        "UPDATE openalex_person_works SET openalex_author_id=NULL, record_json=? "
        "WHERE person_id=?",
        (json.dumps(relationship, sort_keys=True), pending_person),
    )
    metrics = json.loads(
        connection.execute(
            "SELECT metrics_json FROM openalex_sync_runs WHERE run_id='oa-run'"
        ).fetchone()[0]
    )
    for person in metrics["people"]:
        if person["person_id"] == pending_person:
            person.update(
                resolution="reviewed_openalex_identity_manifest_v1",
                sync_mode="official_evidence_only",
                identity_status="pending",
                works_selected=1,
            )
    connection.execute(
        "UPDATE openalex_sync_runs SET metrics_json=? WHERE run_id='oa-run'",
        (json.dumps(metrics, sort_keys=True),),
    )
    connection.commit()
    connection.close()
    storage = PIIndexStorage(pilot)
    try:
        storage.enqueue_vector_dirty(
            "openalex_works_sync",
            pending_person,
            "identity_refresh_pending",
            person_id=pending_person,
        )
    finally:
        storage.close()

    preflight = promotion.preflight_promotion(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
    )

    assert preflight["status"] == "ready"
    assert preflight["completion"]["confirmed_author_links"] == 1
    assert preflight["completion"]["identity_pending_work_only_people"] == 1

    backup = tmp_path / "pending-identity-backup.db"
    report = promotion.promote_faculty_pilot(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
        backup_db=backup,
    )
    assert report["status"] == "committed"
    promoted = sqlite3.connect(target)
    try:
        row = promoted.execute(
            "SELECT openalex_author_id, record_json FROM openalex_person_works "
            "WHERE person_id=?",
            (pending_person,),
        ).fetchone()
        assert row[0] is None
        assert json.loads(row[1])["relationship_evidence"]["identity_status"] == "pending"
        assert promoted.execute(
            "SELECT COUNT(*) FROM vector_dirty_queue "
            "WHERE entity_kind='openalex_works_sync' AND entity_id=? "
            "AND status='pending'",
            (pending_person,),
        ).fetchone()[0] == 1
    finally:
        promoted.close()


def test_identity_conflict_blocks_without_writes_or_backup(tmp_path: Path) -> None:
    pilot, target, allowlist, _people = _build_pair(tmp_path)
    connection = sqlite3.connect(target)
    connection.execute(
        "UPDATE canonical_pi_records SET display_name='Different Person' "
        "WHERE person_id='pi_0000000000000001'"
    )
    connection.commit()
    connection.close()
    before = _snapshot(target)
    backup = tmp_path / "must-not-exist.db"

    report = promotion.preflight_promotion(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
        backup_db=backup,
    )

    assert report["status"] == "blocked"
    assert any("canonical name differs" in error for error in report["errors"])
    with pytest.raises(promotion.PromotionError, match="Preflight blocked"):
        promotion.promote_faculty_pilot(
            pilot,
            target,
            allowlist,
            institution_id=INSTITUTION_ID,
            expected_count=2,
            backup_db=backup,
        )
    assert not backup.exists()
    assert _snapshot(target) == before


def test_stale_paper_vector_blocks_promotion(tmp_path: Path) -> None:
    pilot, target, allowlist, _people = _build_pair(tmp_path)
    connection = sqlite3.connect(pilot)
    connection.execute(
        "UPDATE openalex_work_vectors SET source_text_hash=? WHERE openalex_work_id='W1001'",
        ("0" * 64,),
    )
    connection.commit()
    connection.close()

    report = promotion.preflight_promotion(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
    )

    assert report["status"] == "blocked"
    assert any("paper vector is stale" in error for error in report["errors"])


def test_multi_profile_provenance_is_accepted_but_historical_pollution_is_not_copied(
    tmp_path: Path,
) -> None:
    pilot, target, allowlist, people = _build_pair(tmp_path)
    person_id = people[0]
    secondary = "A2001"
    storage = PIIndexStorage(pilot)
    try:
        link = storage.conn.execute(
            "SELECT evidence_json FROM openalex_author_links WHERE person_id=?",
            (person_id,),
        ).fetchone()
        evidence = json.loads(link[0])
        evidence["confirmed_openalex_author_ids"] = ["A1001", secondary]
        evidence["reviewed_identity"]["confirmed_openalex_author_ids"] = [
            "A1001",
            secondary,
        ]
        storage.conn.execute(
            "UPDATE openalex_author_links SET evidence_json=? WHERE person_id=?",
            (json.dumps(evidence, sort_keys=True), person_id),
        )
        work = json.loads(
            storage.conn.execute(
                "SELECT raw_json FROM openalex_works WHERE openalex_work_id='W1001'"
            ).fetchone()[0]
        )
        work["authorships"] = [
            {
                "author": {
                    "id": f"https://openalex.org/{secondary}",
                    "display_name": "Researcher 1",
                }
            }
        ]
        storage.conn.execute(
            "UPDATE openalex_works SET raw_json=? WHERE openalex_work_id='W1001'",
            (json.dumps(work, sort_keys=True),),
        )
        storage.conn.execute(
            "UPDATE openalex_person_works SET openalex_author_id=? "
            "WHERE person_id=? AND openalex_work_id='W1001'",
            (secondary, person_id),
        )
        metrics = json.loads(
            storage.conn.execute(
                "SELECT metrics_json FROM openalex_sync_runs WHERE run_id='oa-run'"
            ).fetchone()[0]
        )
        for person in metrics["people"]:
            if person["person_id"] == person_id:
                person["snapshot_audit"]["authorship_validation"][secondary] = {
                    "raw_work_count": 1,
                    "accepted_work_count": 1,
                    "rejected_work_count": 0,
                    "rejected_fraction": 0.0,
                    "rejected_reasons": {},
                }
        storage.conn.execute(
            "UPDATE openalex_sync_runs SET metrics_json=? WHERE run_id='oa-run'",
            (json.dumps(metrics, sort_keys=True),),
        )
        storage.upsert_openalex_work(
            {
                "id": "W9999",
                "title": "Historical polluted work",
                "abstract_inverted_index": {"pollution": [0]},
                "authorships": [
                    {
                        "author": {
                            "id": "https://openalex.org/A9999",
                            "display_name": "Different Person",
                        }
                    }
                ],
            },
            "oa-run",
        )
        storage.conn.execute(
            """
            INSERT INTO openalex_person_works
            (person_id, openalex_work_id, institution_id, openalex_author_id,
             relationship_status, missing_streak, first_seen_at, last_seen_at,
             last_seen_run_id, last_checked_at, tombstoned_at, record_json)
            VALUES (?, 'W9999', ?, 'A9999', 'tombstoned', 1, ?, ?, 'oa-run', ?, ?, '{}')
            """,
            (
                person_id,
                INSTITUTION_ID,
                "2026-07-15T00:00:00+00:00",
                "2026-07-15T00:00:00+00:00",
                "2026-07-15T00:00:00+00:00",
                "2026-07-15T00:00:00+00:00",
            ),
        )
        storage.conn.commit()
        process_vector_queue(storage)
    finally:
        storage.close()

    preflight = promotion.preflight_promotion(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
    )
    assert preflight["status"] == "ready"
    assert preflight["completion"]["excluded_historical_person_work_relationships"] == 1

    promotion.promote_faculty_pilot(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
        backup_db=tmp_path / "clean-scope-backup.db",
    )
    promoted = sqlite3.connect(target)
    try:
        assert promoted.execute(
            "SELECT openalex_author_id FROM openalex_person_works "
            "WHERE person_id=? AND openalex_work_id='W1001'",
            (person_id,),
        ).fetchone()[0] == secondary
        assert promoted.execute(
            "SELECT COUNT(*) FROM openalex_works WHERE openalex_work_id='W9999'"
        ).fetchone()[0] == 0
        assert promoted.execute(
            "SELECT COUNT(*) FROM openalex_work_vectors WHERE openalex_work_id='W9999'"
        ).fetchone()[0] == 0
    finally:
        promoted.close()


def test_unreviewed_identity_and_false_per_work_provenance_block(tmp_path: Path) -> None:
    pilot, target, allowlist, people = _build_pair(tmp_path)
    connection = sqlite3.connect(pilot)
    connection.execute(
        "UPDATE openalex_author_links SET match_method='automatic', evidence_json='{}' "
        "WHERE person_id=?",
        (people[0],),
    )
    connection.commit()
    connection.close()
    unreviewed = promotion.preflight_promotion(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
    )
    assert unreviewed["status"] == "blocked"
    assert any("not human-reviewed" in error for error in unreviewed["errors"])

    provenance_dir = tmp_path / "provenance"
    provenance_dir.mkdir()
    pilot, target, allowlist, people = _build_pair(provenance_dir)
    connection = sqlite3.connect(pilot)
    evidence = json.loads(
        connection.execute(
            "SELECT evidence_json FROM openalex_author_links WHERE person_id=?",
            (people[0],),
        ).fetchone()[0]
    )
    evidence["confirmed_openalex_author_ids"] = ["A1001", "A2001"]
    evidence["reviewed_identity"]["confirmed_openalex_author_ids"] = [
        "A1001",
        "A2001",
    ]
    connection.execute(
        "UPDATE openalex_author_links SET evidence_json=? WHERE person_id=?",
        (json.dumps(evidence, sort_keys=True), people[0]),
    )
    connection.execute(
        "UPDATE openalex_person_works SET openalex_author_id='A2001' WHERE person_id=?",
        (people[0],),
    )
    connection.commit()
    connection.close()
    false_provenance = promotion.preflight_promotion(
        pilot,
        target,
        allowlist,
        institution_id=INSTITUTION_ID,
        expected_count=2,
    )
    assert false_provenance["status"] == "blocked"
    assert any("absent from the stored OpenAlex authorship" in error for error in false_provenance["errors"])


def test_cli_defaults_to_dry_run_and_apply_requires_backup(tmp_path: Path) -> None:
    parser = promotion._parser()
    args = parser.parse_args(
        [
            "--pilot-db",
            "pilot.db",
            "--target-db",
            "target.db",
            "--person-allowlist",
            "people.txt",
            "--institution-id",
            INSTITUTION_ID,
        ]
    )
    assert args.apply is False
    assert args.expected_count == 124
    assert args.out is None
    with pytest.raises(promotion.PromotionError, match="requires --backup-db"):
        promotion._safe_backup_path(
            None,
            source_db=tmp_path / "source.db",
            target_db=tmp_path / "target.db",
            required=True,
        )
