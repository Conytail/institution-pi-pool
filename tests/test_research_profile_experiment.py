from pi_index.eval.research_profile_experiment import (
    ProfileConfig,
    build_cases,
    build_profile,
    generate_configs,
    leave_one_institution_out,
    name_similarity,
    _resolve_author_batch,
    profile_storage_components,
    profile_score,
    select_one_se,
)


def _work(work_id: str, year: int, author_id: str, text: str) -> dict:
    return {
        "id": work_id,
        "title": text,
        "abstract": text,
        "topics": [],
        "year": year,
        "author_ids": [author_id],
    }


def test_name_similarity_accepts_middle_initial_variant():
    assert name_similarity("David Blei", "David M. Blei") > 0.83
    assert name_similarity("David Blei", "David Knowles") == 0.0
    assert name_similarity("Jose Alvarez", "José Alvarez") == 1.0


def test_batch_identity_resolution_uses_one_ror_grounded_query():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def get(self, endpoint, params, cache_key):
            self.calls.append((endpoint, params, cache_key))
            return {
                "results": [
                    {
                        "id": "https://openalex.org/A1",
                        "display_name": "Jane Doe",
                        "display_name_alternatives": [],
                        "works_count": 20,
                        "affiliations": [
                            {"institution": {"ror": "https://ror.org/012345678"}}
                        ],
                        "last_known_institutions": [],
                    },
                    {
                        "id": "https://openalex.org/A2",
                        "display_name": "John Smith",
                        "display_name_alternatives": [],
                        "works_count": 20,
                        "affiliations": [
                            {"institution": {"ror": "https://ror.org/012345678"}}
                        ],
                        "last_known_institutions": [],
                    },
                ]
            }

    pis = [
        {
            "person_id": "P1",
            "display_name": "Jane Doe",
            "ror_id": "https://ror.org/012345678",
        },
        {
            "person_id": "P2",
            "display_name": "John Smith",
            "ror_id": "https://ror.org/012345678",
        },
    ]
    client = FakeClient()
    resolved = _resolve_author_batch(client, pis)
    assert set(resolved) == {"P1", "P2"}
    assert len(client.calls) == 1
    assert "affiliations.institution.ror:https://ror.org/012345678" in client.calls[0][1]["filter"]
    assert "Jane Doe|John Smith" in client.calls[0][1]["filter"]


def test_build_cases_removes_all_heldout_works_from_profiles():
    works = [_work(f"W{index}", 2010 + index, "A1", f"topic {index} protein design") for index in range(12)]
    pi = {
        "person_id": "P1",
        "institution_id": "I1",
        "institution_name": "Institution",
        "openalex_author_id": "https://openalex.org/A1",
        "works": works,
    }
    cases, profiles, heldout = build_cases([pi], 2, 2, 8)
    assert len(cases) == 2
    assert len(heldout) == 4
    assert not heldout.intersection({work["id"] for work in profiles["P1"]})


def test_cluster_profile_can_match_minor_topic_better_than_global_only():
    vectors = {
        "W1": {"systems": 1.0},
        "W2": {"systems": 1.0},
        "W3": {"protein": 1.0},
    }
    works = [
        {"id": "W1", "title": "systems", "year": 2024},
        {"id": "W2", "title": "systems", "year": 2025},
        {"id": "W3", "title": "protein", "year": 2025},
    ]
    career_config = ProfileConfig("career", 64, 5, "0", 0, "none")
    cluster_config = ProfileConfig("career_recent_clusters", 64, 5, "2", 1, "none")
    career = build_profile(works, vectors, career_config)
    clustered = build_profile(works, vectors, cluster_config)
    query = {"protein": 1.0}
    assert profile_score(query, {}, clustered, cluster_config) > profile_score(query, {}, career, career_config)


def test_one_se_rule_prefers_smaller_profile():
    rows = [
        {
            "config_id": "large",
            "ndcg_at_10": 0.80,
            "ndcg_se": 0.03,
            "recall_at_5": 0.90,
            "storage_bytes_per_pi": 1000,
            "query_ms_per_case": 2.0,
        },
        {
            "config_id": "small",
            "ndcg_at_10": 0.78,
            "ndcg_se": 0.03,
            "recall_at_5": 0.89,
            "storage_bytes_per_pi": 200,
            "query_ms_per_case": 1.0,
        },
    ]
    assert select_one_se(rows)["config_id"] == "small"


def test_storage_separates_fixed_publication_manifest_from_research_vectors():
    config = ProfileConfig("career_recent", 64, 5, "0", 0, "none")
    works = [{"id": "W1", "title": "protein design", "year": 2025}]
    profile = build_profile(works, {"W1": {"protein": 1.0}}, config)
    manifest_bytes, research_bytes = profile_storage_components(profile, config)
    assert manifest_bytes > 0
    assert research_bytes == 80


def test_screening_grid_contains_low_mid_high_publication_anchors():
    configs = generate_configs()
    assert len(configs) == len({config.config_id for config in configs}) == 72
    anchors = {
        (config.feature_limit, config.recent_years, config.cluster_count, config.paper_scope)
        for config in configs
    }
    assert (64, 3, "adaptive", "all") in anchors
    assert (128, 5, "5", "all") in anchors
    assert (256, 3, "8", "all") in anchors


def test_leave_one_institution_out_never_trains_on_heldout_rows():
    configs = [
        {
            "config_id": "small",
            "mode": "career",
            "feature_limit": 64,
            "recent_years": 5,
            "cluster_count": "0",
            "representatives_per_cluster": 0,
            "paper_scope": "none",
            "storage_bytes_per_pi": 100,
            "query_ms_per_case": 1.0,
        },
        {
            "config_id": "large",
            "mode": "career",
            "feature_limit": 256,
            "recent_years": 5,
            "cluster_count": "0",
            "representatives_per_cluster": 0,
            "paper_scope": "none",
            "storage_bytes_per_pi": 500,
            "query_ms_per_case": 1.0,
        },
    ]
    rows = []
    for institution, small_ndcg, large_ndcg in (("A", 0.8, 0.82), ("B", 0.7, 0.9)):
        for config_id, ndcg in (("small", small_ndcg), ("large", large_ndcg)):
            rows.append(
                {
                    "config_id": config_id,
                    "institution_name": institution,
                    "source_pi_id": f"{institution}-PI",
                    "ndcg_at_10": ndcg,
                    "reciprocal_rank": ndcg,
                    "rank": 1,
                }
            )
    output = leave_one_institution_out(configs, rows)
    assert {row["heldout_institution"] for row in output} == {"A", "B"}
    heldout_a = next(row for row in output if row["heldout_institution"] == "A")
    assert heldout_a["train_ndcg_at_10"] in {0.7, 0.9}
