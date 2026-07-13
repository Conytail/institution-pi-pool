from argparse import Namespace

from pi_index.eval.authorship_weight_experiment import (
    AuthorshipWeightConfig,
    ProfileGroup,
    acceptance_gate,
    author_role,
    build_authorship_cases,
    build_evaluation_matrix_context,
    build_institution_folds,
    build_numeric_profile_bases,
    enrich_pi_works,
    evaluate_profiles,
    evaluate_profiles_reference,
    evaluate_profiles_matrix,
    field_support_rows,
    generate_weight_configs,
    learn_field_overrides,
    normalize_work_type,
    publication_weight,
    robust_role_selection,
    phase_two_readiness,
    weighted_career_vector,
    weighted_numeric_career_vector,
)


def _work(work_id: str, year: int, author_id: str, role: str = "middle", count: int = 3):
    return {
        "id": work_id,
        "title": f"protein design method {work_id}",
        "abstract": "active learning for scientific discovery and protein engineering",
        "topics": ["Artificial Intelligence"],
        "year": year,
        "author_ids": [author_id],
        "author_position": role,
        "corresponding_confirmed": "unknown",
        "author_count": count,
        "author_role": role,
        "author_count_band": "2-4",
        "primary_openalex_field_id": "F1",
        "primary_openalex_field_name": "Computer Science",
        "primary_openalex_domain_id": "D1",
        "primary_openalex_domain_name": "Physical Sciences",
        "work_type": "journal",
    }


def test_enrichment_preserves_target_role_and_treats_false_corresponding_as_unknown():
    pi = {
        "person_id": "P1",
        "openalex_author_id": "https://openalex.org/A1",
        "works": [{"id": "https://openalex.org/W1", "title": "A title", "year": 2025}],
    }
    raw = {
        "https://openalex.org/W1": {
            "id": "https://openalex.org/W1",
            "authorships": [
                {
                    "author_position": "last",
                    "author": {"id": "https://openalex.org/A1"},
                    "is_corresponding": False,
                }
            ],
            "topics": [
                {
                    "field": {"id": "F1", "display_name": "Medicine"},
                    "domain": {"id": "D1", "display_name": "Health Sciences"},
                }
            ],
        }
    }
    enriched, coverage = enrich_pi_works(
        pi,
        raw,
        {"https://openalex.org/W1": ("article", "journal")},
    )
    work = enriched["works"][0]
    assert work["author_position"] == "last"
    assert work["corresponding_confirmed"] == "unknown"
    assert work["primary_openalex_field_id"] == "F1"
    assert work["work_type"] == "journal"
    assert coverage["position_last"] == 1


def test_inverted_profile_scoring_matches_reference_ranks():
    case = {
        "case_id": "C1",
        "source_pi_id": "P1",
        "institution_id": "I1",
        "institution_name": "Institution",
        "positive_pi_ids": ["P1"],
        "author_role": "first",
        "author_position": "first",
        "corresponding_confirmed": "unknown",
        "author_count_band": "2-4",
        "work_type": "journal",
        "publication_age_band": "0-3",
        "primary_openalex_field_id": "F1",
        "primary_openalex_field_name": "Field",
        "primary_openalex_domain_name": "Domain",
    }
    profiles = {
        "P1": {"protein": 0.8, "design": 0.6},
        "P2": {"systems": 0.8, "design": 0.6},
    }
    queries = {"C1": ({"protein": 1.0}, {"design": 1.0})}
    candidates = {"I1": ["P1", "P2"]}
    reference = evaluate_profiles_reference(
        [case], profiles, queries, candidates, "cfg", "production_terms", "equal"
    )
    inverted = evaluate_profiles(
        [case], profiles, queries, candidates, "cfg", "production_terms", "equal"
    )
    bases = build_numeric_profile_bases(
        {
            pi_id: [
                ProfileGroup("*", "first", "unknown", "2-4", 2, vector, 1)
            ]
            for pi_id, vector in profiles.items()
        }
    )
    context = build_evaluation_matrix_context([case], bases, queries, candidates)
    matrix = evaluate_profiles_matrix(
        [case], profiles, context, "cfg", "production_terms", "equal"
    )
    assert inverted == reference
    assert matrix == reference


