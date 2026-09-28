from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import sqlite3


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "final_acceptance_audit.py"
_SPEC = importlib.util.spec_from_file_location("final_acceptance_audit", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
audit_database = _MODULE.audit_database


def _audit_record(person_id: str, *, profile_url: str, institution_id: str = "school-a"):
    return {
        "person_id": person_id,
        "institution_id": institution_id,
        "institution_name": "School A",
        "display_name": f"Person {person_id}",
        "aliases": [],
        "title": "Professor",
        "department": "Department A",
        "emails": [],
        "profile_url": profile_url,
        "profile_urls": [profile_url],
        "research_areas": [],
        "external_ids": {},
        "contact_confidence": "none",
        "topic_match_confidence": "unknown",
        "current_affiliation_confidence": "high",
        "payload": {},
    }


def _record_json(*, external_ids=None, profile_urls=None) -> str:
    return json.dumps(
        {
            "external_ids": external_ids or {},
            "profile_urls": profile_urls or [],
        }
    )


def test_final_acceptance_audit_separates_areas_publications_and_identity_groups(tmp_path):
    database = tmp_path / "acceptance.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE canonical_pi_records (
            person_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            institution_id TEXT NOT NULL,
            institution_name TEXT NOT NULL,
            title TEXT,
            department TEXT,
            profile_url TEXT,
            emails_json TEXT NOT NULL,
            research_areas_json TEXT NOT NULL,
            contact_confidence TEXT,
            topic_match_confidence TEXT,
            current_affiliation_confidence TEXT,
            record_json TEXT NOT NULL,
            membership_status TEXT
        );
        CREATE TABLE official_publication_fingerprints (
            fingerprint_id TEXT PRIMARY KEY,
            person_id TEXT NOT NULL,
            title TEXT NOT NULL,
            citation_text TEXT NOT NULL,
            publication_year INTEGER,
            doi TEXT,
            publication_url TEXT
        );
        CREATE TABLE pi_identity_aliases (
            alias_person_id TEXT PRIMARY KEY,
            canonical_person_id TEXT NOT NULL
        );
        CREATE TABLE pi_identity_keys (
            institution_id TEXT NOT NULL,
            person_id TEXT NOT NULL,
            identity_kind TEXT NOT NULL,
            identity_value TEXT NOT NULL
        );
        """
    )
    rows = [
        (
            "p1",
            "Same Name",
            "school-a",
            "School A",
            "Professor",
            "Department A",
            "https://example.edu/people/same-name",
            '["shared@example.edu"]',
            "[]",
            "high",
            "high",
            "high",
            _record_json(
                external_ids={"orcid": "0000-0001-2345-6789"},
                profile_urls=["https://example.edu/people/same-name"],
            ),
            "active",
        ),
        (
            "p2",
            "Same Name",
            "school-a",
            "School A",
            "Lecturer",
            "Department B",
            "https://example.edu/people/same-name",
            '["shared@example.edu"]',
            "[]",
            "high",
            "low",
            "high",
            _record_json(
                external_ids={"orcid_url": "https://orcid.org/0000-0001-2345-6789"},
                profile_urls=["https://example.edu/people/same-name/"],
            ),
            "active",
        ),
        (
            "p3",
            "Area Person",
            "school-b",
            "School B",
            None,
            "Department C",
            None,
            "[]",
            '["machine learning"]',
            "none",
            "high",
            "high",
            _record_json(),
            "active",
        ),
    ]
    connection.executemany(
        """
        INSERT INTO canonical_pi_records VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    connection.execute(
        "UPDATE canonical_pi_records SET record_json=? WHERE person_id='p3'",
        (json.dumps({"pi_supervisor_confidence": "high"}),),
    )
    connection.execute(
        """
        INSERT INTO official_publication_fingerprints VALUES
        ('f1', 'p1', 'A meaningful publication title', 'Journal of Tests (2025)', 2025, NULL, NULL)
        """
    )
    for person_id in ("p1", "p2"):
        connection.executemany(
            "INSERT INTO pi_identity_keys VALUES (?, ?, ?, ?)",
            [
                ("school-a", person_id, "__indexed__", "1"),
                ("school-a", person_id, "external:orcid", "0000-0001-2345-6789"),
                (
                    "school-a",
                    person_id,
                    "profile_exact",
                    "https://example.edu/people/same-name",
                ),
            ],
        )
    connection.execute(
        "INSERT INTO pi_identity_keys VALUES ('school-b', 'p3', '__indexed__', '1')"
    )
    connection.commit()
    connection.close()

    report = audit_database(database, sample_limit=2)

    assert report["read_only"] is True
    assert report["integrity"]["ok"] is True
    assert report["summary"]["active"] == 3
    assert report["summary"]["shared_email_groups"] == 1
    assert report["summary"]["strong_identity_duplicate_groups"] == {
        "orcid": 1,
        "openalex": 0,
        "profile": 1,
    }
    assert report["summary"]["same_name_groups"] == 1
    assert report["summary"]["active_non_person_display_names"] == 0
    assert report["summary"]["unusable_profile_urls"] == 0
    assert report["acceptance"]["checks"]["active_non_person_display_names_zero"] is True
    assert report["acceptance"]["checks"]["active_profile_urls_usable"] is True
    assert report["summary"]["legacy_supervision_field_records"] == 1
    assert report["legacy_supervision_fields"] == [
        {
            "table": "canonical_pi_records",
            "record_id": "p3",
            "name": "Area Person",
            "keys": ["pi_supervisor_confidence"],
        }
    ]
    assert report["strong_identity_duplicates"]["identity_index_complete"] is True

    schools = {item["school"]: item["metrics"] for item in report["schools"]}
    assert schools["School A"]["no_official_research_areas"] == 2
    assert schools["School A"][
        "no_official_areas_but_with_meaningful_official_publications"
    ] == 1
    assert schools["School A"][
        "no_official_areas_and_no_meaningful_official_publications"
    ] == 1
    assert schools["School B"]["with_official_research_areas"] == 1
    assert schools["School B"]["no_email"] == 1
    assert schools["School B"]["no_title"] == 1

    # The same anomalies become critical acceptance failures when they are
    # present on an active serving record.
    connection = sqlite3.connect(database)
    connection.execute(
        """
        UPDATE canonical_pi_records
        SET display_name='Page Not Found',
            profile_url='mailt:person@example.edu',
            record_json=?
        WHERE person_id='p3'
        """,
        (
            json.dumps(
                {
                    "pi_supervisor_confidence": "high",
                    "profile_urls": ["https://orcid.org/0000-0001-2345-6789"],
                }
            ),
        ),
    )
    connection.commit()
    connection.close()

    anomalous = audit_database(database, sample_limit=1)
    assert anomalous["summary"]["active_non_person_display_names"] == 1
    assert anomalous["summary"]["unusable_profile_urls"] == 2
    assert (
        anomalous["acceptance"]["checks"]["active_non_person_display_names_zero"]
        is False
    )
    assert anomalous["acceptance"]["checks"]["active_profile_urls_usable"] is False
    assert anomalous["acceptance"]["pass"] is False


