from __future__ import annotations

import json
import math

import pytest

from pi_index.index.vector_index import (
    ProductionTermVectorIndex,
    aggregate_career_vector,
    encode_publication_text,
    normalize_sparse_vector,
)
from pi_index.eval.research_profile_experiment import normalize_vector
from pi_index.match.semantic import semantic_vector
from pi_index.models import CanonicalPIRecord
from pi_index.pipeline.build_research_vectors import process_vector_queue
from pi_index.storage import PIIndexStorage


def _pi(person_id: str = "pi_1") -> CanonicalPIRecord:
    return CanonicalPIRecord(
        person_id=person_id,
        display_name=f"Researcher {person_id}",
        given_name=None,
        family_name=None,
        aliases=[],
        institution_id="inst_1",
        institution_name="Institution One",
        ror_id="https://ror.org/012345678",
        department="Business School",
        title="Professor",
        profile_url=f"https://example.edu/{person_id}",
        lab_url=None,
        emails=[],
        research_areas=[],
        publications_summary={},
        external_ids={},
        source_evidence_ids=[],
        last_checked_at="2026-07-15T00:00:00+00:00",
    )


def _norm(vector: dict[str, float]) -> float:
    return math.sqrt(sum(value * value for value in vector.values()))


def test_production_term_backend_is_sparse_deterministic_and_normalized() -> None:
    text = "Title: climate finance risk\nAbstract: climate transition risk and green bonds"
    first = encode_publication_text(text)
    second = encode_publication_text(text)

    assert first == second
    assert 0 < len(first) <= 256
    assert _norm(first) == pytest.approx(1.0)
    assert encode_publication_text("") == {}

    frozen_experiment_vector = normalize_vector(
        semantic_vector(text, max_features=1024),
        256,
    )
    assert first == pytest.approx(frozen_experiment_vector)


def test_normalization_uses_top_absolute_features_with_stable_ties() -> None:
    vector = normalize_sparse_vector({"z": 2.0, "a": 2.0, "m": 1.0}, 2)
    assert list(vector) == ["a", "z"]
    assert vector == pytest.approx({"a": 2**-0.5, "z": 2**-0.5})


def test_career_vector_is_equal_weight_normalized_centroid() -> None:
    career = aggregate_career_vector([{"a": 1.0}, {"b": 1.0}])
    assert career == pytest.approx({"a": 2**-0.5, "b": 2**-0.5})


def test_vector_storage_rejects_stale_source_hash(tmp_path) -> None:
    storage = PIIndexStorage(tmp_path / "vectors.db")
    try:
        storage.upsert_openalex_work(
            {"id": "W101", "title": "Climate finance"},
            "run-1",
            enqueue_vectors=False,
        )
        row = storage.conn.execute(
            "SELECT vector_text_hash FROM openalex_works WHERE openalex_work_id='W101'"
        ).fetchone()
        with pytest.raises(RuntimeError, match="changed while encoding"):
            storage.upsert_openalex_work_vector(
                "W101",
                {"climate": 1.0},
                "0" * 64,
            )
        stored = storage.upsert_openalex_work_vector(
            "W101",
            {"climate": 1.0},
            row["vector_text_hash"],
        )
        assert stored["feature_count"] == 1
        assert stored["vector"] == {"climate": 1.0}
        assert len(stored["vector_hash"]) == 64
    finally:
        storage.close()


