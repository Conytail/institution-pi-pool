from __future__ import annotations

from dataclasses import replace
import sqlite3

from pi_index import storage as storage_module
from pi_index.models import (
    CanonicalPIRecord,
    EmailEvidence,
    OfficialPublicationFingerprint,
)
from pi_index.pipeline.ingest_institution import _merge_records
from pi_index.storage import PIIndexStorage, is_unusable_profile_url, normalize_profile_url


def _pi(**overrides) -> CanonicalPIRecord:
    values = {
        "person_id": "pi_0123456789abcdef",
        "display_name": "Jane Doe",
        "given_name": "Jane",
        "family_name": "Doe",
        "aliases": [],
        "institution_id": "inst_example",
        "institution_name": "Example University",
        "ror_id": None,
        "department": "Computer Science",
        "title": "Professor",
        "profile_url": "https://example.edu/people/jane-doe",
        "lab_url": None,
        "emails": ["jane@example.edu"],
        "research_areas": ["machine learning"],
        "publications_summary": {},
        "external_ids": {},
        "source_evidence_ids": [],
        "last_checked_at": "2026-01-01T00:00:00+00:00",
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "last_seen_at": "2026-01-01T00:00:00+00:00",
        "last_seen_run_id": "run-1",
        "departments": ["Computer Science"],
        "profile_urls": ["https://example.edu/people/jane-doe"],
        "field_sources": {
            "display_name": "official_directory",
            "title": "official_directory",
            "profile_url": "official_directory",
            "emails": "official_directory",
        },
        "email_association": "person_local",
    }
    values.update(overrides)
    return CanonicalPIRecord(**values)


def test_profile_url_normalization_removes_transport_and_tracking_noise():
    left = "HTTPS://WWW.Example.edu/people/Jane-Doe/?utm_source=test#bio"
    right = "http://example.edu/people/jane-doe"
    assert normalize_profile_url(left) == normalize_profile_url(right)


def test_unusable_profile_url_rejects_non_web_schemes_and_404_documents():
    assert is_unusable_profile_url("mailt:person@example.edu") is True
    assert is_unusable_profile_url("mailto:person@example.edu") is True
    assert is_unusable_profile_url("javascript:void(0)") is True
    assert is_unusable_profile_url("https://example.edu/error/404?item=/person") is True
    assert is_unusable_profile_url("https://scholars.example.edu/") is True
    assert is_unusable_profile_url("https://example.edu/people/person") is False
    assert is_unusable_profile_url("https://clarke.seas.harvard.edu/") is False


def test_profile_url_normalization_preserves_identity_bearing_fragments():
    alex = "https://mehu.hku.hk/academic-staff#AlexGearin"
    carl = "https://mehu.hku.hk/academic-staff#CarlHildebrand"

    assert normalize_profile_url(alex) != normalize_profile_url(carl)
    assert normalize_profile_url(alex).endswith("#alexgearin")


def test_external_research_ids_are_not_materialized_as_official_profile_keys():
    record = _pi(
        person_id="pi_external_links",
        display_name="Benjamin Moorhouse",
        profile_url="https://scholars.cityu.edu.hk/en/persons/bmoorhou/",
        profile_urls=[
            "https://scholars.cityu.edu.hk/en/persons/bmoorhou/",
            "https://orcid.org/0000-0002-3913-5194",
            "https://www.scopus.com/authid/detail.uri?authorId=57195513283",
            "https://www.cityu.edu.hk/error/404?item=/broken-profile",
        ],
    )

    entries = storage_module._identity_index_entries(record)

    profile_values = {
        value for kind, value in entries if kind in {"profile", "profile_exact"}
    }
    assert profile_values == {"scholars.cityu.edu.hk/en/persons/bmoorhou"}


