from __future__ import annotations

from pathlib import Path

from pi_index.adapters.institution_adapter import (
    ConfigDrivenInstitutionAdapter,
    discover_pagination_links,
)
from pi_index.crawl.fetcher import FetchResult
from pi_index.crawl.robots import RobotPolicy
from pi_index.config import load_institution_config
from pi_index.models import content_hash
from pi_index.storage import PIIndexStorage


ROOT = Path(__file__).parent.parent


class _FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def set_domain_delay(self, _url, _delay):
        return None

    def fetch(self, url, *, source_type="official_page", crawl_method="direct_fetch"):
        self.calls.append((url, source_type, crawl_method))
        value = self.pages[url]
        if isinstance(value, Exception):
            return FetchResult(
                url=url,
                final_url=url,
                status_code=None,
                text="",
                content_type=None,
                content_hash=content_hash(b""),
                error=str(value),
            )
        body = value.encode()
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            text=value,
            content_type="text/html; charset=utf-8",
            content_hash=content_hash(body),
            body=body,
            network_bytes=len(body),
        )


def _config(max_pages=10):
    return {
        "institution": {
            "name": "Example University",
            "homepage_url": "https://example.edu",
            "official_domains": ["example.edu"],
        },
        "pool_scope": {
            "type": "multi_unit",
            "name": "Computing and Engineering",
            "units": [
                {"name": "Computing", "seed_urls": ["https://example.edu/computing"]},
                {"name": "Engineering", "seed_urls": ["https://example.edu/engineering"]},
            ],
        },
        "crawl": {
            "seed_urls": ["https://example.edu/computing", "https://example.edu/engineering"],
            "max_depth": 1,
            "max_pages": max_pages,
            "crawl_delay_seconds": 0,
            "use_homepage_discovery": False,
            "allow_serp": False,
            "profile_url_patterns": ["/people/"],
        },
        "parsing": {"preferred_adapters": ["generic_html"]},
    }


def _pages(engineering_failure=False):
    return {
        "https://example.edu/computing": """
            <html><body>
              <a rel="next" href="/computing?page=2">Next</a>
              <a href="/people/jane-doe">Jane Doe</a>
            </body></html>
        """,
        "https://example.edu/engineering": RuntimeError("http_503") if engineering_failure else "<html><body>Engineering</body></html>",
        "https://example.edu/computing?page=2": "<html><body><a href='/people/john-smith'>John Smith</a></body></html>",
        "https://example.edu/people/jane-doe": "<html><head><title>Jane Doe</title></head><body><h1>Jane Doe</h1><p>Professor</p></body></html>",
        "https://example.edu/people/john-smith": "<html><head><title>John Smith</title></head><body><h1>John Smith</h1><p>Professor</p></body></html>",
    }


def _crawl(tmp_path, config, pages):
    storage = PIIndexStorage(tmp_path / "pool.db")
    fetcher = _FakeFetcher(pages)
    adapter = ConfigDrivenInstitutionAdapter(
        config,
        {"common_official_paths": []},
        fetcher,
        storage,
        "inst_example",
        "run-1",
    )
    outcome = adapter.crawl_and_parse()
    return storage, fetcher, outcome


def test_completeness_metrics_cover_units_pagination_and_profile_parsing(tmp_path):
    storage, fetcher, outcome = _crawl(tmp_path, _config(), _pages())
    metrics = outcome.metrics

    assert metrics["urls_discovered"] == 5
    assert metrics["pages_attempted"] == 5
    assert metrics["pages_succeeded"] == 5
    assert metrics["seed_url_coverage"] == 1.0
    assert metrics["unit_coverage"] == 1.0
    assert metrics["units_missing"] == []
    assert metrics["pagination_links_discovered"] == 1
    assert metrics["pagination_pages_succeeded"] == 1
    assert metrics["pagination_complete"] is True
    assert metrics["profile_links_discovered"] == 2
    assert metrics["profile_fetch_coverage"] == 1.0
    assert metrics["profile_parse_coverage"] == 1.0
    assert metrics["queue_exhausted"] is True
    assert metrics["max_pages_reached"] is False
    assert sum(len(people) for _result, people in outcome.parsed_results) == 2
    assert len({url for url, _source_type, _method in fetcher.calls}) == len(fetcher.calls)
    storage.close()


