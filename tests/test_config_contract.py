import json
from pathlib import Path

import pytest

from pi_index.config import (
    ConfigValidationError,
    load_institution_config,
    load_matching_policy,
    validate_institution_config,
)


ROOT = Path(__file__).parent.parent


def test_all_tracked_institution_configs_follow_v2_contract():
    config_paths = sorted((ROOT / "configs" / "institutions").glob("*.yaml"))
    assert config_paths
    for path in config_paths:
        config = load_institution_config(path)
        assert config["schema_version"] == 2
        assert config["site"]["template_family"]
        assert config["institution"].get("ror_id") or config["institution"].get("homepage_url")


def test_title_neutral_pool_semantics_do_not_leave_reactivatable_role_exclusions():
    polyu = load_institution_config(ROOT / "configs" / "institutions" / "polyu.yaml")
    excluded = {value.casefold() for value in polyu["crawl"].get("exclude_url_patterns", [])}
    assert excluded.isdisjoint({"emeritus", "honorary", "visiting", "adjunct"})

    workbook_source = (ROOT / "scripts" / "build_research_profile_workbook.mjs").read_text(
        encoding="utf-8"
    )
    assert "supervisor eligibility" not in workbook_source.casefold()
    assert "supervisor validity" not in workbook_source.casefold()


def test_matching_v1_freezes_constraint_and_paper_feature_semantics():
    policy = load_matching_policy(ROOT / "configs" / "matching" / "matching_v1.yaml")
    assert policy["implementation_status"] == "validated_candidate_not_serving"
    assert policy["institution_fit"]["mode"] == "hard_filter"
    assert policy["institution_fit"]["cross_institution_backtrace_override"] is False
    assert policy["research_fit"]["paper_evidence_role"] == "feature_not_filter"
    assert policy["research_fit"]["semantic_fallback_when_paper_score_zero"] is True
    assert policy["research_fit"]["zero_only_when_semantic_and_profile_are_zero"] is True
    assert policy["full_match"]["research_score"] == {
        "operation": "max",
        "components": ["career_score", "paper_top3_score"],
    }
    assert policy["full_match"]["recent_score"]["enabled"] is False


def test_institution_config_requires_stable_identity_anchor():
    with pytest.raises(ConfigValidationError, match="stable identity anchor"):
        validate_institution_config(
            {
                "schema_version": 2,
                "config_version": 2,
                "institution": {"name": "Example", "official_domains": ["example.edu"]},
                "pool_scope": {
                    "type": "institution",
                    "name": "Example",
                    "units": [{"name": "Example", "seed_urls": ["https://example.edu"]}],
                },
                "site": {"template_family": "generic_v1"},
                "crawl": {"seed_urls": ["https://example.edu"], "max_depth": 0, "max_pages": 1},
                "parsing": {
                    "preferred_adapters": ["generic_html"],
                    "extract_publication_fingerprints": True,
                },
                "refresh": {"directory_interval_days": 30, "profile_interval_days": 90},
                "capture": {
                    "archive_enabled": True,
                    "compression": "gzip",
                    "conditional_requests": True,
                    "missing_runs_before_inactive": 2,
                },
                "quality_gate": {
                    "minimum_people": 1,
                    "maximum_duplicate_rate": 0.1,
                    "minimum_profile_url_coverage": 0.5,
                    "minimum_seed_url_coverage": 1.0,
                    "minimum_unit_coverage": 1.0,
                    "minimum_profile_fetch_coverage": 0.9,
                    "minimum_profile_parse_coverage": 0.0,
                    "require_pagination_complete": True,
                },
            }
        )


@pytest.mark.parametrize(
    ("filename", "title"),
    [
        ("pi_record.v1.schema.json", "Canonical PI Record v1"),
        ("institution_config.v1.schema.json", "Institution Adapter Config v1"),
        ("institution_config.v2.schema.json", "Institution Adapter Config v2"),
        ("pi_record.v2.schema.json", "Canonical PI Record v2"),
        ("matching_policy.v1.schema.json", "Matching Policy v1"),
        ("institution_registry.v1.schema.json", "Institution Registry v1"),
        ("institution_discovery_manifest.v1.schema.json", "Institution Discovery Manifest v1"),
    ],
)
def test_json_contract_files_are_parseable(filename, title):
    schema = json.loads((ROOT / "schemas" / filename).read_text(encoding="utf-8"))
    assert schema["title"] == title
    assert schema["$schema"].endswith("2020-12/schema")
