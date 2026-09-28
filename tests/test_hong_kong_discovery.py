from pathlib import Path

from pi_index.config import load_institution_config
from pi_index.discovery_contracts import (
    load_discovery_manifest,
    load_institution_registry,
)


ROOT = Path(__file__).parent.parent
REGISTRY_PATH = ROOT / "data" / "institutions" / "hong_kong" / "institution_registry.v1.yaml"

EXPECTED_INSTITUTIONS = {
    "cityu_hk",
    "hkbu",
    "lingnan_hk",
    "cuhk",
    "eduhk",
    "polyu",
    "hkust",
    "hku",
}

EXPECTED_UNIT_COUNTS = {
    "cityu_hk": 32,
    "hkbu": 7,
    "lingnan_hk": 6,
    "cuhk": 9,
    "eduhk": 5,
    "polyu": 10,
    "hkust": 7,
    "hku": 14,
}


def test_hong_kong_registry_has_the_complete_ugc_sector_without_overclaiming_region():
    registry = load_institution_registry(REGISTRY_PATH)
    ids = {item["institution_id"] for item in registry["institutions"]}

    assert ids == EXPECTED_INSTITUTIONS
    assert registry["selection"]["included_count"] == 8
    assert registry["selection"]["sector_complete"] is True
    assert registry["selection"]["region_complete"] is False
    assert registry["selection"]["policy"] == "all_ugc_funded_universities"
    assert registry["baseline_tag"] == "baseline-v0.1"


def test_every_registry_entry_has_a_matching_complete_discovery_manifest():
    registry = load_institution_registry(REGISTRY_PATH)

    for entry in registry["institutions"]:
        path = ROOT / entry["discovery_manifest"]
        assert path.is_file(), path

        manifest = load_discovery_manifest(path)
        assert manifest["institution_id"] == entry["institution_id"]
        assert manifest["identity"]["official_name"] == entry["official_name"]
        assert manifest["identity"]["ror_id"] == entry["ror_id"]
        assert set(manifest["identity"]["official_domains"]) == set(entry["official_domains"])
        assert manifest["baseline_tag"] == registry["baseline_tag"]
        assert manifest["coverage"]["top_level_unit_count"] == EXPECTED_UNIT_COUNTS[entry["institution_id"]]
        assert manifest["coverage"]["discovery_status"] == "complete"


def test_discovery_manifests_use_title_neutral_membership():
    registry = load_institution_registry(REGISTRY_PATH)

    for entry in registry["institutions"]:
        manifest = load_discovery_manifest(ROOT / entry["discovery_manifest"])
        assert manifest["scope"]["population"] == "academic_and_research_personnel"
        assert "without inferring eligibility from title" in manifest["scope"]["membership_policy"]
        roles = {source["role"] for source in manifest["sources"]}
        assert "organizational_scope" in roles
        assert "research_degree" in roles
        assert roles.intersection({"people_directory", "research_profile"})


def test_every_discovered_hong_kong_institution_has_a_runnable_config_and_batch_row():
    registry = load_institution_registry(REGISTRY_PATH)

    assert all(entry["adapter_config_status"] in {"in_progress", "validated"} for entry in registry["institutions"])
    existing_config_names = {path.stem for path in (ROOT / "configs" / "institutions").glob("*.yaml")}
    assert EXPECTED_INSTITUTIONS.issubset(existing_config_names)

    batch_text = (
        ROOT / "data" / "institutions" / "hong_kong" / "ugc_batch.v1.csv"
    ).read_text(encoding="utf-8")
    for institution_id in EXPECTED_INSTITUTIONS:
        assert f"configs/institutions/{institution_id}.yaml" in batch_text


def test_large_hong_kong_directories_have_non_truncating_profile_crawl_budgets():
    minimum_profile_budgets = {
        "hku": 2500,
        "cityu_hk": 1200,
        "polyu": 1700,
    }

    for institution_id, minimum_profiles in minimum_profile_budgets.items():
        config = load_institution_config(ROOT / "configs" / "institutions" / f"{institution_id}.yaml")
        crawl = config["crawl"]
        profile_limit = crawl["profile_link_limit"]
        seed_count = len(crawl["seed_urls"])

        assert profile_limit >= minimum_profiles
        # Seeds, discovered profiles and paginated directory pages share the
        # same queue budget.  Preserve explicit headroom so complete profile
        # discovery cannot still be silently cut off by max_pages.
        assert crawl["max_pages"] >= seed_count + profile_limit + 50
        assert config["quality_gate"]["minimum_profile_fetch_coverage"] >= 0.95
        assert config["quality_gate"]["require_pagination_complete"] is True


def test_cityu_profile_chain_keeps_multi_person_official_cards():
    config = load_institution_config(
        ROOT / "configs" / "institutions" / "cityu_hk.yaml"
    )

    assert "cityu_multi_person_profile" in config["parsing"]["profile_adapters"]
    adjunct_url = (
        "https://www.en.cityu.edu.hk/en/our-people/adjunct-visiting-professors"
    )
    english_unit = next(
        unit
        for unit in config["pool_scope"]["units"]
        if unit["name"] == "Department of English"
    )
    assert adjunct_url in english_unit["seed_urls"]
    assert adjunct_url in config["crawl"]["seed_urls"]