def test_profile_identity_normalization_preserves_person_query_ids():
    first = _MODULE._normalize_profile("https://www.example.edu/profile.php?id=111&utm_source=x")
    second = _MODULE._normalize_profile("http://example.edu/profile.php?id=222")

    assert first == "example.edu/profile.php?id=111"
    assert second == "example.edu/profile.php?id=222"
    assert first != second


def test_active_serving_anomaly_rules_are_conservative_and_title_neutral():
    real = _audit_record(
        "anqi",
        profile_url="https://example.edu/persons/anqi-sun",
    )
    real.update(
        display_name="Anqi SUN",
        title="Research Assistant Professor",
        aliases=["SUN Anqi"],
    )
    honorary = _audit_record(
        "aelrun",
        profile_url="https://example.edu/profile/aelrun-goette",
    )
    honorary.update(display_name="Aelrun Goette", title="Honorary Lecturer")
    polluted = _audit_record(
        "polluted",
        profile_url="https://example.edu/people",
    )
    polluted.update(display_name="Selected Publications", title="Professor")

    result = _MODULE._active_serving_anomalies([real, honorary, polluted])

    assert result["non_person_display_names"] == [
        {
            "person_id": "polluted",
            "name": "Selected Publications",
            "school": "School A",
            "reason": "known_page_or_collective_heading",
        }
    ]
    assert result["unusable_profile_urls"] == [
        {
            "person_id": "polluted",
            "name": "Selected Publications",
            "school": "School A",
            "url": "https://example.edu/people",
            "locations": ["canonical_pi_records.profile_url", "profile_urls"],
            "reason": "aggregate_directory_url",
        }
    ]

    for legitimate_display in (
        "Dr. Ada T.T. Tian",
        "Research Assistant Professor Anqi Sun",
        "Benjamin Luke Moorhouse",
        "AOYAMA Reijiro",
    ):
        assert _MODULE._non_person_display_reason(legitimate_display) is None