def test_max_pages_marks_pagination_incomplete(tmp_path):
    storage, _fetcher, outcome = _crawl(tmp_path, _config(max_pages=2), _pages())
    metrics = outcome.metrics

    assert metrics["pages_attempted"] == 2
    assert metrics["max_pages_reached"] is True
    assert metrics["queue_exhausted"] is False
    assert metrics["pagination_complete"] is False
    assert metrics["profile_fetch_coverage"] == 0.0
    storage.close()


def test_pagination_link_on_last_allowed_page_is_still_discovered(tmp_path):
    pages = _pages()
    pages["https://example.edu/computing"] = "<html><body>Computing</body></html>"
    pages["https://example.edu/engineering"] = (
        "<html><body><a rel='next' href='/engineering?page=2'>Next</a></body></html>"
    )
    pages["https://example.edu/engineering?page=2"] = "<html><body>More</body></html>"

    storage, _fetcher, outcome = _crawl(tmp_path, _config(max_pages=2), pages)

    assert outcome.metrics["pagination_links_discovered"] == 1
    assert outcome.metrics["max_pages_reached"] is True
    assert outcome.metrics["pagination_complete"] is False
    storage.close()


def test_failed_seed_identifies_missing_unit(tmp_path):
    storage, _fetcher, outcome = _crawl(tmp_path, _config(), _pages(engineering_failure=True))
    metrics = outcome.metrics

    assert metrics["seed_url_coverage"] == 0.5
    assert metrics["unit_coverage"] == 0.5
    assert metrics["units_missing"] == ["Engineering"]
    assert metrics["pages_failed"] == 1
    error = storage.conn.execute("SELECT run_id, stage, reason FROM crawl_errors").fetchone()
    assert dict(error) == {"run_id": "run-1", "stage": "fetch", "reason": "http_503"}
    storage.close()


def test_explicit_person_sitemap_profiles_are_measured_as_profile_pages(tmp_path):
    sitemap = "https://profiles.example.edu/sitemap/persons.xml"
    profile = "https://profiles.example.edu/en/persons/jane-doe/"
    pages = {
        sitemap: f"<?xml version='1.0'?><urlset><url><loc>{profile}</loc></url></urlset>",
        profile: "<html><head><title>Jane Doe</title></head><body><h1>Jane Doe</h1><p>Professor</p></body></html>",
    }
    config = _config()
    config["institution"]["official_domains"].append("profiles.example.edu")
    config["crawl"] = {
        "seed_urls": [sitemap],
        "sitemap_urls": [sitemap],
        "sitemap_url_patterns": [r"/en/persons/"],
        "max_depth": 0,
        "max_pages": 2,
        "crawl_delay_seconds": 0,
        "use_homepage_discovery": False,
        "allow_serp": False,
    }
    config["pool_scope"]["units"] = [{"name": "Whole institution", "seed_urls": [sitemap]}]

    storage, fetcher, outcome = _crawl(tmp_path, config, pages)

    assert [call[0] for call in fetcher.calls] == [sitemap, sitemap, profile]
    assert outcome.metrics["profile_links_discovered"] == 1
    assert outcome.metrics["profile_pages_attempted"] == 1
    assert outcome.metrics["profile_pages_parsed"] == 1
    assert outcome.metrics["profile_parse_coverage"] == 1.0
    storage.close()


