from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from typing import Iterable, Mapping

from ..match.semantic import semantic_vector


SparseVector = dict[str, float]

PAPER_VECTOR_KIND = "paper_vector_256"
CAREER_VECTOR_KIND = "career_vector_256"
# Matching Policy v1 used this name before the dirty queue was introduced.
# Keep it as the persisted representation name and accept PAPER_VECTOR_KIND as
# the queue/job alias.
PUBLICATION_VECTOR_REPRESENTATION = "publication_vector_256"
CAREER_VECTOR_REPRESENTATION = "career_vector_256"
PRODUCTION_TERM_ENCODER_ID = "production_terms_v1"
VECTOR_FEATURE_LIMIT = 256
ENCODER_CANDIDATE_LIMIT = 1024


def normalize_sparse_vector(
    vector: Mapping[str, float],
    feature_limit: int = VECTOR_FEATURE_LIMIT,
) -> SparseVector:
    """Select the strongest sparse features and return an L2 unit vector.

    ``*_vector_256`` in the validated experiments means at most 256 sparse
    term features, not a dense array with 256 positions.  Short or empty text
    therefore legitimately produces fewer than 256 entries.
    """

    limit = int(feature_limit)
    if limit < 1:
        raise ValueError("feature_limit must be at least 1")
    cleaned: list[tuple[str, float]] = []
    for raw_term, raw_value in vector.items():
        term = str(raw_term).strip()
        value = float(raw_value)
        if not term or not value:
            continue
        if not math.isfinite(value):
            raise ValueError("sparse vector values must be finite")
        cleaned.append((term, value))
    selected = sorted(cleaned, key=lambda item: (-abs(item[1]), item[0]))[:limit]
    norm = math.sqrt(sum(value * value for _term, value in selected))
    if norm <= 0:
        return {}
    return {term: value / norm for term, value in selected}


def encode_publication_text(
    text: str,
    *,
    feature_limit: int = VECTOR_FEATURE_LIMIT,
) -> SparseVector:
    """Encode OpenAlex title/abstract/topic text with the frozen local encoder.

    This exactly follows the ``production_terms`` branch used by the profile
    resolution and granularity experiments: generate up to 1,024 candidate
    unigram/bigram/trigram terms, retain the strongest 256, then L2-normalize.
    It has no model download, API call, or corpus-wide fitting step.
    """

    candidates = semantic_vector(text or "", max_features=ENCODER_CANDIDATE_LIMIT)
    return normalize_sparse_vector(candidates, feature_limit)


def aggregate_career_vector(
    paper_vectors: Iterable[Mapping[str, float]],
    *,
    feature_limit: int = VECTOR_FEATURE_LIMIT,
) -> SparseVector:
    """Build the frozen equal-weight career centroid from paper vectors."""

    vectors = [dict(vector) for vector in paper_vectors if vector]
    if not vectors:
        return {}
    combined: defaultdict[str, float] = defaultdict(float)
    for vector in vectors:
        # Persisted paper vectors are already normalized.  Normalizing again
        # makes this public helper safe for callers with equivalent raw maps.
        for term, value in normalize_sparse_vector(vector, feature_limit).items():
            combined[term] += value
    scale = 1.0 / len(vectors)
    return normalize_sparse_vector(
        {term: value * scale for term, value in combined.items()},
        feature_limit,
    )


def sparse_vector_json(vector: Mapping[str, float]) -> str:
    """Return the canonical on-disk JSON representation."""

    return json.dumps(
        {term: float(value) for term, value in sorted(vector.items())},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sparse_vector_hash(vector: Mapping[str, float]) -> str:
    return hashlib.sha256(sparse_vector_json(vector).encode("utf-8")).hexdigest()


class ProductionTermVectorIndex:
    """The deterministic local backend validated by Matching Policy v1."""

    encoder_id = PRODUCTION_TERM_ENCODER_ID
    feature_limit = VECTOR_FEATURE_LIMIT
    publication_representation = PUBLICATION_VECTOR_REPRESENTATION
    career_representation = CAREER_VECTOR_REPRESENTATION

    def available(self) -> bool:
        return True

    def encode_publication(self, text: str) -> SparseVector:
        return encode_publication_text(text, feature_limit=self.feature_limit)

    def aggregate_career(
        self,
        paper_vectors: Iterable[Mapping[str, float]],
    ) -> SparseVector:
        return aggregate_career_vector(
            paper_vectors,
            feature_limit=self.feature_limit,
        )


class OptionalVectorIndex:
    """Compatibility wrapper for callers that want an optional backend.

    With no backend supplied it preserves the baseline-v0.1 behaviour and is
    unavailable.  Supplying :class:`ProductionTermVectorIndex` activates the
    now-implemented local backend without silently changing older callers.
    """

    def __init__(self, backend: ProductionTermVectorIndex | None = None):
        self.backend = backend

    def available(self) -> bool:
        return bool(self.backend and self.backend.available())