def test_publication_weight_uses_team_penalty_only_for_non_corresponding_middle():
    config = AuthorshipWeightConfig(
        "role_aware",
        corresponding_weight=2.0,
        middle_medium_weight=0.75,
        middle_large_weight=0.5,
    )
    middle_large = {
        "author_position": "middle",
        "author_count": 20,
        "corresponding_confirmed": "unknown",
    }
    corresponding_middle = {**middle_large, "corresponding_confirmed": "true"}
    assert publication_weight(middle_large, config) == 0.5
    assert publication_weight(corresponding_middle, config) == 2.0


def test_weight_is_clipped_and_solo_overrides_position():
    config = AuthorshipWeightConfig(
        "role_aware",
        first_weight=1.5,
        corresponding_weight=2.0,
        solo_weight=1.5,
    )
    solo = {
        "author_position": "first",
        "author_count": 1,
        "corresponding_confirmed": "true",
    }
    assert author_role(solo) == "solo"
    assert publication_weight(solo, config) == 2.5


def test_equal_weighted_profile_matches_normalized_group_sum():
    groups = [
        ProfileGroup("F1", "first", "unknown", "2-4", 3, {"protein": 1.0}, 1),
        ProfileGroup("F1", "last", "unknown", "2-4", 3, {"systems": 1.0}, 1),
    ]
    vector = weighted_career_vector(groups, AuthorshipWeightConfig("equal"), feature_limit=256)
    assert round(vector["protein"], 6) == round(2**-0.5, 6)
    assert round(vector["systems"], 6) == round(2**-0.5, 6)


def test_numeric_profile_path_is_equivalent_to_reference_implementation():
    groups = [
        ProfileGroup("*", "first", "unknown", "2-4", 3, {"protein": 1.2, "ai": 0.2}, 2),
        ProfileGroup("*", "middle", "unknown", "11+", 20, {"systems": 0.8, "ai": 0.4}, 3),
    ]
    config = AuthorshipWeightConfig(
        "role_aware",
        first_weight=1.5,
        middle_large_weight=0.5,
    )
    expected = weighted_career_vector(groups, config, feature_limit=256)
    basis = build_numeric_profile_bases({"P1": groups})["P1"]
    actual = weighted_numeric_career_vector(basis, config, feature_limit=256)
    assert actual.keys() == expected.keys()
    assert all(abs(actual[key] - expected[key]) < 1e-12 for key in actual)


def test_case_builder_keeps_global_holdouts_out_of_every_profile():
    pis = []
    for index in range(2):
        author_id = f"A{index + 1}"
        works = [_work(f"W{index}-{number}", 2010 + number, author_id) for number in range(14)]
        pis.append(
            {
                "person_id": f"P{index + 1}",
                "institution_id": "I1",
                "institution_name": "Institution",
                "openalex_author_id": f"https://openalex.org/{author_id}",
                "works": works,
            }
        )
    cases, profiles, heldout = build_authorship_cases(pis, 3, 2, 8)
    assert cases
    assert len({case["proposal_work_id"] for case in cases}) == len(cases)
    assert all(len(works) >= 8 for works in profiles.values())
    assert all(not heldout.intersection({work["id"] for work in works}) for works in profiles.values())


def test_grid_contains_equal_plus_all_nonduplicate_role_configs():
    configs = generate_weight_configs()
    assert len(configs) == 1152
    assert len({config.config_id for config in configs}) == 1152
    assert sum(config.scheme == "equal" for config in configs) == 1


def test_field_support_requires_both_pi_and_institution_thresholds():
    pis = [
        {
            "person_id": f"P{index}",
            "institution_id": f"I{index % 5}",
            "works": [{"primary_openalex_field_id": "F1", "primary_openalex_field_name": "Field"}],
        }
        for index in range(100)
    ]
    rows = field_support_rows(pis)
    assert rows[0]["field_aware_eligible"] is True
    assert field_support_rows(pis, min_institutions=6)[0]["field_aware_eligible"] is False