def test_cityu_profile_override_follows_hesheng_official_profile_and_decodes_email(tmp_path):
    seed = "https://www.cityu.edu.hk/en/phy/faculty"
    profile = "https://scholars.cityu.edu.hk/en/persons/heshchen/"
    pages = {
        seed: """
            <div class="faculty-row">
              <h3><a href="https://scholars.cityu.edu.hk/en/persons/stale-id/">CHEN Hesheng</a></h3>
              <p>Professor</p>
            </div>
        """,
        profile: """
            <html><head><title>CHEN Hesheng | CityU Scholars</title></head><body>
              <h1>CHEN Hesheng</h1><p>Professor</p>
              <a class="email" data-md5="bWFpbHRvOmhlc2hjaGVuQGNpdHl1LmVkdS5oaw==" href="#">protected</a>
            </body></html>
        """,
    }
    config = _config(max_pages=3)
    config["institution"].update(
        {
            "name": "City University of Hong Kong",
            "homepage_url": "https://www.cityu.edu.hk",
            "official_domains": ["cityu.edu.hk"],
        }
    )
    config["pool_scope"]["units"] = [{"name": "Department of Physics", "seed_urls": [seed]}]
    config["crawl"].update(
        {
            "seed_urls": [seed],
            "profile_links_from_parsed_people_only": True,
            "profile_link_limit": 10,
        }
    )
    config["parsing"] = {
        "preferred_adapters": ["faculty_directory"],
        "profile_adapters": ["generic_html"],
        "profile_overrides": {"CHEN Hesheng": profile},
    }

    storage, fetcher, outcome = _crawl(tmp_path, config, pages)

    assert [call[0] for call in fetcher.calls] == [seed, profile]
    assert outcome.metrics["profile_links_discovered"] == 1
    assert outcome.metrics["profile_fetch_coverage"] == 1.0
    assert outcome.metrics["profile_parse_coverage"] == 1.0
    profile_people = [
        person
        for result, people in outcome.parsed_results
        if result.url == profile
        for person in people
    ]
    assert len(profile_people) == 1
    assert profile_people[0].name == "CHEN Hesheng"
    assert profile_people[0].emails == ["heshchen@cityu.edu.hk"]
    storage.close()


def test_excluded_parsed_person_profile_is_not_enqueued(tmp_path):
    seed = "https://example.edu/faculty"
    excluded_profile = "https://profiles.example.edu/people/jane-doe"
    pages = {
        seed: f"""
            <div class="faculty-row">
              <h3><a href="{excluded_profile}">Jane Doe</a></h3>
              <p>Professor</p>
            </div>
        """,
    }
    config = _config(max_pages=2)
    config["institution"]["official_domains"].append("profiles.example.edu")
    config["pool_scope"]["units"] = [{"name": "Faculty", "seed_urls": [seed]}]
    config["crawl"].update(
        {
            "seed_urls": [seed],
            "profile_links_from_parsed_people_only": True,
            "exclude_url_patterns": ["profiles.example.edu/people/"],
        }
    )
    config["parsing"] = {"preferred_adapters": ["faculty_directory"]}

    storage, fetcher, outcome = _crawl(tmp_path, config, pages)

    assert [call[0] for call in fetcher.calls] == [seed]
    assert outcome.metrics["urls_discovered"] == 1
    assert outcome.metrics["profile_links_discovered"] == 0
    assert outcome.metrics["profile_pages_attempted"] == 0
    storage.close()


