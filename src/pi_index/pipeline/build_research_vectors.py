from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from ..index.vector_index import (
    CAREER_VECTOR_KIND,
    PAPER_VECTOR_KIND,
    ProductionTermVectorIndex,
)
from ..models import utc_now_iso
from ..storage import PIIndexStorage


VECTOR_JOB_KINDS = (PAPER_VECTOR_KIND, CAREER_VECTOR_KIND)


def _work_row(storage: PIIndexStorage, openalex_work_id: str) -> dict[str, Any]:
    row = storage.conn.execute(
        "SELECT * FROM openalex_works WHERE openalex_work_id=?",
        (openalex_work_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown OpenAlex work: {openalex_work_id}")
    return dict(row)


def _paper_vector_is_current(
    stored: dict[str, Any] | None,
    work: dict[str, Any],
    backend: ProductionTermVectorIndex,
) -> bool:
    return bool(
        stored
        and stored["encoder_id"] == backend.encoder_id
        and int(stored["feature_limit"]) == backend.feature_limit
        and stored["source_text_hash"] == work["vector_text_hash"]
    )


def ensure_paper_vector(
    storage: PIIndexStorage,
    openalex_work_id: str,
    *,
    backend: ProductionTermVectorIndex | None = None,
    enqueue_linked_careers: bool = False,
    run_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Return a fresh paper vector, rebuilding it only when its input changed."""

    backend = backend or ProductionTermVectorIndex()
    work = _work_row(storage, openalex_work_id)
    stored = storage.get_openalex_work_vector(
        openalex_work_id,
        representation=backend.publication_representation,
    )
    if _paper_vector_is_current(stored, work, backend):
        return stored or {}, False

    vector = backend.encode_publication(str(work.get("vector_text") or ""))
    result = storage.upsert_openalex_work_vector(
        openalex_work_id,
        vector,
        str(work.get("vector_text_hash") or ""),
        representation=backend.publication_representation,
        encoder_id=backend.encoder_id,
        feature_limit=backend.feature_limit,
        record={"source": "openalex_works.vector_text"},
    )
    if enqueue_linked_careers:
        linked_people = storage.conn.execute(
            """
            SELECT DISTINCT person_id FROM openalex_person_works
            WHERE openalex_work_id=?
              AND relationship_status IN ('active', 'missing')
            ORDER BY person_id
            """,
            (openalex_work_id,),
        ).fetchall()
        for linked in linked_people:
            person_id = str(linked["person_id"])
            storage.enqueue_vector_dirty(
                CAREER_VECTOR_KIND,
                person_id,
                "paper_vector_rebuilt",
                run_id=run_id,
                person_id=person_id,
                payload={"openalex_work_id": openalex_work_id},
            )
    return result, True


def _dependency_hash(
    backend: ProductionTermVectorIndex,
    dependencies: list[dict[str, str]],
) -> str:
    payload = {
        "encoder_id": backend.encoder_id,
        "feature_limit": backend.feature_limit,
        "paper_representation": backend.publication_representation,
        "works": sorted(
            dependencies,
            key=lambda item: (item["openalex_work_id"], item["vector_hash"]),
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_career_vector(
    storage: PIIndexStorage,
    person_id: str,
    *,
    backend: ProductionTermVectorIndex | None = None,
) -> tuple[dict[str, Any], int]:
    """Build one PI vector from every current (active or provisional) Work.

    ``missing`` relationships remain current until the configured second full
    snapshot tombstones them, matching the OpenAlex inventory lifecycle.
    """

    backend = backend or ProductionTermVectorIndex()
    if storage.get_pi_record(person_id) is None:
        raise KeyError(f"Unknown PI: {person_id}")
    works = list(storage.iter_current_openalex_works(person_id=person_id))
    paper_vectors: list[dict[str, float]] = []
    dependencies: list[dict[str, str]] = []
    inline_built = 0
    for work in works:
        paper, built = ensure_paper_vector(
            storage,
            str(work["openalex_work_id"]),
            backend=backend,
            enqueue_linked_careers=False,
        )
        inline_built += int(built)
        vector = dict(paper.get("vector") or {})
        if vector:
            paper_vectors.append(vector)
        dependencies.append(
            {
                "openalex_work_id": str(work["openalex_work_id"]),
                "source_text_hash": str(paper["source_text_hash"]),
                "vector_hash": str(paper["vector_hash"]),
            }
        )

    career = backend.aggregate_career(paper_vectors)
    dependency_hash = _dependency_hash(backend, dependencies)
    result = storage.upsert_pi_career_vector(
        person_id,
        career,
        dependency_hash,
        len(works),
        len(paper_vectors),
        representation=backend.career_representation,
        encoder_id=backend.encoder_id,
        feature_limit=backend.feature_limit,
        record={
            "aggregation": "equal_weight_l2_normalized_centroid",
            "current_relationship_statuses": ["active", "missing"],
        },
    )
    return result, inline_built


def process_vector_queue(
    storage: PIIndexStorage,
    *,
    limit: int | None = None,
    batch_size: int = 100,
    owner: str | None = None,
    lease_seconds: float = 900.0,
    max_attempts: int = 3,
    backend: ProductionTermVectorIndex | None = None,
    person_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Drain paper/career jobs, optionally restricted to an exact PI cohort."""

    backend = backend or ProductionTermVectorIndex()
    if limit is not None and int(limit) < 1:
        raise ValueError("limit must be at least 1")
    if int(batch_size) < 1:
        raise ValueError("batch_size must be at least 1")
    if int(max_attempts) < 1:
        raise ValueError("max_attempts must be at least 1")
    scoped_people = (
        tuple(
            dict.fromkeys(
                " ".join(str(value or "").split()) for value in person_ids
            )
        )
        if person_ids is not None
        else None
    )
    if scoped_people is not None and any(not value for value in scoped_people):
        raise ValueError("person_ids cannot contain blank values")

    metrics: dict[str, Any] = {
        "status": "success",
        "encoder_id": backend.encoder_id,
        "feature_limit": backend.feature_limit,
        "paper_representation": backend.publication_representation,
        "career_representation": backend.career_representation,
        "claimed": 0,
        "completed": 0,
        "retried": 0,
        "failed": 0,
        "paper_built": 0,
        "paper_reused": 0,
        "career_built": 0,
        "paper_built_inline_for_career": 0,
        "errors": [],
        "scope_person_ids": list(scoped_people) if scoped_people is not None else None,
        "scope_person_count": len(scoped_people) if scoped_people is not None else None,
        "started_at": utc_now_iso(),
    }

    while limit is None or metrics["claimed"] < int(limit):
        remaining = int(limit) - metrics["claimed"] if limit is not None else batch_size
        claim_count = min(int(batch_size), remaining)
        jobs = storage.claim_vector_dirty_jobs(
            claim_count,
            owner=owner,
            lease_seconds=lease_seconds,
            entity_kinds=VECTOR_JOB_KINDS,
            scope_person_ids=scoped_people,
        )
        if not jobs:
            break
        metrics["claimed"] += len(jobs)
        # A career job can be older than some of its paper jobs.  Paper-first
        # reduces inline work while build_career_vector still guarantees a
        # complete fresh dependency set when batches split the two kinds.
        jobs.sort(
            key=lambda job: (
                0 if job["entity_kind"] == PAPER_VECTOR_KIND else 1,
                int(job["queue_id"]),
            )
        )
        for job in jobs:
            try:
                if job["entity_kind"] == PAPER_VECTOR_KIND:
                    _paper, built = ensure_paper_vector(
                        storage,
                        str(job["entity_id"]),
                        backend=backend,
                        enqueue_linked_careers=True,
                        run_id=job.get("run_id"),
                    )
                    metrics["paper_built" if built else "paper_reused"] += 1
                elif job["entity_kind"] == CAREER_VECTOR_KIND:
                    _career, inline_built = build_career_vector(
                        storage,
                        str(job["entity_id"]),
                        backend=backend,
                    )
                    metrics["career_built"] += 1
                    metrics["paper_built_inline_for_career"] += inline_built
                else:  # Defensive; entity_kinds filtering should make this unreachable.
                    raise ValueError(f"Unsupported vector job kind: {job['entity_kind']}")
            except Exception as exc:  # Keep the leased queue resumable per item.
                retry = int(job["attempts"]) < int(max_attempts)
                storage.finish_vector_dirty_job(
                    int(job["queue_id"]),
                    success=False,
                    error_reason=f"{type(exc).__name__}: {exc}",
                    retry=retry,
                    claim_token=str(job["claim_token"]),
                )
                metrics["retried" if retry else "failed"] += 1
                metrics["errors"].append(
                    {
                        "queue_id": int(job["queue_id"]),
                        "entity_kind": job["entity_kind"],
                        "entity_id": job["entity_id"],
                        "retry": retry,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            storage.finish_vector_dirty_job(
                int(job["queue_id"]),
                success=True,
                claim_token=str(job["claim_token"]),
            )
            metrics["completed"] += 1

    if scoped_people is None:
        pending = storage.conn.execute(
            """
            SELECT COUNT(*) FROM vector_dirty_queue
            WHERE status='pending' AND entity_kind IN (?, ?)
            """,
            VECTOR_JOB_KINDS,
        ).fetchone()[0]
    elif not scoped_people:
        pending = 0
    else:
        placeholders = ",".join("?" for _ in scoped_people)
        pending = storage.conn.execute(
            f"""
            SELECT COUNT(*)
            FROM vector_dirty_queue AS q
            WHERE q.status='pending'
              AND (
                  (q.entity_kind=? AND q.entity_id IN ({placeholders}))
                  OR (
                      q.entity_kind=?
                      AND EXISTS (
                          SELECT 1
                          FROM openalex_person_works AS pw
                          WHERE pw.openalex_work_id=q.entity_id
                            AND pw.relationship_status IN ('active', 'missing')
                            AND pw.person_id IN ({placeholders})
                      )
                  )
              )
            """,
            (
                CAREER_VECTOR_KIND,
                *scoped_people,
                PAPER_VECTOR_KIND,
                *scoped_people,
            ),
        ).fetchone()[0]
    metrics["pending"] = int(pending)
    metrics["finished_at"] = utc_now_iso()
    if metrics["failed"]:
        metrics["status"] = "partial"
    elif metrics["pending"] and limit is not None:
        metrics["status"] = "limited"
    return metrics
