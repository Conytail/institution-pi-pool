from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from pi_index.storage import PIIndexStorage


def _enqueue_jobs(db_path, count: int) -> None:
    storage = PIIndexStorage(db_path)
    try:
        for index in range(count):
            storage.enqueue_vector_dirty(
                "paper_vector_256",
                f"W{index}",
                "work_changed",
            )
    finally:
        storage.close()


def test_vector_queue_additive_lease_columns_migrate(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE vector_dirty_queue (
            queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_kind TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            person_id TEXT,
            fingerprint_id TEXT,
            reason TEXT NOT NULL,
            run_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            processed_at TEXT,
            last_error TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    connection.commit()
    connection.close()

    storage = PIIndexStorage(db_path)
    try:
        columns = {
            row["name"]
            for row in storage.conn.execute("PRAGMA table_info(vector_dirty_queue)")
        }
        assert {"claim_token", "claim_owner", "lease_expires_at"} <= columns
    finally:
        storage.close()


def test_two_connections_cannot_claim_the_same_jobs(tmp_path) -> None:
    db_path = tmp_path / "queue.db"
    _enqueue_jobs(db_path, 10)
    ready = threading.Barrier(2)

    def claim(owner: str) -> list[dict]:
        storage = PIIndexStorage(db_path)
        try:
            ready.wait(timeout=10)
            return storage.claim_vector_dirty_jobs(
                5,
                owner=owner,
                lease_seconds=60,
            )
        finally:
            storage.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(claim, "worker-a")
        second_future = pool.submit(claim, "worker-b")
        first = first_future.result(timeout=20)
        second = second_future.result(timeout=20)

    first_ids = {job["queue_id"] for job in first}
    second_ids = {job["queue_id"] for job in second}
    assert len(first) == len(second) == 5
    assert first_ids.isdisjoint(second_ids)
    assert len(first_ids | second_ids) == 10
    assert len({job["claim_token"] for job in first + second}) == 10
    assert {job["claim_owner"] for job in first} == {"worker-a"}
    assert {job["claim_owner"] for job in second} == {"worker-b"}


def test_finish_requires_active_token_but_same_instance_is_compatible(tmp_path) -> None:
    db_path = tmp_path / "finish.db"
    _enqueue_jobs(db_path, 1)
    owner = PIIndexStorage(db_path)
    stranger = PIIndexStorage(db_path)
    try:
        job = owner.claim_vector_dirty_jobs(1, owner="owner")[0]

        with pytest.raises(ValueError, match="claim_token"):
            stranger.finish_vector_dirty_job(job["queue_id"], success=True)
        with pytest.raises(RuntimeError, match="claim token"):
            stranger.finish_vector_dirty_job(
                job["queue_id"],
                success=True,
                claim_token="not-the-token",
            )

        # Backward-compatible same-instance call obtains the private token
        # cached when this storage instance claimed the row.
        finished = owner.finish_vector_dirty_job(job["queue_id"], success=True)
        assert finished["status"] == "completed"
        assert finished["claim_token"] is None
        assert finished["claim_owner"] is None
        assert finished["lease_expires_at"] is None
    finally:
        stranger.close()
        owner.close()


def test_expired_processing_lease_is_reclaimed_and_old_token_loses(tmp_path) -> None:
    db_path = tmp_path / "lease.db"
    _enqueue_jobs(db_path, 1)
    first_worker = PIIndexStorage(db_path)
    second_worker = PIIndexStorage(db_path)
    try:
        first_claim = first_worker.claim_vector_dirty_jobs(
            1,
            owner="worker-a",
            lease_seconds=3600,
        )[0]
        second_worker.conn.execute(
            """
            UPDATE vector_dirty_queue
            SET lease_expires_at='2000-01-01T00:00:00.000000+00:00'
            WHERE queue_id=?
            """,
            (first_claim["queue_id"],),
        )
        second_worker.conn.commit()

        second_claim = second_worker.claim_vector_dirty_jobs(
            1,
            owner="worker-b",
            lease_seconds=3600,
        )[0]
        assert second_claim["queue_id"] == first_claim["queue_id"]
        assert second_claim["claim_token"] != first_claim["claim_token"]
        assert second_claim["claim_owner"] == "worker-b"
        assert second_claim["attempts"] == 2

        with pytest.raises(RuntimeError, match="claim token"):
            first_worker.finish_vector_dirty_job(
                first_claim["queue_id"],
                success=True,
                claim_token=first_claim["claim_token"],
            )
        finished = second_worker.finish_vector_dirty_job(
            second_claim["queue_id"],
            success=True,
            claim_token=second_claim["claim_token"],
        )
        assert finished["status"] == "completed"
    finally:
        second_worker.close()
        first_worker.close()


def test_unexpired_processing_lease_is_not_reclaimed(tmp_path) -> None:
    db_path = tmp_path / "live-lease.db"
    _enqueue_jobs(db_path, 1)
    first_worker = PIIndexStorage(db_path)
    second_worker = PIIndexStorage(db_path)
    try:
        first_worker.claim_vector_dirty_jobs(
            1,
            owner="worker-a",
            lease_seconds=3600,
        )
        assert second_worker.claim_vector_dirty_jobs(1, owner="worker-b") == []
    finally:
        second_worker.close()
        first_worker.close()


def test_successful_openalex_sync_only_supersedes_pending_source_jobs(tmp_path) -> None:
    storage = PIIndexStorage(tmp_path / "source-jobs.db")
    try:
        pending = storage.enqueue_vector_dirty(
            "openalex_works_sync",
            "pi_1",
            "official_publication_claim_changed",
            person_id="pi_1",
            payload={"fingerprint": "fp_1"},
        )
        processing = storage.enqueue_vector_dirty(
            "openalex_works_sync",
            "pi_1_second",
            "official_publication_claim_changed",
            person_id="pi_1",
        )
        other = storage.enqueue_vector_dirty(
            "openalex_works_sync",
            "pi_2",
            "official_publication_claim_changed",
            person_id="pi_2",
        )
        claimed = storage.claim_vector_dirty_jobs(
            1,
            entity_kinds=["openalex_works_sync"],
        )[0]
        assert claimed["queue_id"] == pending["queue_id"]
        # Leave the first row actively processing; the other pi_1 row remains pending.
        completed = storage.complete_openalex_sync_jobs(
            "pi_1",
            "openalex_run_1",
            completed_at="2026-07-15T08:00:00+00:00",
        )

        assert completed == 1
        rows = {
            row["queue_id"]: row
            for row in storage.iter_vector_dirty_queue(status=None)
        }
        assert rows[pending["queue_id"]]["status"] == "processing"
        assert rows[processing["queue_id"]]["status"] == "completed"
        assert rows[processing["queue_id"]]["payload"] == {
            "completed_by_openalex_sync_at": "2026-07-15T08:00:00+00:00",
            "completed_by_openalex_sync_run_id": "openalex_run_1",
        }
        assert rows[other["queue_id"]]["status"] == "pending"
    finally:
        storage.close()


def test_complete_openalex_sync_jobs_requires_explicit_scope(tmp_path) -> None:
    storage = PIIndexStorage(tmp_path / "scope.db")
    try:
        with pytest.raises(ValueError, match="person_id and run_id"):
            storage.complete_openalex_sync_jobs("", "run")
        with pytest.raises(ValueError, match="person_id and run_id"):
            storage.complete_openalex_sync_jobs("pi_1", "")
    finally:
        storage.close()


def test_vector_claim_scope_selects_only_linked_papers_and_careers(tmp_path) -> None:
    storage = PIIndexStorage(tmp_path / "cohort-queue.db")
    try:
        now = "2026-07-15T00:00:00+00:00"
        storage.conn.executemany(
            """
            INSERT INTO openalex_person_works
            (person_id, openalex_work_id, institution_id, openalex_author_id,
             relationship_status, missing_streak, first_seen_at, last_seen_at,
             last_seen_run_id, last_checked_at, tombstoned_at, record_json)
            VALUES (?, ?, 'inst_1', 'A1', ?, 0, ?, ?, 'run', ?, NULL, '{}')
            """,
            [
                ("pi_1", "W1", "active", now, now, now),
                ("pi_2", "W2", "active", now, now, now),
                ("pi_1", "W3", "tombstoned", now, now, now),
            ],
        )
        storage.conn.commit()
        for kind, entity in (
            ("paper_vector_256", "W1"),
            ("paper_vector_256", "W2"),
            ("paper_vector_256", "W3"),
            ("career_vector_256", "pi_1"),
            ("career_vector_256", "pi_2"),
        ):
            storage.enqueue_vector_dirty(kind, entity, "test")

        claimed = storage.claim_vector_dirty_jobs(
            10,
            entity_kinds=["paper_vector_256", "career_vector_256"],
            scope_person_ids=["pi_1"],
        )

        assert {(job["entity_kind"], job["entity_id"]) for job in claimed} == {
            ("paper_vector_256", "W1"),
            ("career_vector_256", "pi_1"),
        }
    finally:
        storage.close()