def test_cityu_exact_legacy_shells_are_not_enqueued_but_valid_ee_profile_is(tmp_path):
    cityu = load_institution_config(ROOT / "configs" / "institutions" / "cityu_hk.yaml")
    seed = "https://www.ee.cityu.edu.hk/en/people/academic_staff/faculty"
    valid_profile = "https://www.ee.cityu.edu.hk/~ewong/"
    excluded_profiles = {
        "http://www.ee.cityu.edu.hk/~hangwong/",
        "http://www.ee.cityu.edu.hk/~rosachan/",
        "http://www.ee.cityu.edu.hk/~schan",
        "https://www.ds.cityu.edu.hk/en/people/academic-staff/professor-jonathan-zhu",
    }
    cards = [
        ("Eric Wing-Ming Wong", valid_profile),
        ("Hang Wong", "http://www.ee.cityu.edu.hk/~hangwong/"),
        ("Rosa Chan", "http://www.ee.cityu.edu.hk/~rosachan/"),
        ("S Chan", "http://www.ee.cityu.edu.hk/~schan"),
        (
            "Jonathan Zhu",
            "https://www.ds.cityu.edu.hk/en/people/academic-staff/professor-jonathan-zhu",
        ),
    ]
    seed_html = "".join(
        f'<div class="faculty-row"><h3><a href="{url}">{name}</a></h3><p>Professor</p></div>'
        for name, url in cards
    )
    pages = {
        seed: seed_html,
        valid_profile: """
            <html><head><title>Eric Wing-Ming Wong</title></head>
            <body><h1>Eric Wing-Ming Wong</h1><p>Associate Professor</p></body></html>
        """,
    }
    config = _config(max_pages=10)
    config["institution"] = {
        "name": "City University of Hong Kong",
        "homepage_url": "https://www.cityu.edu.hk",
        "official_domains": cityu["institution"]["official_domains"],
    }
    config["pool_scope"]["units"] = [
        {"name": "Department of Electrical Engineering", "seed_urls": [seed]}
    ]
    config["crawl"].update(
        {
            "seed_urls": [seed],
            "profile_links_from_parsed_people_only": True,
            "exclude_url_patterns": cityu["crawl"]["exclude_url_patterns"],
        }
    )
    config["parsing"] = {
        "preferred_adapters": ["faculty_directory"],
        "profile_adapters": ["generic_html"],
    }

    configured_exclusions = {
        value.casefold() for value in cityu["crawl"]["exclude_url_patterns"]
    }
    assert {
        "ee.cityu.edu.hk/~hangwong/",
        "ee.cityu.edu.hk/~rosachan/",
        "ee.cityu.edu.hk/~schan",
        "ds.cityu.edu.hk/en/people/academic-staff/professor-jonathan-zhu",
    } <= configured_exclusions

    storage, fetcher, outcome = _crawl(tmp_path, config, pages)

    fetched_urls = [call[0] for call in fetcher.calls]
    assert fetched_urls == [seed, valid_profile]
    assert excluded_profiles.isdisjoint(fetched_urls)
    assert outcome.metrics["profile_links_discovered"] == 1
    assert outcome.metrics["profile_pages_attempted"] == 1
    assert outcome.metrics["profile_parse_coverage"] == 1.0
    storage.close()


def test_robot_policy_fetches_rules_with_the_configured_user_agent(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        text = "User-agent: *\nDisallow: /private\nAllow: /public\n"

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("pi_index.crawl.robots.requests.get", fake_get)
    policy = RobotPolicy("pi-index-test/1.0", respect_robots=True)

    assert policy.can_fetch("https://example.edu/public/faculty") is True
    assert policy.can_fetch("https://example.edu/private/staff") is False
    assert len(calls) == 1
    assert calls[0][0] == "https://example.edu/robots.txt"
    assert calls[0][1]["headers"]["User-Agent"] == "pi-index-test/1.0"


def test_pagination_discovery_stays_within_the_current_directory_family():
    html = """
    <a href="/people/faculty/?pg=2">2</a>
    <a href="/research/seminars/?pg=265">Seminar archive</a>
    """

    assert discover_pagination_links(
        html,
        "https://example.edu/people/faculty/",
        ["example.edu"],
        {"pagination_url_patterns": ["?pg="]},
    ) == ["https://example.edu/people/faculty/?pg=2"]

    assert discover_pagination_links(
        '<a rel="next" href="/faculty-academics/page3#main">Next</a>',
        "https://example.edu/faculty-academics/page2",
        ["example.edu"],
    ) == ["https://example.edu/faculty-academics/page3"]