def test_queue_worker_builds_global_papers_and_pi_career_incrementally(tmp_path) -> None:
    storage = PIIndexStorage(tmp_path / "vectors.db")
    try:
        storage.upsert_pi_record(_pi())
        storage.upsert_openalex_author_link("pi_1", "inst_1", "A101")
        for work_id, title in (
            ("W101", "Climate transition risk in financial markets"),
            ("W102", "Green bonds and sustainable corporate finance"),
        ):
            storage.upsert_openalex_work(
                {
                    "id": work_id,
                    "title": title,
                    "abstract_inverted_index": {
                        "financial": [0],
                        "risk": [1],
                        "climate": [2],
                    },
                    "topics": [{"display_name": "Sustainable Finance"}],
                },
                "run-1",
            )
        storage.reconcile_openalex_person_works(
            "pi_1",
            "inst_1",
            "A101",
            "run-1",
            ["W101", "W102"],
            full_snapshot=True,
        )
        # This is publication-maintenance work, not a vector job.  The vector
        # worker must leave it untouched in the shared queue.
        storage.enqueue_vector_dirty(
            "openalex_works_sync",
            "pi_1",
            "official_profile_changed",
            person_id="pi_1",
        )

        report = process_vector_queue(storage, batch_size=2)

        assert report["status"] == "success"
        assert report["pending"] == 0
        assert storage.conn.execute(
            "SELECT COUNT(*) FROM openalex_work_vectors"
        ).fetchone()[0] == 2
        assert storage.conn.execute(
            "SELECT COUNT(*) FROM pi_career_vectors"
        ).fetchone()[0] == 1
        assert storage.conn.execute(
            """
            SELECT status FROM vector_dirty_queue
            WHERE entity_kind='openalex_works_sync'
            """
        ).fetchone()[0] == "pending"

        career = storage.get_pi_career_vector("pi_1")
        assert career is not None
        assert career["encoder_id"] == ProductionTermVectorIndex.encoder_id
        assert career["feature_limit"] == 256
        assert career["work_count"] == 2
        assert career["nonempty_work_count"] == 2
        assert career["feature_count"] <= 256
        assert _norm(career["vector"]) == pytest.approx(1.0)
        first_dependency = career["dependency_hash"]

        # A changed Work text must replace its global paper vector and then
        # rebuild the linked PI dependency rather than re-encoding everything.
        storage.upsert_openalex_work(
            {
                "id": "W101",
                "title": "Climate transition risk and carbon disclosure",
                "abstract_inverted_index": {"carbon": [0], "disclosure": [1]},
            },
            "run-2",
        )
        second = process_vector_queue(storage)
        assert second["paper_built"] == 1
        updated = storage.get_pi_career_vector("pi_1")
        assert updated is not None
        assert updated["dependency_hash"] != first_dependency
        assert updated["work_count"] == 2

        # Stored JSON is deterministic and exactly agrees with feature_count.
        for row in storage.conn.execute(
            "SELECT vector_json, feature_count FROM openalex_work_vectors"
        ):
            assert len(json.loads(row["vector_json"])) == row["feature_count"]
    finally:
        storage.close()


def test_reviewed_official_work_without_author_id_builds_paper_and_career_vectors(
    tmp_path,
) -> None:
    storage = PIIndexStorage(tmp_path / "reviewed-work.db")
    try:
        storage.upsert_pi_record(_pi())
        storage.upsert_openalex_work(
            {
                "id": "W901",
                "title": "International business evidence",
                "abstract_inverted_index": {"international": [0], "business": [1]},
            },
            "review-run",
        )
        storage.reconcile_openalex_person_works(
            "pi_1",
            "inst_1",
            None,
            "review-run",
            ["W901"],
            full_snapshot=False,
            relationship_evidence={
                "relationship_method": "reviewed_official_evidence_only",
                "identity_status": "pending",
                "coverage_limit": "reviewed_official_evidence_only",
                "manifest_sha256": "a" * 64,
            },
        )

        report = process_vector_queue(storage)

        assert report["status"] == "success"
        relationship = storage.conn.execute(
            "SELECT openalex_author_id, record_json FROM openalex_person_works"
        ).fetchone()
        assert relationship["openalex_author_id"] is None
        assert json.loads(relationship["record_json"])["relationship_evidence"][
            "identity_status"
        ] == "pending"
        assert storage.conn.execute(
            "SELECT COUNT(*) FROM openalex_work_vectors"
        ).fetchone()[0] == 1
        assert storage.get_pi_career_vector("pi_1")["work_count"] == 1
        assert storage.get_openalex_author_link("pi_1") is None
    finally:
        storage.close()


