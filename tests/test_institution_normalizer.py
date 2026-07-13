from pi_index.normalize.institution import institution_from_config


def test_institution_from_config_uses_homepage_domain():
    record = institution_from_config(
        {
            "institution": {
                "name": "Example University",
                "homepage_url": "https://www.example.edu",
                "official_domains": ["example.edu"],
            }
        },
        use_ror=False,
    )
    assert record.name == "Example University"
    assert "example.edu" in record.official_domains


def test_live_ror_enrichment_does_not_change_configured_pool_id(monkeypatch):
    config = {
        "institution": {
            "name": "Example University",
            "homepage_url": "https://www.example.edu",
            "official_domains": ["example.edu"],
        }
    }
    without_enrichment = institution_from_config(config, use_ror=False)
    monkeypatch.setattr(
        "pi_index.normalize.institution.RORClient.normalize",
        lambda _client, _name: {
            "ror_id": "https://ror.org/012345678",
            "homepage_url": "https://different.example.edu",
            "aliases": ["Enriched Name"],
        },
    )
    with_enrichment = institution_from_config(config, use_ror=True)

    assert with_enrichment.institution_id == without_enrichment.institution_id
    assert with_enrichment.ror_id == "https://ror.org/012345678"