def test_strong_identity_ids_merge_name_aliases_but_arbitrary_external_ids_do_not(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    existing = _pi(external_ids={"orcid": "0000-0001-2345-6789"})
    storage.upsert_pi_record(existing)

    orcid_alias = _pi(
        person_id="pi_orcid_alias",
        display_name="Completely Different Display Name",
        profile_url="https://example.edu/profiles/opaque-1",
        profile_urls=["https://example.edu/profiles/opaque-1"],
        emails=[],
        external_ids={"orcid_url": "https://orcid.org/0000-0001-2345-6789"},
    )
    assert storage.find_existing_duplicate(orcid_alias) == (existing.person_id, "same_orcid")

    openalex = _pi(
        person_id="pi_openalex",
        display_name="John Smith",
        profile_url="https://example.edu/people/john-smith",
        profile_urls=["https://example.edu/people/john-smith"],
        emails=[],
        external_ids={"openalex_author_id": "https://openalex.org/A123456789"},
    )
    storage.upsert_pi_record(openalex)
    openalex_alias = replace(
        openalex,
        person_id="pi_openalex_alias",
        display_name="J. Smith",
        profile_url="https://example.edu/profiles/opaque-2",
        profile_urls=["https://example.edu/profiles/opaque-2"],
        external_ids={"openalex_id": "a123456789"},
    )
    assert storage.find_existing_duplicate(openalex_alias) == (
        openalex.person_id,
        "same_openalex_author_id",
    )

    arbitrary = _pi(
        person_id="pi_arbitrary",
        display_name="Alice Jones",
        profile_url="https://example.edu/people/alice-jones",
        profile_urls=["https://example.edu/people/alice-jones"],
        emails=[],
        external_ids={"researchgate_url": "https://researchgate.net/profile/shared"},
    )
    storage.upsert_pi_record(arbitrary)
    unrelated = replace(
        arbitrary,
        person_id="pi_unrelated",
        display_name="Bob Brown",
        profile_url="https://example.edu/people/bob-brown",
        profile_urls=["https://example.edu/people/bob-brown"],
    )
    assert storage.find_existing_duplicate(unrelated) is None
    storage.close()


def test_profile_and_email_identity_rules_reject_shared_pages_and_misbound_email(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    existing = _pi(display_name="Alex Kwan Yue JEN", emails=["alexjen@example.edu"])
    storage.upsert_pi_record(existing)

    email_alias = _pi(
        person_id="pi_email_alias",
        display_name="K. Y. Alex JEN",
        profile_url="https://example.edu/profiles/alex-jen",
        profile_urls=["https://example.edu/profiles/alex-jen"],
        emails=["alexjen@example.edu"],
    )
    assert storage.find_existing_duplicate(email_alias) == (
        existing.person_id,
        "same_email_with_name_alias",
    )

    misbound = replace(
        email_alias,
        person_id="pi_misbound",
        display_name="Lawrence Wu",
        profile_url="https://example.edu/profiles/lawrence-wu",
        profile_urls=["https://example.edu/profiles/lawrence-wu"],
    )
    assert storage.find_existing_duplicate(misbound) is None

    ambiguous = replace(
        email_alias,
        person_id="pi_ambiguous_email",
        email_association="ambiguous_email",
    )
    assert storage.find_existing_duplicate(ambiguous) is None

    same_name_only = replace(
        email_alias,
        person_id="pi_same_name",
        display_name=existing.display_name,
        profile_url="https://example.edu/profiles/another-alex-jen",
        profile_urls=["https://example.edu/profiles/another-alex-jen"],
        emails=[],
    )
    assert storage.find_existing_duplicate(same_name_only) is None

    shared_page = _pi(
        person_id="pi_shared",
        display_name="Alice Smith",
        profile_url="https://example.edu/people/affiliates/",
        profile_urls=["https://example.edu/people/affiliates/"],
        emails=[],
    )
    storage.upsert_pi_record(shared_page)
    another_on_shared_page = replace(
        shared_page,
        person_id="pi_shared_other",
        display_name="Bob Jones",
    )
    assert storage.find_existing_duplicate(another_on_shared_page) is None

    same_surname_on_misbound_profile = _pi(
        person_id="pi_same_surname_profile",
        display_name="Bob Chen",
        profile_url="https://example.edu/people/alice-chen",
        profile_urls=["https://example.edu/people/alice-chen"],
        emails=[],
    )
    storage.upsert_pi_record(
        replace(
            same_surname_on_misbound_profile,
            person_id="pi_alice_chen",
            display_name="Alice Chen",
        )
    )
    assert storage.find_existing_duplicate(same_surname_on_misbound_profile) is None

    role_address = _pi(
        person_id="pi_role_address",
        display_name="Chris Wong",
        profile_url="https://example.edu/people/chris-wong",
        profile_urls=["https://example.edu/people/chris-wong"],
        emails=["graduate.office@example.edu"],
    )
    storage.upsert_pi_record(role_address)
    role_address_alias = replace(
        role_address,
        person_id="pi_role_address_alias",
        profile_url="https://example.edu/profiles/chris-wong-new",
        profile_urls=["https://example.edu/profiles/chris-wong-new"],
    )
    assert storage.find_existing_duplicate(role_address_alias) is None
    storage.close()


def test_pure_uuid_identity_links_directory_alias_without_unsafe_email_merge(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    directory = _pi(
        display_name="Ada T.T. Tian",
        profile_url=(
            "https://research.polyu.edu.hk/en/persons/"
            "0ee58ae4-e8fa-4be4-9add-eda8c3f9c5c9"
        ),
        profile_urls=[
            "https://research.polyu.edu.hk/en/persons/"
            "0ee58ae4-e8fa-4be4-9add-eda8c3f9c5c9"
        ],
        emails=["tingting.tian@polyu.edu.hk"],
    )
    storage.upsert_pi_record(directory)
    pure_profile = _pi(
        person_id="pi_tingting_tian_pure",
        display_name="Tingting Tian",
        profile_url="https://research.polyu.edu.hk/en/persons/tingting-tian/",
        profile_urls=["https://research.polyu.edu.hk/en/persons/tingting-tian/"],
        emails=["tingting.tian@polyu.edu.hk"],
        email_association="ambiguous_email",
        external_ids={
            "official_person_id": (
                "research.polyu.edu.hk:uuid:"
                "0ee58ae4-e8fa-4be4-9add-eda8c3f9c5c9"
            ),
            "scopus_author_id": "57209692154",
        },
    )

    # Their names are intentionally not compatible and the profile email is
    # ambiguous.  The first-party Pure UUID is the sole merge authority.
    assert storage.find_existing_duplicate(pure_profile) == (
        directory.person_id,
        "same_official_person_id",
    )
    storage.close()


def test_exact_profile_and_person_email_repairs_truncated_fung_name_safely(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    truncated = _pi(
        display_name="Mr Keith",
        profile_url="https://example.edu/people/academic-staff/mr-keith-fung/",
        profile_urls=["https://example.edu/people/academic-staff/mr-keith-fung/"],
        emails=["keith.fung@example.edu"],
    )
    storage.upsert_pi_record(truncated)
    corrected = _pi(
        person_id="pi_corrected_keith_fung",
        display_name="Keith FUNG",
        profile_url="https://example.edu/people/academic-staff/mr-keith-fung/",
        profile_urls=["https://example.edu/people/academic-staff/mr-keith-fung/"],
        emails=["keith.fung@example.edu"],
    )

    assert storage.find_existing_duplicate(corrected) == (
        truncated.person_id,
        "same_profile_url_and_person_email",
    )

    # Either anchor alone remains insufficient when the corrected and legacy
    # names share only one meaningful token.
    same_profile_without_email = replace(
        corrected,
        person_id="pi_same_profile_only",
        emails=[],
    )
    assert storage.find_existing_duplicate(same_profile_without_email) is None
    same_email_different_profile = replace(
        corrected,
        person_id="pi_same_email_only",
        profile_url="https://example.edu/people/academic-staff/another-keith/",
        profile_urls=["https://example.edu/people/academic-staff/another-keith/"],
    )
    assert storage.find_existing_duplicate(same_email_different_profile) is None
    storage.close()


def test_exact_opaque_official_profile_merges_compatible_name_punctuation(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    directory = _pi(
        display_name="Catherine K. K. CHAN",
        profile_url="https://web.edu.hku.hk/faculty-academics/kkcc7950",
        profile_urls=["https://web.edu.hku.hk/faculty-academics/kkcc7950"],
        emails=[],
        email_association="none",
    )
    storage.upsert_pi_record(directory)
    profile = replace(
        directory,
        person_id="pi_catherine_profile",
        display_name="Catherine K. K CHAN",
        field_sources={**directory.field_sources, "profile_url": "official_profile"},
    )

    assert storage.find_existing_duplicate(profile) == (
        directory.person_id,
        "same_profile_url_with_name_alias",
    )
    storage.close()


def test_exact_numeric_opaque_profile_id_is_not_rejected_as_too_short(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    directory = _pi(
        display_name="Jin Jian",
        profile_url="https://web.chinese.hku.hk/en/people/staff/34/",
        profile_urls=["https://web.chinese.hku.hk/en/people/staff/34/"],
        emails=[],
        email_association="none",
    )
    storage.upsert_pi_record(directory)
    profile = replace(
        directory,
        person_id="pi_jin_jian_profile",
        display_name="Ms. JIN Jian",
        field_sources={**directory.field_sources, "profile_url": "official_profile"},
    )

    assert storage.find_existing_duplicate(profile) == (
        directory.person_id,
        "same_profile_url_with_name_alias",
    )
    storage.close()


def test_exact_profile_supports_ordered_initial_to_full_name_alias(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    directory = _pi(
        display_name="J.C. Chen",
        profile_url="https://www.dase.hku.hk/people/j-c-chen",
        profile_urls=["https://www.dase.hku.hk/people/j-c-chen"],
        emails=[],
        email_association="none",
    )
    storage.upsert_pi_record(directory)
    profile = replace(
        directory,
        person_id="pi_jiangcheng_chen",
        display_name="Jiangcheng Chen",
        field_sources={**directory.field_sources, "profile_url": "official_profile"},
    )

    assert storage.find_existing_duplicate(profile) == (
        directory.person_id,
        "same_profile_url_with_name_alias",
    )
    storage.close()


def test_exact_profile_supports_compact_uppercase_initials(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    directory = _pi(
        display_name="Wai-Pan Peter YAU",
        profile_url="https://www.ortho.hku.hk/biography/yau-wai-pan/",
        profile_urls=["https://www.ortho.hku.hk/biography/yau-wai-pan/"],
        emails=[],
        email_association="none",
    )
    storage.upsert_pi_record(directory)
    profile = replace(
        directory,
        person_id="pi_wp_yau",
        display_name="WP Yau",
        field_sources={**directory.field_sources, "profile_url": "official_profile"},
    )

    assert storage.find_existing_duplicate(profile) == (
        directory.person_id,
        "same_profile_url_with_name_alias",
    )
    storage.close()


def test_exact_profile_initial_alias_requires_a_shared_complete_token(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    existing = _pi(
        display_name="C. Sun",
        profile_url="https://example.edu/people/opaque-person",
        profile_urls=["https://example.edu/people/opaque-person"],
        emails=[],
        email_association="none",
    )
    storage.upsert_pi_record(existing)
    unrelated = replace(
        existing,
        person_id="pi_unrelated_initial",
        display_name="Chen Moon",
    )

    assert storage.find_existing_duplicate(unrelated) is None
    storage.close()


def test_exact_shared_aggregate_url_does_not_merge_distinct_people(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    first = _pi(
        display_name="Alex Smith",
        profile_url="https://example.edu/people/academic-staff",
        profile_urls=["https://example.edu/people/academic-staff"],
        emails=[],
        email_association="none",
    )
    storage.upsert_pi_record(first)
    second = replace(
        first,
        person_id="pi_second_alex_smith",
        display_name="Alex J. Smith",
    )

    assert storage.find_existing_duplicate(second) is None
    storage.close()


def test_distinct_fragment_people_on_shared_directory_do_not_collide(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    alex = _pi(
        display_name="Alex Gearin",
        profile_url="https://mehu.hku.hk/academic-staff#AlexGearin",
        profile_urls=["https://mehu.hku.hk/academic-staff#AlexGearin"],
        emails=[],
        email_association="none",
    )
    storage.upsert_pi_record(alex)
    carl = replace(
        alex,
        person_id="pi_carl_hildebrand",
        display_name="Carl Hildebrand",
        profile_url="https://mehu.hku.hk/academic-staff#CarlHildebrand",
        profile_urls=["https://mehu.hku.hk/academic-staff#CarlHildebrand"],
    )

    assert storage.find_existing_duplicate(carl) is None
    storage.close()


def test_same_school_same_name_cross_host_slug_with_conflicting_identity_stays_split(
    tmp_path,
):
    storage = PIIndexStorage(tmp_path / "pool.db")
    biomedical = _pi(
        person_id="pi_hku_biomedical_yang_liu",
        display_name="Yang Liu",
        department="School of Biomedical Sciences",
        departments=["School of Biomedical Sciences"],
        profile_url="https://www.sbms.hku.hk/staff/yang-liu",
        profile_urls=["https://www.sbms.hku.hk/staff/yang-liu"],
        emails=["yangliu9@hku.hk"],
        external_ids={},
    )
    storage.upsert_pi_record(biomedical)
    business = _pi(
        person_id="pi_hku_business_yang_liu",
        display_name="Yang LIU",
        department="HKU Business School",
        departments=["HKU Business School"],
        profile_url="https://www.hkubs.hku.hk/people/yang-liu/",
        profile_urls=["https://www.hkubs.hku.hk/people/yang-liu/"],
        emails=["yangliu5@hku.hk"],
        external_ids={},
    )

    assert storage.find_existing_duplicate(business) is None
    storage.close()


def test_same_parent_domain_slug_with_two_official_identity_conflicts_stays_split(
    tmp_path,
):
    storage = PIIndexStorage(tmp_path / "pool.db")
    computer_science = _pi(
        person_id="pi_cs_jane_doe",
        department="Computer Science",
        departments=["Computer Science"],
        profile_url="https://cs.example.edu/people/jane-doe",
        profile_urls=["https://cs.example.edu/people/jane-doe"],
        emails=[],
        email_association="none",
    )
    storage.upsert_pi_record(computer_science)
    medicine = replace(
        computer_science,
        person_id="pi_medicine_jane_doe",
        department="School of Medicine",
        departments=["School of Medicine"],
        profile_url="https://medicine.example.edu/faculty/jane-doe",
        profile_urls=["https://medicine.example.edu/faculty/jane-doe"],
    )

    # The registrable-domain slug nominates this pair, but independent official
    # profile and department conflicts are sufficient to block a weak merge.
    assert storage.find_existing_duplicate(medicine) is None
    storage.close()


def test_strong_shared_orcid_can_override_cross_profile_contact_conflicts(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    first = _pi(
        person_id="pi_cross_appointment_first",
        department="School of Medicine",
        departments=["School of Medicine"],
        profile_url="https://medicine.example.edu/staff/jane-doe",
        profile_urls=["https://medicine.example.edu/staff/jane-doe"],
        emails=["jane.medicine@example.edu"],
        external_ids={"orcid": "0000-0001-2345-6789"},
    )
    storage.upsert_pi_record(first)
    second = _pi(
        person_id="pi_cross_appointment_second",
        department="Business School",
        departments=["Business School"],
        profile_url="https://business.example.edu/people/jane-doe",
        profile_urls=["https://business.example.edu/people/jane-doe"],
        emails=["jane.business@example.edu"],
        external_ids={"orcid_url": "https://orcid.org/0000-0001-2345-6789"},
    )

    assert storage.find_existing_duplicate(second) == (
        first.person_id,
        "same_orcid",
    )
    storage.close()


def test_identity_index_algorithm_upgrade_rebuilds_rows_with_existing_sentinel(tmp_path):
    db_path = tmp_path / "pool.db"
    storage = PIIndexStorage(db_path)
    record = _pi(
        display_name="Catherine K. K. CHAN",
        profile_url="https://web.edu.hku.hk/faculty-academics/kkcc7950",
        profile_urls=["https://web.edu.hku.hk/faculty-academics/kkcc7950"],
        emails=[],
        email_association="none",
    )
    storage.upsert_pi_record(record)
    storage.close()

    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "DELETE FROM pi_identity_keys WHERE identity_kind='profile_exact'"
    )
    legacy.execute(
        "UPDATE schema_meta SET value='1-legacy' WHERE key='pi_identity_index_schema'"
    )
    assert legacy.execute(
        "SELECT COUNT(*) FROM pi_identity_keys WHERE identity_kind='__indexed__'"
    ).fetchone()[0] == 1
    legacy.commit()
    legacy.close()

    upgraded = PIIndexStorage(db_path)
    assert upgraded.conn.execute(
        """
        SELECT COUNT(*) FROM pi_identity_keys
        WHERE person_id=? AND identity_kind='profile_exact'
        """,
        (record.person_id,),
    ).fetchone()[0] == 1
    incoming = replace(
        record,
        person_id="pi_catherine_profile",
        display_name="Catherine K. K CHAN",
        field_sources={**record.field_sources, "profile_url": "official_profile"},
    )
    assert upgraded.find_existing_duplicate(incoming) == (
        record.person_id,
        "same_profile_url_with_name_alias",
    )
    upgraded.close()


def test_unresolved_duplicate_audit_counts_canonical_collisions_not_merge_history(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    first = _pi(external_ids={"orcid": "0000-0001-2345-6789"})
    second = _pi(
        person_id="pi_second_orcid_record",
        display_name="J. Doe",
        profile_url="https://example.edu/profiles/opaque-jd",
        profile_urls=["https://example.edu/profiles/opaque-jd"],
        emails=[],
        external_ids={"orcid_url": "https://orcid.org/0000-0001-2345-6789"},
    )
    same_name_only = _pi(
        person_id="pi_same_name_only",
        profile_url="https://example.edu/people/another-jane-doe",
        profile_urls=["https://example.edu/people/another-jane-doe"],
        emails=[],
        external_ids={},
    )
    for record in (first, second, same_name_only):
        storage.upsert_pi_record(record)

    assert storage.find_unresolved_duplicate_groups("inst_example") == [
        sorted([first.person_id, second.person_id])
    ]

    storage.consolidate_person_ids(
        second.person_id,
        first.person_id,
        first.institution_id,
        "same_orcid",
        "run-2",
    )
    storage.record_duplicate(
        first.institution_id,
        "inst_example|identity|pi_0123456789abcdef",
        first.person_id,
        second.person_id,
        "same_orcid",
        "run-2",
    )

    # The historical merge is evidence that dedupe succeeded, while the
    # same-name-only record remains intentionally distinct.
    assert storage.find_unresolved_duplicate_groups("inst_example") == []
    assert storage.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 1
    storage.close()


def test_same_name_profile_script_query_ids_remain_distinct(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    existing = _pi(
        display_name="Wei Wang",
        profile_url="https://example.edu/profile.php?id=111",
        profile_urls=["https://example.edu/profile.php?id=111"],
        emails=[],
    )
    storage.upsert_pi_record(existing)
    distinct = replace(
        existing,
        person_id="pi_distinct_wei_wang",
        profile_url="https://example.edu/profile.php?id=222",
        profile_urls=["https://example.edu/profile.php?id=222"],
    )

    assert storage.find_existing_duplicate(distinct) is None
    different_system_same_query = replace(
        existing,
        person_id="pi_other_system_wei_wang",
        profile_url="https://profiles.example.edu/profile.php?id=111",
        profile_urls=["https://profiles.example.edu/profile.php?id=111"],
    )
    assert storage.find_existing_duplicate(different_system_same_query) is None
    storage.close()


def test_identity_lookup_deserializes_only_sql_nominated_candidates(tmp_path, monkeypatch):
    storage = PIIndexStorage(tmp_path / "pool.db")
    for index in range(500):
        storage.upsert_pi_record(
            _pi(
                person_id=f"pi_bulk_{index:04d}",
                display_name=f"Researcher Alpha {index}",
                profile_url=f"https://example.edu/people/researcher-alpha-{index}",
                profile_urls=[f"https://example.edu/people/researcher-alpha-{index}"],
                emails=[f"researcher{index}@example.edu"],
            )
        )

    calls = 0
    original = storage_module._pi_from_json

    def counted_pi_from_json(value):
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(storage_module, "_pi_from_json", counted_pi_from_json)
    incoming = _pi(
        person_id="pi_incoming_bulk_alias",
        display_name="Alpha Researcher 237",
        profile_url="https://example.edu/profiles/opaque-identity",
        profile_urls=["https://example.edu/profiles/opaque-identity"],
        emails=["researcher237@example.edu"],
    )

    assert storage.find_existing_duplicate(incoming) == (
        "pi_bulk_0237",
        "same_email_with_name_alias",
    )
    assert calls == 1
    assert storage.conn.execute("SELECT COUNT(*) FROM canonical_pi_records").fetchone()[0] == 500
    storage.close()


def test_quality_merge_prefers_clean_profile_fields_and_keeps_all_affiliations():
    existing = _pi(
        display_name="Alex",
        given_name=None,
        family_name=None,
        department="Journalism and Media Studies Centre",
        departments=["Journalism and Media Studies Centre"],
        title=(
            "Professor Richard Allen is a veteran journalist. He joined the university "
            "after working in international news organisations for many years."
        ),
        profile_url="https://example.edu/people/academic-staff",
        profile_urls=["https://example.edu/people/academic-staff"],
        emails=["someone.else@example.edu"],
        external_ids={"scopus_author_id": "123456789"},
    )
    incoming = _pi(
        person_id="pi_incoming",
        display_name="Richard Allen",
        given_name="Richard",
        family_name="Allen",
        department="Department of Media and Communication",
        departments=["Department of Media and Communication"],
        title="Professor of Journalism",
        profile_url="https://example.edu/people/richard-allen",
        profile_urls=["https://example.edu/people/richard-allen"],
        emails=["richard.allen@example.edu"],
        external_ids={"orcid": "0000-0001-2345-6789"},
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )

    merged = _merge_records(existing, incoming)

    assert merged.display_name == "Richard Allen"
    assert merged.title == "Professor of Journalism"
    assert merged.emails == ["richard.allen@example.edu"]
    assert merged.department == (
        "Journalism and Media Studies Centre; Department of Media and Communication"
    )
    assert merged.departments == [
        "Journalism and Media Studies Centre",
        "Department of Media and Communication",
    ]
    assert merged.profile_url == "https://example.edu/people/richard-allen"
    assert merged.profile_urls == [
        "https://example.edu/people/academic-staff",
        "https://example.edu/people/richard-allen",
    ]
    assert merged.aliases == ["Alex"]
    assert merged.external_ids == {
        "orcid": "0000-0001-2345-6789",
        "scopus_author_id": "123456789",
    }


def test_fresh_profile_repairs_juha_page_heading_name_and_equal_quality_title():
    polluted = _pi(
        display_name="Current research",
        given_name="Current",
        family_name="research",
        aliases=["Faculty and staff", "Honorary Professors"],
        title="Dean",
        last_checked_at="2025-01-01T00:00:00+00:00",
        last_seen_at="2025-01-01T00:00:00+00:00",
        last_seen_run_id="run-2025",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )
    fresh_profile = _pi(
        person_id="pi_juha_profile",
        display_name="Juha MERILÄ",
        given_name="Juha",
        family_name="MERILÄ",
        aliases=["Current research", "Faculty & Staff"],
        title="Professor and Director of the Research Division",
        last_checked_at="2026-07-14T00:00:00+00:00",
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="run-2026",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )

    merged = _merge_records(polluted, fresh_profile)

    assert merged.display_name == "Juha MERILÄ"
    assert merged.given_name == "Juha"
    assert merged.family_name == "MERILÄ"
    assert merged.title == "Professor and Director of the Research Division"
    assert merged.aliases == []


def test_fresh_profile_wins_equal_quality_title_tie_for_feifei_wang():
    stale = _pi(
        display_name="Feifei Wang",
        title="Professor",
        last_checked_at="2025-01-01T00:00:00+00:00",
        last_seen_at="2025-01-01T00:00:00+00:00",
        last_seen_run_id="run-2025",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )
    fresh_profile = _pi(
        person_id="pi_feifei_fresh_profile",
        display_name="Feifei Wang",
        title="Assistant Professor",
        last_checked_at="2026-07-14T00:00:00+00:00",
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="run-2026",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )

    merged = _merge_records(stale, fresh_profile)

    assert merged.title == "Assistant Professor"
    assert merged.field_sources["title"] == "official_profile"


def test_current_same_profile_repairs_stale_high_scoring_navigation_title():
    profile_url = "https://example.edu/people/brian-tang"
    stale_profile = _pi(
        display_name="Brian Tang",
        title="Professor",
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2025-01-01T00:00:00+00:00",
        last_seen_run_id="run-old",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )
    current_directory = _pi(
        person_id="pi_brian_directory_current",
        display_name="Brian Tang",
        title="Principal Professional Practitioner",
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2026-07-14T00:00:01+00:00",
        last_seen_run_id="run-current",
    )

    touched = _merge_records(stale_profile, current_directory)

    assert touched.title == "Professor"
    assert touched.field_sources["title"] == "official_profile"

    current_profile = _pi(
        person_id="pi_brian_profile_current",
        display_name="Brian Tang",
        title="Principal Professional Practitioner",
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2026-07-14T00:00:02+00:00",
        last_seen_run_id="run-current",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )

    merged = _merge_records(touched, current_profile)

    assert merged.title == "Principal Professional Practitioner"
    assert merged.field_sources["title"] == "official_profile"


def test_current_person_observation_repairs_stale_dean_navigation_title():
    profile_url = "https://example.edu/people/current-researcher"
    stale_profile = _pi(
        title="Dean",
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2025-01-01T00:00:00+00:00",
        last_seen_run_id="run-old",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )
    current_directory = _pi(
        person_id="pi_current_directory",
        title="Post-Doctoral Fellow",
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="run-current",
    )

    repaired = _merge_records(stale_profile, current_directory)

    assert repaired.title == "Post-Doctoral Fellow"
    assert repaired.field_sources["title"] == "official_directory"


def test_navigation_title_repair_requires_the_same_person_profile():
    stale_profile = _pi(
        title="Dean",
        profile_url="https://example.edu/people/jane-doe",
        profile_urls=["https://example.edu/people/jane-doe"],
        last_seen_at="2025-01-01T00:00:00+00:00",
        last_seen_run_id="run-old",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )
    unrelated = _pi(
        person_id="pi_unrelated_directory",
        title="Lecturer",
        profile_url="https://example.edu/people/another-person",
        profile_urls=["https://example.edu/people/another-person"],
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="run-current",
    )

    merged = _merge_records(stale_profile, unrelated)

    assert merged.title == "Dean"
    assert merged.field_sources["title"] == "official_profile"


def test_current_profile_does_not_override_manual_title_verification():
    profile_url = "https://example.edu/people/verified-person"
    verified = _pi(
        title="Verified Current Appointment",
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2025-01-01T00:00:00+00:00",
        last_seen_run_id="run-old",
        field_sources={
            "display_name": "official_profile_manual_verification",
            "title": "official_profile_manual_verification",
            "profile_url": "official_profile_manual_verification",
            "emails": "official_profile_manual_verification",
        },
    )
    automatic = _pi(
        person_id="pi_automatic_profile_current",
        title="Dean",
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="run-current",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )

    merged = _merge_records(verified, automatic)

    assert merged.title == "Verified Current Appointment"
    assert merged.field_sources["title"] == "official_profile_manual_verification"


def test_current_same_profile_replaces_title_contaminated_name_without_aliasing_pollution():
    profile_url = "https://www.law.hku.hk/academic_staff/professor-simon-young/"
    polluted = _pi(
        display_name="Simon Young Professor",
        given_name="Simon Young",
        family_name="Professor",
        aliases=["Simon Young Professor", "Faculty of Law"],
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2025-01-01T00:00:00+00:00",
        last_seen_run_id="run-old",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )
    current_profile = _pi(
        person_id="pi_simon_young_current",
        display_name="Simon Young",
        given_name="Simon",
        family_name="Young",
        aliases=[],
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="run-current",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )

    merged = _merge_records(polluted, current_profile)

    assert merged.display_name == "Simon Young"
    assert merged.given_name == "Simon"
    assert merged.family_name == "Young"
    assert merged.aliases == []
    assert merged.field_sources["display_name"] == "official_profile"


def test_merge_removes_external_identifier_links_and_interface_aliases():
    profile_url = "https://scholars.cityu.edu.hk/en/persons/bmoorhou/"
    existing = _pi(
        display_name="Benjamin Luke MOORHOUSE",
        aliases=["Benjamin Moorhouse", "ORCID iD", "Scopus Author ID"],
        profile_url=profile_url,
        profile_urls=[
            profile_url,
            "https://orcid.org/0000-0002-3913-5194",
            "https://www.scopus.com/authid/detail.uri?authorId=57195513283",
            "https://www.cityu.edu.hk/error/404?item=/bmoorhou",
        ],
        last_seen_at="2025-01-01T00:00:00+00:00",
        last_seen_run_id="run-old",
    )
    current = _pi(
        person_id="pi_benjamin_current",
        display_name="Benjamin Luke MOORHOUSE",
        aliases=["Benjamin Moorhouse"],
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="run-current",
    )

    merged = _merge_records(existing, current)

    assert merged.aliases == ["Benjamin Moorhouse"]
    assert merged.profile_url == profile_url
    assert merged.profile_urls == [profile_url]


def test_current_same_profile_does_not_override_manually_verified_name():
    profile_url = "https://example.edu/people/simon-young"
    verified = _pi(
        display_name="Simon K. W. Young",
        given_name="Simon K. W.",
        family_name="Young",
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2025-01-01T00:00:00+00:00",
        last_seen_run_id="run-old",
        field_sources={
            "display_name": "official_profile_manual_verification",
            "title": "official_profile_manual_verification",
            "profile_url": "official_profile_manual_verification",
            "emails": "official_profile_manual_verification",
        },
    )
    automatic = _pi(
        person_id="pi_simon_young_automatic",
        display_name="Simon Young",
        given_name="Simon",
        family_name="Young",
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="run-current",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )

    merged = _merge_records(verified, automatic)

    assert merged.display_name == "Simon K. W. Young"
    assert merged.given_name == "Simon K. W."
    assert merged.family_name == "Young"
    assert merged.aliases == ["Simon Young"]
    assert (
        merged.field_sources["display_name"]
        == "official_profile_manual_verification"
    )


def test_fresh_clean_name_on_different_profile_does_not_replace_canonical_name():
    polluted = _pi(
        display_name="Simon Young Professor",
        given_name="Simon Young",
        family_name="Professor",
        aliases=[],
        profile_url="https://example.edu/people/simon-young-legacy",
        profile_urls=["https://example.edu/people/simon-young-legacy"],
        last_seen_at="2025-01-01T00:00:00+00:00",
        last_seen_run_id="run-old",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )
    different_profile = _pi(
        person_id="pi_simon_young_other_profile",
        display_name="Simon Young",
        given_name="Simon",
        family_name="Young",
        aliases=[],
        profile_url="https://example.edu/people/simon-young-current",
        profile_urls=["https://example.edu/people/simon-young-current"],
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="run-current",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )

    merged = _merge_records(polluted, different_profile)

    assert merged.display_name == "Simon Young Professor"
    assert merged.given_name == "Simon Young"
    assert merged.family_name == "Professor"
    assert merged.aliases == ["Simon Young"]


def test_fresh_low_quality_fields_do_not_replace_good_canonical_values_or_become_aliases():
    canonical = _pi(
        display_name="Margaret Chan",
        given_name="Margaret",
        family_name="Chan",
        title="Professor",
        last_checked_at="2025-01-01T00:00:00+00:00",
        last_seen_at="2025-01-01T00:00:00+00:00",
        last_seen_run_id="run-2025",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )
    polluted_profile = _pi(
        person_id="pi_polluted_fresh_profile",
        display_name="Honorary Professors",
        given_name="Honorary",
        family_name="Professors",
        aliases=["Faculty and staff", "Current research"],
        title=(
            "Biography: she is currently working at the university. She joined "
            "after she received her degree and worked in several organisations."
        ),
        last_checked_at="2026-07-14T00:00:00+00:00",
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="run-2026",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )

    merged = _merge_records(canonical, polluted_profile)

    assert merged.display_name == "Margaret Chan"
    assert merged.given_name == "Margaret"
    assert merged.family_name == "Chan"
    assert merged.title == "Professor"
    assert merged.aliases == []


def test_clean_current_observation_promotes_over_non_person_canonical_and_cleans_aliases():
    profile_url = "https://example.edu/people/chia-hung-chen"
    polluted = _pi(
        display_name="SELECTED PUBLICATIONS",
        given_name="SELECTED",
        family_name="PUBLICATIONS",
        aliases=[
            "Chia-Hung CHEN",
            "Personal profile",
            "Political development",
            "Dept. of EE, City Univ. of Hong Kong Chia-Hung CHEN",
        ],
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2025-01-01T00:00:00+00:00",
        last_seen_run_id="run-old",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )
    current_directory = _pi(
        person_id="pi_chia_hung_current",
        display_name="CHEN, Chia-Hung",
        given_name="Chia-Hung",
        family_name="CHEN",
        aliases=[],
        profile_url=profile_url,
        profile_urls=[profile_url],
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="run-current",
        field_sources={
            "display_name": "official_directory",
            "title": "official_directory",
            "profile_url": "official_directory",
            "emails": "official_directory",
        },
    )

    merged = _merge_records(polluted, current_directory)

    assert merged.display_name == "CHEN, Chia-Hung"
    assert merged.given_name == "Chia-Hung"
    assert merged.family_name == "CHEN"
    assert merged.aliases == ["Chia-Hung CHEN"]


def test_higher_authority_profile_ambiguity_clears_directory_email_for_feifei_wang():
    directory = _pi(
        display_name="Feifei Wang",
        given_name="Feifei",
        family_name="Wang",
        emails=["wnlee@eee.hku.hk"],
        email_association="person_local",
        field_sources={
            "display_name": "official_directory",
            "title": "official_directory",
            "profile_url": "official_directory",
            "emails": "official_directory",
        },
    )
    profile = _pi(
        person_id="pi_feifei_profile",
        display_name="Feifei Wang",
        given_name="Feifei",
        family_name="Wang",
        emails=[],
        email_association="ambiguous_email",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )

    merged = _merge_records(directory, profile)

    assert merged.emails == []
    assert merged.email_association == "ambiguous_email"
    assert merged.field_sources["emails"] == "official_profile"


def test_higher_authority_profile_without_email_clears_directory_email():
    directory = _pi(emails=["stale@example.edu"])
    profile = _pi(
        person_id="pi_profile_without_email",
        emails=[],
        email_association="none",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )

    merged = _merge_records(directory, profile)

    assert merged.emails == []
    assert merged.email_association == "none"
    assert merged.field_sources["emails"] == "official_profile"


def test_current_same_profile_ambiguity_clears_old_pollution_after_directory_touch():
    profile_url = "https://sbme.hku.hk/people/wangfeifei"
    old_polluted_profile = _pi(
        display_name="Feifei Wang",
        given_name="Feifei",
        family_name="Wang",
        profile_url=profile_url,
        profile_urls=[profile_url],
        emails=["wnlee@eee.hku.hk"],
        email_association="person_local",
        last_seen_run_id="run-old",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )
    current_directory = _pi(
        person_id="pi_feifei_directory_current",
        display_name="Feifei Wang",
        given_name="Feifei",
        family_name="Wang",
        profile_url=profile_url,
        profile_urls=[profile_url],
        emails=["wnlee@eee.hku.hk"],
        email_association="person_local",
        last_seen_run_id="run-current",
        field_sources={
            "display_name": "official_directory",
            "title": "official_directory",
            "profile_url": "official_directory",
            "emails": "official_directory",
        },
    )

    touched = _merge_records(old_polluted_profile, current_directory)

    assert touched.last_seen_run_id == "run-current"
    assert touched.emails == ["wnlee@eee.hku.hk"]
    assert touched.field_sources["emails"] == "official_profile"

    current_profile = _pi(
        person_id="pi_feifei_profile_current",
        display_name="Feifei Wang",
        given_name="Feifei",
        family_name="Wang",
        profile_url=profile_url,
        profile_urls=[profile_url],
        emails=[],
        email_association="ambiguous_email",
        last_seen_run_id="run-current",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )

    merged = _merge_records(touched, current_profile)

    assert merged.emails == []
    assert merged.email_association == "ambiguous_email"
    assert merged.field_sources["emails"] == "official_profile"


def test_equal_authority_person_local_observations_union_within_one_run():
    first_profile = _pi(
        emails=["jane.doe@example.edu"],
        email_association="person_local",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )
    second_profile = _pi(
        person_id="pi_second_profile_observation",
        emails=["jane@example.edu"],
        email_association="person_local",
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
    )

    merged = _merge_records(first_profile, second_profile)

    assert merged.emails == ["jane.doe@example.edu", "jane@example.edu"]
    assert merged.email_association == "person_local"
    assert merged.field_sources["emails"] == "official_profile"


def test_consolidation_reparents_identity_evidence_and_keeps_first_seen_id(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    canonical = _pi(first_seen_at="2025-01-01T00:00:00+00:00")
    duplicate = _pi(
        person_id="pi_fedcba9876543210",
        display_name="J. Doe",
        profile_url="https://example.edu/profiles/j-doe",
        profile_urls=["https://example.edu/profiles/j-doe"],
        first_seen_at="2026-01-01T00:00:00+00:00",
        external_ids={"orcid": "0000-0001-2345-6789"},
    )
    storage.upsert_pi_record(canonical)
    storage.upsert_pi_record(duplicate)
    storage.insert_pi_observation(duplicate, "run-1", duplicate.profile_url, {"title": "Professor"})
    storage.insert_email_evidence(
        EmailEvidence(
            email="jane@example.edu",
            source_url=duplicate.profile_url,
            source_type="official_profile",
            domain_aligned=True,
            official_source=True,
            extracted_at="2026-01-01T00:00:00+00:00",
            confidence=0.9,
            verdict="official_domain_aligned",
            person_id=duplicate.person_id,
            run_id="run-1",
        )
    )
    storage.upsert_publication_fingerprint(
        OfficialPublicationFingerprint(
            fingerprint_id="pubfp_duplicate",
            person_id=duplicate.person_id,
            institution_id=duplicate.institution_id,
            title="A useful paper",
            citation_text="A useful paper (2025)",
            source_url=duplicate.profile_url,
            run_id="run-1",
            first_seen_at="2026-01-01T00:00:00+00:00",
            last_seen_at="2026-01-01T00:00:00+00:00",
            last_seen_run_id="run-1",
            publication_year=2025,
        )
    )

    assert storage.preferred_canonical_person_id(canonical.person_id, duplicate.person_id) == canonical.person_id
    storage.consolidate_person_ids(
        duplicate.person_id,
        canonical.person_id,
        canonical.institution_id,
        "same_orcid",
        "run-2",
    )

    assert storage.get_pi_record(duplicate.person_id) is None
    assert storage.resolve_person_id(duplicate.person_id) == canonical.person_id
    assert storage.conn.execute(
        "SELECT person_id FROM pi_observations"
    ).fetchone()["person_id"] == canonical.person_id
    assert storage.conn.execute(
        "SELECT person_id FROM email_evidence"
    ).fetchone()["person_id"] == canonical.person_id
    assert storage.conn.execute(
        "SELECT person_id FROM official_publication_fingerprints"
    ).fetchone()["person_id"] == canonical.person_id
    consolidated = storage.get_pi_record(canonical.person_id)
    assert consolidated is not None
    assert consolidated.external_ids["orcid"] == "0000-0001-2345-6789"
    assert storage.conn.execute(
        """
        SELECT COUNT(*) FROM pi_identity_keys
        WHERE person_id=? AND identity_kind='external:orcid'
          AND identity_value='0000-0001-2345-6789'
        """,
        (canonical.person_id,),
    ).fetchone()[0] == 1
    later_alias = _pi(
        person_id="pi_later_orcid_alias",
        display_name="Jane D.",
        profile_url="https://example.edu/profiles/opaque-jane",
        profile_urls=["https://example.edu/profiles/opaque-jane"],
        emails=[],
        external_ids={"orcid_url": "https://orcid.org/0000-0001-2345-6789"},
    )
    assert storage.find_existing_duplicate(later_alias) == (
        canonical.person_id,
        "same_orcid",
    )
    storage.close()


def test_reviewed_consolidation_promotes_clean_name_and_drops_bad_aliases_and_urls(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    polluted_keeper = _pi(
        display_name="Link to profile",
        given_name="Link to",
        family_name="profile",
        aliases=["Personal profile", "WORK EXPERIENCE"],
        profile_url="https://scholars.example.edu/",
        profile_urls=[
            "https://scholars.example.edu/",
            "mailt:clarke@example.edu",
        ],
        field_sources={
            "display_name": "official_profile",
            "title": "official_profile",
            "profile_url": "official_profile",
            "emails": "official_profile",
        },
        first_seen_at="2025-01-01T00:00:00+00:00",
    )
    clean_duplicate = _pi(
        person_id="pi_clean_clarke",
        display_name="CLARKE David R",
        given_name="David R",
        family_name="CLARKE",
        aliases=[],
        profile_url="https://clarke.seas.harvard.edu/",
        profile_urls=["https://clarke.seas.harvard.edu/"],
        first_seen_at="2026-01-01T00:00:00+00:00",
    )
    storage.upsert_pi_record(polluted_keeper)
    storage.upsert_pi_record(clean_duplicate)

    storage.consolidate_person_ids(
        clean_duplicate.person_id,
        polluted_keeper.person_id,
        polluted_keeper.institution_id,
        "reviewed_same_person",
        "run-reviewed",
    )

    consolidated = storage.get_pi_record(polluted_keeper.person_id)
    assert consolidated is not None
    assert consolidated.display_name == "CLARKE David R"
    assert consolidated.given_name == "David R"
    assert consolidated.family_name == "CLARKE"
    assert consolidated.field_sources["display_name"] == "official_directory"
    assert consolidated.aliases == []
    assert consolidated.profile_url == "https://clarke.seas.harvard.edu/"
    assert consolidated.profile_urls == ["https://clarke.seas.harvard.edu/"]
    storage.close()