def test_identity_pending_reconcile_atomically_archives_only_automatic_author_link(
    tmp_path,
) -> None:
    storage = PIIndexStorage(tmp_path / "automatic-link-archive.db")
    reviewed_identity = {
        "reviewed": True,
        "sync_mode": "official_evidence_only",
        "primary_openalex_author_id": None,
        "confirmed_openalex_author_ids": [],
        "manifest_sha256": "a" * 64,
    }
    relationship_evidence = {
        "relationship_method": "reviewed_official_evidence_only",
        "identity_status": "pending",
        "coverage_limit": "reviewed_official_evidence_only",
        "reviewed_identity": reviewed_identity,
    }
    archive_request = {
        "expected_openalex_author_id": "A999",
        "reason": "superseded_by_reviewed_official_evidence_only_identity_pending",
    }
    try:
        storage.upsert_pi_record(_pi())
        storage.upsert_openalex_author_link(
            "pi_1",
            "inst_1",
            "A999",
            match_method="exact_name_ror_and_official_publication_overlap",
            evidence={
                "primary_openalex_author_id": "A999",
                "confirmed_openalex_author_ids": ["A999"],
            },
        )
        for work_id in ("W900", "W901"):
            storage.upsert_openalex_work(
                {"id": work_id, "title": f"Publication {work_id}"},
                "setup-run",
                enqueue_vectors=False,
            )
        storage.reconcile_openalex_person_works(
            "pi_1",
            "inst_1",
            "A999",
            "setup-run",
            ["W900"],
            full_snapshot=True,
            enqueue_vectors=False,
        )

        result = storage.reconcile_openalex_person_works(
            "pi_1",
            "inst_1",
            None,
            "review-run",
            ["W901"],
            full_snapshot=True,
            missing_runs_before_tombstone=1,
            enqueue_vectors=False,
            relationship_evidence=relationship_evidence,
            archive_existing_author_link=archive_request,
        )

        assert storage.get_openalex_author_link("pi_1") is None
        assert result["counts"]["author_links_archived"] == 1
        assert result["author_link_archive"]["action"] == "archived"
        archive = storage.conn.execute(
            "SELECT * FROM openalex_author_link_archives WHERE person_id='pi_1'"
        ).fetchone()
        assert archive["openalex_author_id"] == "A999"
        assert archive["replacement_manifest_sha256"] == "a" * 64
        assert len(archive["original_link_sha256"]) == 64
        original = json.loads(archive["original_link_json"])
        assert original["match_method"] == (
            "exact_name_ror_and_official_publication_overlap"
        )
        relationships = {
            row["openalex_work_id"]: (
                row["relationship_status"],
                row["openalex_author_id"],
            )
            for row in storage.conn.execute(
                "SELECT openalex_work_id, relationship_status, openalex_author_id "
                "FROM openalex_person_works"
            )
        }
        assert relationships == {
            "W900": ("tombstoned", "A999"),
            "W901": ("active", None),
        }
    finally:
        storage.close()


@pytest.mark.parametrize(
    ("match_method", "evidence"),
    [
        (
            "reviewed_openalex_identity_manifest_v1",
            {"reviewed_identity": {"reviewed": True}},
        ),
        (
            "automatic_old_rule",
            {
                "primary_openalex_author_id": "A998",
                "confirmed_openalex_author_ids": ["A999"],
            },
        ),
    ],
)
def test_identity_pending_reconcile_fails_closed_without_archiving_reviewed_or_conflicting_link(
    tmp_path, match_method, evidence
) -> None:
    storage = PIIndexStorage(tmp_path / "unsafe-link-archive.db")
    try:
        storage.upsert_pi_record(_pi())
        storage.upsert_openalex_author_link(
            "pi_1",
            "inst_1",
            "A999",
            match_method=match_method,
            evidence=evidence,
        )
        storage.upsert_openalex_work(
            {"id": "W901", "title": "Reviewed publication"},
            "setup-run",
            enqueue_vectors=False,
        )
        relationship_evidence = {
            "relationship_method": "reviewed_official_evidence_only",
            "identity_status": "pending",
            "reviewed_identity": {
                "reviewed": True,
                "sync_mode": "official_evidence_only",
                "primary_openalex_author_id": None,
                "confirmed_openalex_author_ids": [],
                "manifest_sha256": "b" * 64,
            },
        }

        with pytest.raises(ValueError, match="reviewed|conflicts"):
            storage.reconcile_openalex_person_works(
                "pi_1",
                "inst_1",
                None,
                "review-run",
                ["W901"],
                full_snapshot=True,
                missing_runs_before_tombstone=1,
                enqueue_vectors=False,
                relationship_evidence=relationship_evidence,
                archive_existing_author_link={
                    "expected_openalex_author_id": "A999",
                    "reason": (
                        "superseded_by_reviewed_official_evidence_only_"
                        "identity_pending"
                    ),
                },
            )

        assert storage.get_openalex_author_link("pi_1") is not None
        assert storage.conn.execute(
            "SELECT COUNT(*) FROM openalex_author_link_archives"
        ).fetchone()[0] == 0
        assert storage.conn.execute(
            "SELECT COUNT(*) FROM openalex_person_works"
        ).fetchone()[0] == 0
    finally:
        storage.close()