def test_non_person_display_rules_cover_unmistakable_chrome_and_headlines():
    examples = {
        "Page Not Found": "known_page_or_collective_heading",
        "Course Taught": "known_page_or_collective_heading",
        "Select Publications (law/interdisciplinary)": "publication_section_heading",
        "COM's Scholarships & Awards": "awards_or_scholarship_heading",
        "Welcome to Lin Dai's HomePage": "homepage_chrome",
        "Congratulations to Dr. Example on Their Publication!": "news_headline",
        "Yao and Winston's paper accepted in IEEE IoT-J": "news_headline",
        "← Henry won the symposium poster presentation": "news_headline",
        "Another CALAS Graduate at top Position": "news_headline",
        "A Hub of Minds in a New Era": "site_slogan",
        "A mist that appears for a little time and then vanishes": (
            "quotation_or_sentence"
        ),
        "Vivian Yam. He": "biography_sentence_fragment",
    }

    assert {
        value: _MODULE._non_person_display_reason(value) for value in examples
    } == examples


def test_profile_url_anomaly_rules_preserve_person_identifiers_and_personal_roots():
    invalid = {
        "mailt:person@example.edu": "non_http_or_hostless_url",
        "https://orcid.org/0000-0001-2345-6789": "external_research_identity_url",
        "https://www.example.edu/error/404?item=person": "error_404_url",
        "https://example.edu/people": "aggregate_directory_url",
        "https://scholars.example.edu/": "generic_research_portal_root",
    }
    valid = (
        "https://example.edu/profile.php?id=111",
        "https://example.edu/academic-staff#AlexGearin",
        "https://example.edu/people/anqi-sun",
        "https://clarke.seas.harvard.edu/",
    )

    assert {
        value: _MODULE._profile_url_anomaly_reason(value) for value in invalid
    } == invalid
    for value in valid:
        assert _MODULE._profile_url_anomaly_reason(value) is None


def test_identity_index_must_cover_every_active_person_and_not_collapse_query_ids():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE pi_identity_keys (
            institution_id TEXT,
            person_id TEXT,
            identity_kind TEXT,
            identity_value TEXT
        )
        """
    )
    connection.executemany(
        "INSERT INTO pi_identity_keys VALUES (?, ?, ?, ?)",
        [
            ("school-a", "p1", "__indexed__", "1"),
            ("school-a", "p1", "profile_exact", "https://example.edu/profile.php?id=111"),
            ("school-a", "p2", "profile_exact", "https://example.edu/profile.php?id=222"),
        ],
    )
    records = [
        _audit_record("p1", profile_url="https://example.edu/profile.php?id=111"),
        _audit_record("p2", profile_url="https://example.edu/profile.php?id=222"),
    ]

    result = _MODULE._strong_identity_duplicates(connection, records, {}, {})

    assert result["counts"]["profile"] == 0
    assert result["identity_index_complete"] is False
    assert result["missing_active_person_ids"] == ["p2"]


def test_profile_exact_candidate_requires_name_or_person_local_email_compatibility():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE pi_identity_keys (
            institution_id TEXT,
            person_id TEXT,
            identity_kind TEXT,
            identity_value TEXT
        )
        """
    )
    shared = "https://example.edu/people/distinguished-visiting-professors"
    records = [
        _audit_record("p1", profile_url=shared),
        _audit_record("p2", profile_url=shared),
    ]
    records[0]["display_name"] = "Dacheng Tao"
    records[1]["display_name"] = "Sean Li"
    for record in records:
        record["payload"] = {"email_association": "none"}
        connection.executemany(
            "INSERT INTO pi_identity_keys VALUES (?, ?, ?, ?)",
            [
                ("school-a", record["person_id"], "__indexed__", "1"),
                ("school-a", record["person_id"], "profile_exact", shared),
            ],
        )

    unrelated = _MODULE._strong_identity_duplicates(connection, records, {}, {})

    assert unrelated["counts"]["profile"] == 0

    records[1]["display_name"] = "Tao Dacheng"
    compatible = _MODULE._strong_identity_duplicates(connection, records, {}, {})
    assert compatible["counts"]["profile"] == 1


