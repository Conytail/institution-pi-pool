import numpy as np
from scipy.sparse import csr_matrix

from pi_index.eval.profile_granularity_experiment import (
    InstitutionContext,
    _combined_query,
    score_query,
    top_k_mean_by_span,
)


def test_combined_query_preserves_frozen_proposal_cv_weights():
    actual = _combined_query({"a": 1.0}, {"a": 0.5, "b": 1.0})
    assert actual.keys() == {"a", "b"}
    assert np.allclose([actual["a"], actual["b"]], [0.9, 0.2])


def test_top_k_mean_is_computed_within_each_pi():
    scores = np.asarray([0.9, 0.6, 0.1, 0.8, 0.7])
    actual = top_k_mean_by_span(scores, [(0, 3), (3, 5)], 2)
    assert np.allclose(actual, [0.75, 0.75])


def _context() -> InstitutionContext:
    return InstitutionContext(
        institution_id="i1",
        candidates=["pi_a", "pi_b", "pi_c"],
        cases=[{"case_id": "c1"}],
        query_matrix=csr_matrix([[1.0, 0.0]]),
        career_matrix=csr_matrix(
            [
                [0.8, 0.0],
                [0.7, 0.0],
                [0.6, 0.0],
            ]
        ),
        paper_matrix=csr_matrix(
            [
                [0.2, 0.0],
                [0.3, 0.0],
                [0.95, 0.0],
                [0.9, 0.0],
                [0.99, 0.0],
            ]
        ),
        paper_spans=[(0, 2), (2, 4), (4, 5)],
    )


def test_paper_max_can_rank_narrow_match_above_career_match():
    ranked, compared_pis, compared_papers = score_query(
        _context(), 0, "paper_max256", 2
    )
    assert ranked == ["pi_c", "pi_b", "pi_a"]
    assert compared_pis == 0
    assert compared_papers == 5


def test_hybrid_only_reranks_the_career_shortlist():
    ranked, compared_pis, compared_papers = score_query(
        _context(), 0, "hybrid_top10", 2
    )
    assert ranked == ["pi_b", "pi_a", "pi_c"]
    assert compared_pis == 3
    assert compared_papers == 4
