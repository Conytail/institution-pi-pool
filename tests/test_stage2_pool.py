from pi_index.eval.stage2_pool import generated_institution_config, validate_cohort


def test_generated_stage2_config_is_official_seed_only():
    entry = {
        "name": "Example University",
        "country": "Example",
        "region": "Example",
        "ror_id": "https://ror.org/012345678",
        "homepage_url": "https://example.edu",
        "official_domains": ["example.edu", "science.example.edu"],
        "seed_url": "https://science.example.edu/people/faculty",
        "evaluation_domain": "Life Sciences",
        "pool_scope": "Biology faculty",
    }
    config = generated_institution_config(entry)
    assert config["schema_version"] == 1
    assert config["site"]["template_family"] == "generic_faculty_directory_v1"
    assert config["crawl"]["seed_urls"] == [entry["seed_url"]]
    assert config["crawl"]["use_homepage_discovery"] is False
    assert config["crawl"]["allow_serp"] is False
    assert config["crawl"]["max_depth"] == 0
    assert config["institution"]["ror_id"] == entry["ror_id"]


def test_stage2_manifest_requires_25_institutions_and_four_domains():
    entries = []
    domains = ["Physical Sciences", "Life Sciences", "Health Sciences", "Social Sciences"]
    for index in range(25):
        entries.append(
            {
                "slug": f"example-{index}",
                "ror_id": f"https://ror.org/{index:09d}",
                "evaluation_domain": domains[index % 4],
            }
        )
    validate_cohort({"institutions": entries})