def test_split_profile_relationships_preserve_per_work_author_provenance(
    tmp_path,
) -> None:
    storage = PIIndexStorage(tmp_path / "split-provenance.db")
    try:
        storage.upsert_pi_record(_pi())
        storage.upsert_openalex_author_link(
            "pi_1",
            "inst_1",
            "A101",
            evidence={
                "primary_openalex_author_id": "A101",
                "confirmed_openalex_author_ids": ["A101", "A202"],
            },
        )
        for work_id in ("W101", "W202"):
            storage.upsert_openalex_work(
                {"id": work_id, "title": f"Publication {work_id}"},
                "run-1",
                enqueue_vectors=False,
            )

        storage.reconcile_openalex_person_works(
            "pi_1",
            "inst_1",
            "A101",
            "run-1",
            ["W101", "W202"],
            full_snapshot=True,
            enqueue_vectors=False,
            observed_work_author_ids={"W101": "A101", "W202": "A202"},
        )

        rows = {
            row["openalex_work_id"]: row["openalex_author_id"]
            for row in storage.conn.execute(
                "SELECT openalex_work_id, openalex_author_id FROM openalex_person_works"
            )
        }
        assert rows == {"W101": "A101", "W202": "A202"}

        with pytest.raises(ValueError, match="not confirmed"):
            storage.reconcile_openalex_person_works(
                "pi_1",
                "inst_1",
                "A101",
                "run-2",
                ["W101"],
                full_snapshot=False,
                enqueue_vectors=False,
                observed_work_author_ids={"W101": "A999"},
            )
    finally:
        storage.close()


def test_openalex_identity_probe_cache_persists_hits_and_explicit_misses(tmp_path) -> None:
    storage = PIIndexStorage(tmp_path / "probe-cache.db")
    try:
        hit = storage.upsert_openalex_identity_probe_cache(
            "official_work_probe_v1|doi:10.1/example",
            "official_work_probe_v1",
            "doi",
            "10.1/example",
            [{"id": "https://openalex.org/W1", "title": "Example"}],
            fetched_at="2026-07-15T00:00:00Z",
            expires_at="2026-08-14T00:00:00Z",
            run_id="probe-run",
        )
        miss = storage.upsert_openalex_identity_probe_cache(
            "official_work_probe_v1|title:missing",
            "official_work_probe_v1",
            "title",
            "missing",
            [],
            fetched_at="2026-07-15T00:00:00Z",
            expires_at="2026-07-16T00:00:00Z",
            run_id="probe-run",
        )

        assert hit["result_status"] == "hit"
        assert hit["works"][0]["id"].endswith("/W1")
        assert miss["result_status"] == "miss"
        assert miss["works"] == []
        assert len(hit["works_sha256"]) == 64
    finally:
        storage.close()


def test_empty_current_manifest_persists_an_auditable_empty_career(tmp_path) -> None:
    storage = PIIndexStorage(tmp_path / "empty.db")
    try:
        storage.upsert_pi_record(_pi())
        storage.enqueue_vector_dirty(
            "career_vector_256",
            "pi_1",
            "empty_manifest",
            person_id="pi_1",
        )
        report = process_vector_queue(storage)
        career = storage.get_pi_career_vector("pi_1")
        assert report["status"] == "success"
        assert career is not None
        assert career["work_count"] == 0
        assert career["nonempty_work_count"] == 0
        assert career["feature_count"] == 0
        assert career["vector"] == {}
    finally:
        storage.close()


def test_shared_openalex_work_has_one_global_vector_and_two_careers(tmp_path) -> None:
    storage = PIIndexStorage(tmp_path / "shared.db")
    try:
        for person_id, author_id in (("pi_1", "A101"), ("pi_2", "A102")):
            storage.upsert_pi_record(_pi(person_id))
            storage.upsert_openalex_author_link(person_id, "inst_1", author_id)
        storage.upsert_openalex_work(
            {"id": "W201", "title": "Shared research on sustainable finance"},
            "run-1",
        )
        for person_id, author_id in (("pi_1", "A101"), ("pi_2", "A102")):
            storage.reconcile_openalex_person_works(
                person_id,
                "inst_1",
                author_id,
                "run-1",
                ["W201"],
                full_snapshot=True,
            )

        report = process_vector_queue(storage)

        assert report["status"] == "success"
        assert storage.conn.execute(
            "SELECT COUNT(*) FROM openalex_work_vectors"
        ).fetchone()[0] == 1
        assert storage.conn.execute(
            "SELECT COUNT(*) FROM pi_career_vectors"
        ).fetchone()[0] == 2
    finally:
        storage.close()