def test_institution_folds_never_split_an_institution():
    cases = [
        {"institution_id": f"I{institution}"}
        for institution in range(7)
        for _case in range(institution + 1)
    ]
    folds = build_institution_folds(cases, 5)
    flattened = [institution for fold in folds for institution in fold]
    assert len(folds) == 5
    assert len(flattened) == len(set(flattened)) == 7


def test_acceptance_gate_cannot_pass_without_phase_two_data():
    selected = {"source_pi_ndcg_at_10": 0.91, "source_pi_hit_at_5": 0.98}
    equal = {"source_pi_ndcg_at_10": 0.90, "source_pi_hit_at_5": 0.98}
    folds = [{"source_pi_ndcg_delta": 0.01}] * 5
    gate = acceptance_gate(
        selected,
        equal,
        {"ci_low": 0.001},
        folds,
        [],
        {"ready": False},
    )
    assert gate["passed"] is False
    assert gate["checks"]["phase_two_ready"] is False


def test_work_type_normalization_is_conservative():
    assert normalize_work_type("preprint", "repository") == "preprint"
    assert normalize_work_type("article", "conference") == "conference"
    assert normalize_work_type("article", "journal") == "journal"
    assert normalize_work_type(None, None) == "unknown"


def test_robust_selection_reports_non_identifiable_tied_weights():
    base = {
        "scheme": "role_aware",
        "first_weight": 1.5,
        "last_weight": 0.75,
        "middle_medium_weight": 1.0,
        "middle_large_weight": 0.75,
        "weight_deviation": 1.25,
        "source_pi_ndcg_at_10": 0.92,
        "source_pi_hit_at_5": 0.97,
    }
    left = {
        **base,
        "config_id": "aaa",
        "corresponding_weight": 1.0,
        "solo_weight": 1.25,
    }
    right = {
        **base,
        "config_id": "bbb",
        "corresponding_weight": 1.25,
        "solo_weight": 1.0,
    }
    selected = robust_role_selection(
        [
            {"encoder": "production_terms", "config_results": [left, right]},
            {"encoder": "tfidf", "config_results": [left, right]},
        ]
    )
    assert selected["config_id"] == "aaa"
    assert selected["equivalent_config_count"] == 2
    assert selected["non_identifiable_weight_fields"] == [
        "corresponding_weight",
        "solo_weight",
    ]


def test_phase_two_readiness_requires_official_pool_scale():
    result = phase_two_readiness(
        [{"person_id": "P1", "institution_id": "I1", "works": []}]
    )
    assert result["ready"] is False
    assert len(result["reasons"]) == 3


def test_field_override_learning_runs_only_on_requested_field_cases():
    groups = {
        "P1": [ProfileGroup("F1", "first", "unknown", "2-4", 2, {"protein": 1.0}, 1)],
        "P2": [ProfileGroup("F1", "middle", "unknown", "5-10", 6, {"systems": 1.0}, 1)],
    }
    case = {
        "case_id": "C1",
        "source_pi_id": "P1",
        "institution_id": "I1",
        "institution_name": "Institution",
        "positive_pi_ids": ["P1"],
        "author_role": "first",
        "author_position": "first",
        "corresponding_confirmed": "unknown",
        "author_count_band": "2-4",
        "work_type": "journal",
        "publication_age_band": "0-3",
        "primary_openalex_field_id": "F1",
        "primary_openalex_field_name": "Field",
        "primary_openalex_domain_name": "Domain",
    }
    overrides, audit = learn_field_overrides(
        AuthorshipWeightConfig("role_aware"),
        ["F1"],
        [case],
        groups,
        {"C1": ({"protein": 1.0}, {})},
        {"I1": ["P1", "P2"]},
        "production_terms",
    )
    assert set(overrides) == {"F1"}
    assert audit[0]["field_case_count"] == 1