def test_alias_integrity_rejects_alias_that_remains_a_canonical_row():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE canonical_pi_records (
            person_id TEXT PRIMARY KEY,
            institution_id TEXT,
            membership_status TEXT
        );
        CREATE TABLE pi_identity_aliases (
            alias_person_id TEXT PRIMARY KEY,
            canonical_person_id TEXT,
            institution_id TEXT
        );
        INSERT INTO canonical_pi_records VALUES ('p1', 'school-a', 'active');
        INSERT INTO canonical_pi_records VALUES ('p2', 'school-a', 'active');
        INSERT INTO pi_identity_aliases VALUES ('p2', 'p1', 'school-a');
        """
    )

    result = _MODULE._alias_integrity(connection, {"p2": "p1"})

    assert any(item["kind"] == "alias_still_canonical" for item in result["violations"])


def test_serving_json_audit_checks_inactive_nested_contact_and_invalid_payloads():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE canonical_pi_records (
            person_id TEXT PRIMARY KEY,
            display_name TEXT,
            record_json TEXT,
            pi_supervisor_confidence TEXT
        );
        CREATE TABLE contact_verdicts (
            person_id TEXT PRIMARY KEY,
            record_json TEXT
        );
        CREATE TABLE match_results (id INTEGER PRIMARY KEY);
        """
    )
    connection.executemany(
        "INSERT INTO canonical_pi_records VALUES (?, ?, ?, ?)",
        [
            (
                "inactive",
                "Inactive Person",
                json.dumps({"nested": {"supervision_score": 1}}),
                None,
            ),
            ("broken", "Broken Person", "{not-json", None),
        ],
    )
    connection.execute(
        "INSERT INTO contact_verdicts VALUES (?, ?)",
        ("inactive", json.dumps({"likely_supervisor_candidate": True})),
    )

    result = _MODULE._serving_json_audit(connection)

    assert {item["table"] for item in result["legacy_violations"]} == {
        "canonical_pi_records",
        "contact_verdicts",
    }
    assert any(
        "nested.supervision_score" in item["keys"]
        for item in result["legacy_violations"]
    )
    assert any(item.get("kind") == "physical_columns" for item in result["legacy_violations"])
    assert result["invalid_json"] == [
        {"table": "canonical_pi_records", "record_id": "broken", "error": "JSONDecodeError"}
    ]


def test_named_email_case_requires_exact_person_local_official_email_and_school():
    case = {
        "key": "brian_tang_law",
        "exact_names": ("Brian Tang",),
        "required_emails": ("bwtang@hku.hk",),
        "exact_title": "Principal Professional Practitioner",
        "expected_count": 1,
    }
    record = {
        **_audit_record(
            "brian",
            profile_url="https://www.law.hku.hk/academic_staff/brian-tang/",
            institution_id="hku",
        ),
        "institution_name": "The University of Hong Kong",
        "display_name": "Brian Tang",
        "title": "Principal Professional Practitioner",
        "emails": ["bwtang@hku.hk", "wrong@hku.hk"],
    }
    evidence = {"available": True, "pairs": {("brian", "bwtang@hku.hk")}}

    assert _MODULE._case_matches(case, record) is True
    failures = _MODULE._validate_case(case, [record], {}, evidence)
    assert "unexpected email(s) present: ['wrong@hku.hk']" in failures

    record["emails"] = ["bwtang@hku.hk"]
    assert _MODULE._validate_case(case, [record], {}, evidence) == []
    assert _MODULE._validate_case(case, [record], {}, {"available": True, "pairs": set()}) == [
        "official person-local evidence missing for email: bwtang@hku.hk"
    ]

    record["institution_name"] = "Another University"
    assert _MODULE._case_matches(case, record) is False


def test_named_cases_match_exact_official_aliases_but_not_fuzzy_variants():
    cityu = "City University of Hong Kong"
    variants = (
        (
            "andy_chow",
            "Affiliate Prof. CHOW Ho Fai Andy",
            ["Andy H.F. CHOW"],
        ),
        (
            "hesheng_chen",
            "陳和生 Hesheng CHEN",
            ["CHEN Hesheng", "Hesheng Chen"],
        ),
        (
            "benjamin_moorhouse",
            "Benjamin Luke MOORHOUSE",
            ["Benjamin Moorhouse"],
        ),
    )

    for key, display_name, aliases in variants:
        case = next(item for item in _MODULE.NAMED_CASES if item["key"] == key)
        record = _audit_record(key, profile_url="https://scholars.cityu.edu.hk/person")
        record.update(
            institution_name=cityu,
            display_name=display_name,
            aliases=aliases,
        )
        assert _MODULE._case_matches(case, record) is True

        record["aliases"] = [f"{aliases[0]} Junior"]
        assert _MODULE._case_matches(case, record) is False

        record["aliases"] = aliases
        record["institution_name"] = "Another University"
        assert _MODULE._case_matches(case, record) is False