def test_queue_worker_exact_cohort_leaves_other_pi_jobs_untouched(tmp_path) -> None:
    storage = PIIndexStorage(tmp_path / "scoped-vectors.db")
    try:
        for person_id, author_id, work_id in (
            ("pi_1", "A101", "W501"),
            ("pi_2", "A102", "W502"),
        ):
            storage.upsert_pi_record(_pi(person_id))
            storage.upsert_openalex_author_link(person_id, "inst_1", author_id)
            storage.upsert_openalex_work(
                {"id": work_id, "title": f"Research for {person_id}"},
                "run-1",
            )
            storage.reconcile_openalex_person_works(
                person_id,
                "inst_1",
                author_id,
                "run-1",
                [work_id],
                full_snapshot=True,
            )

        report = process_vector_queue(storage, person_ids=["pi_1"])

        assert report["status"] == "success"
        assert report["scope_person_count"] == 1
        assert report["pending"] == 0
        assert storage.get_openalex_work_vector("W501") is not None
        assert storage.get_openalex_work_vector("W502") is None
        assert storage.get_pi_career_vector("pi_1") is not None
        assert storage.get_pi_career_vector("pi_2") is None
        pending_other = storage.conn.execute(
            """
            SELECT COUNT(*) FROM vector_dirty_queue
            WHERE status='pending'
              AND (entity_id='W502' OR entity_id='pi_2')
            """
        ).fetchone()[0]
        assert pending_other == 2
    finally:
        storage.close()


def test_paper_rebuild_racing_with_claimed_career_eventually_drains(tmp_path) -> None:
    storage = PIIndexStorage(tmp_path / "ordering.db")
    try:
        storage.upsert_pi_record(_pi())
        storage.upsert_openalex_author_link("pi_1", "inst_1", "A101")
        storage.upsert_openalex_work(
            {"id": "W301", "title": "Financial markets"},
            "run-1",
            enqueue_vectors=False,
        )
        storage.reconcile_openalex_person_works(
            "pi_1",
            "inst_1",
            "A101",
            "run-1",
            ["W301"],
            full_snapshot=True,
            enqueue_vectors=False,
        )
        storage.enqueue_vector_dirty(
            "career_vector_256",
            "pi_1",
            "older_career",
            person_id="pi_1",
            created_at="2026-01-01T00:00:00+00:00",
        )
        storage.enqueue_vector_dirty(
            "paper_vector_256",
            "W301",
            "newer_paper",
            created_at="2026-01-02T00:00:00+00:00",
        )

        report = process_vector_queue(storage, batch_size=2)

        # Both rows were claimed together. Paper-first processing queued a
        # redundant career rebuild while the older career row was processing;
        # the idempotent worker must still drain that follow-up to zero.
        assert report["paper_built"] == 1
        assert report["career_built"] == 2
        assert report["pending"] == 0
        assert storage.conn.execute(
            """
            SELECT COUNT(*) FROM vector_dirty_queue
            WHERE entity_kind IN ('paper_vector_256', 'career_vector_256')
              AND status!='completed'
            """
        ).fetchone()[0] == 0
    finally:
        storage.close()


def test_source_change_during_encoding_retries_same_job_safely(tmp_path) -> None:
    storage = PIIndexStorage(tmp_path / "stale-retry.db")

    class RacingBackend(ProductionTermVectorIndex):
        def __init__(self) -> None:
            self.changed = False

        def encode_publication(self, text: str) -> dict[str, float]:
            vector = super().encode_publication(text)
            if not self.changed:
                self.changed = True
                storage.upsert_openalex_work(
                    {"id": "W401", "title": "Updated while encoding"},
                    "run-2",
                    enqueue_vectors=False,
                )
            return vector

    try:
        storage.upsert_openalex_work(
            {"id": "W401", "title": "Initial text"},
            "run-1",
        )
        report = process_vector_queue(storage, backend=RacingBackend())
        stored = storage.get_openalex_work_vector("W401")

        assert report["retried"] == 1
        assert report["failed"] == 0
        assert report["completed"] == 1
        assert stored is not None
        source_hash = storage.conn.execute(
            "SELECT vector_text_hash FROM openalex_works WHERE openalex_work_id='W401'"
        ).fetchone()[0]
        assert stored["source_text_hash"] == source_hash
    finally:
        storage.close()
