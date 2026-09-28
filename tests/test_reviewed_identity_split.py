from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_index.models import (
    CanonicalPIRecord,
    EmailEvidence,
    OfficialPublicationFingerprint,
    PersonEvidence,
    stable_id,
)
from pi_index.pipeline.reviewed_identity_split import apply_reviewed_identity_split
from pi_index.storage import PIIndexStorage


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "configs" / "identity_splits" / "hku_yang_liu_business_v1.json"
OLD_ID = "pi_6c68c946bdb06164"
NEW_ID = "pi_ab915b2c31380c0e"
INSTITUTION_ID = "inst_77b83f05042f0881"
SBMS_DIRECTORY = "https://www.sbms.hku.hk/faculty"
SBMS_PROFILE = "https://www.sbms.hku.hk/staff/yang-liu"
BUSINESS_DIRECTORY = "https://www.hkubs.hku.hk/people/?pg=12&"
BUSINESS_PROFILE = "https://www.hkubs.hku.hk/people/yang-liu/"
NOW = "2026-07-14T12:23:29+00:00"
RUN_ID = "test-run"


def _merged_record() -> CanonicalPIRecord:
    return CanonicalPIRecord(
        person_id=OLD_ID,
        display_name="Yang LIU",
        given_name="Yang",
        family_name="LIU",
        aliases=["LIU Yang"],
        institution_id=INSTITUTION_ID,
        institution_name="The University of Hong Kong",
        ror_id="https://ror.org/02zhqgq86",
        department="School of Biomedical Sciences; HKU Business School",
        departments=["School of Biomedical Sciences", "HKU Business School"],
        title="Associate Professor; Assistant Professor",
        profile_url=BUSINESS_PROFILE,
        profile_urls=[SBMS_PROFILE, BUSINESS_PROFILE],
        lab_url="https://www.hkubs.hku.hk/corporate",
        emails=["yangliu5@hku.hk", "yangliu9@hku.hk"],
        research_areas=["asset pricing", "chromatin"],
        publications_summary={"official_fingerprint_count": 1},
        external_ids={"orcid": "0000-0001-7187-9196"},
        source_evidence_ids=[],
        last_checked_at=NOW,
        first_seen_at="2026-07-14T05:37:29+00:00",
        last_seen_at=NOW,
        last_seen_run_id=RUN_ID,
        membership_status="active",
        pool_scope="Whole institution",
        field_sources={"display_name": "official_profile", "profile_url": "official_profile"},
        email_association="person_local",
    )


def _seed_evidence(storage: PIIndexStorage, source_url: str, values: dict[str, object]) -> None:
    for field_name, raw_value in values.items():
        candidates = raw_value if isinstance(raw_value, list) else [raw_value]
        for value in candidates:
            evidence_id = stable_id("ev", source_url, field_name, value)
            storage.insert_person_evidence(
                PersonEvidence(
                    evidence_id=evidence_id,
                    person_temp_id=stable_id("tmp_person", source_url),
                    institution_id=INSTITUTION_ID,
                    field_name=field_name,
                    field_value=str(value),
                    source_url=source_url,
                    source_type="official_profile",
                    extraction_method="test",
                    extracted_at=NOW,
                    confidence=0.95,
                    evidence_text=str(value),
                    content_hash="hash",
                    run_id=RUN_ID,
                )
            )


def _seed_database(path: Path) -> None:
    storage = PIIndexStorage(path)
    merged = _merged_record()
    storage.upsert_pi_record(merged)
    storage.upsert_person_id_alias(
        NEW_ID,
        OLD_ID,
        INSTITUTION_ID,
        "same_normalized_name_and_profile_slug",
        RUN_ID,
    )
    storage.upsert_person_id_alias(
        "pi_dd36721f2e64a044",
        OLD_ID,
        INSTITUTION_ID,
        "same_profile_url_with_name_alias",
        RUN_ID,
    )

    _seed_evidence(
        storage,
        SBMS_PROFILE,
        {
            "display_name": "Yang Liu",
            "department": "School of Biomedical Sciences",
            "profile_url": SBMS_PROFILE,
            "emails": "yangliu9@hku.hk",
        },
    )
    _seed_evidence(
        storage,
        BUSINESS_PROFILE,
        {
            "display_name": "Yang LIU",
            "department": "HKU Business School",
            "profile_url": BUSINESS_PROFILE,
            "emails": "yangliu5@hku.hk",
        },
    )
    for source_url, observation in (
        (SBMS_DIRECTORY, {"name": "Yang Liu", "department": "School of Biomedical Sciences"}),
        (
            SBMS_PROFILE,
            {
                "name": "Yang Liu",
                "department": "School of Biomedical Sciences",
                "profile_url": SBMS_PROFILE,
                "emails": ["yangliu9@hku.hk"],
            },
        ),
        (BUSINESS_DIRECTORY, {"name": "Yang LIU", "department": "HKU Business School"}),
        (
            BUSINESS_PROFILE,
            {
                "name": "Yang LIU",
                "department": "HKU Business School",
                "profile_url": BUSINESS_PROFILE,
                "emails": ["yangliu5@hku.hk"],
            },
        ),
    ):
        storage.insert_pi_observation(merged, RUN_ID, source_url, observation)

    old_fingerprint_id = stable_id("pubfp", OLD_ID, "finance paper|2025")
    storage.upsert_publication_fingerprint(
        OfficialPublicationFingerprint(
            fingerprint_id=old_fingerprint_id,
            person_id=OLD_ID,
            institution_id=INSTITUTION_ID,
            title="Finance Paper",
            citation_text="Finance Paper",
            publication_year=2025,
            doi=None,
            publication_url=None,
            source_url=BUSINESS_PROFILE,
            confidence=0.95,
            run_id=RUN_ID,
            first_seen_at=NOW,
            last_seen_at=NOW,
            last_seen_run_id=RUN_ID,
        )
    )
    fingerprint_payload = {
        "fingerprint_id": old_fingerprint_id,
        "person_id": OLD_ID,
        "institution_id": INSTITUTION_ID,
        "source_url": BUSINESS_PROFILE,
    }
    storage.conn.execute(
        """
        INSERT INTO official_publication_source_claims
        (fingerprint_id, person_id, institution_id, source_url, source_kind,
         claim_status, missing_streak, first_seen_at, last_seen_at,
         last_seen_run_id, last_checked_at, tombstoned_at, record_json)
        VALUES (?, ?, ?, ?, 'official_profile', 'active', 0, ?, ?, ?, ?, NULL, ?)
        """,
        (
            old_fingerprint_id,
            OLD_ID,
            INSTITUTION_ID,
            BUSINESS_PROFILE,
            NOW,
            NOW,
            RUN_ID,
            NOW,
            json.dumps(fingerprint_payload),
        ),
    )
    storage.conn.execute(
        """
        INSERT INTO official_publication_refresh_state
        (person_id, institution_id, source_url, source_kind, checked_at,
         parse_status, parse_complete, publication_count, record_json)
        VALUES (?, ?, ?, 'official_profile', ?, 'success', 1, 1, ?)
        """,
        (
            OLD_ID,
            INSTITUTION_ID,
            BUSINESS_PROFILE,
            NOW,
            json.dumps({"person_id": OLD_ID, "source_url": BUSINESS_PROFILE}),
        ),
    )
    for source_url in (SBMS_DIRECTORY, SBMS_PROFILE, BUSINESS_DIRECTORY, BUSINESS_PROFILE):
        for email in ("yangliu5@hku.hk", "yangliu9@hku.hk"):
            storage.insert_email_evidence(
                EmailEvidence(
                    email=email,
                    source_url=source_url,
                    person_id=OLD_ID,
                    source_type="official_profile" if "yang-liu" in source_url else "official_directory",
                    domain_aligned=True,
                    official_source=True,
                    extracted_at=NOW,
                    confidence=0.95,
                    verdict="official_domain_aligned",
                    association="person_local" if "yang-liu" in source_url else "none",
                    run_id=RUN_ID,
                )
            )
    storage.conn.execute(
        """
        INSERT INTO openalex_author_links
        (person_id, institution_id, openalex_author_id, link_status, confidence,
         match_method, evidence_json, first_linked_at, last_verified_at, record_json)
        VALUES (?, ?, 'A5033745753', 'confirmed', 1.0, 'canonical_orcid_exact', '{}', ?, ?, '{}')
        """,
        (OLD_ID, INSTITUTION_ID, NOW, NOW),
    )
    storage.conn.execute(
        """
        INSERT INTO openalex_person_works
        (person_id, openalex_work_id, institution_id, openalex_author_id,
         relationship_status, missing_streak, first_seen_at, last_seen_at,
         last_seen_run_id, last_checked_at, record_json)
        VALUES (?, 'W1', ?, 'A5033745753', 'active', 0, ?, ?, ?, ?, '{}')
        """,
        (OLD_ID, INSTITUTION_ID, NOW, NOW, RUN_ID, NOW),
    )
    storage.conn.commit()
    storage.close()


def test_reviewed_split_dry_run_is_read_only_then_apply_is_idempotent(tmp_path):
    database = tmp_path / "pool.db"
    report = tmp_path / "split-report.json"
    _seed_database(database)

    before = database.read_bytes()
    dry_run = apply_reviewed_identity_split(
        database, MANIFEST, report_path=report
    )
    assert dry_run["status"] == "planned"
    assert dry_run["plan"]["cohort_before"] == 1
    assert dry_run["plan"]["cohort_after"] == 1
    assert database.read_bytes() == before

    applied = apply_reviewed_identity_split(
        database, MANIFEST, report_path=report, apply=True, run_id="reviewed-test"
    )
    assert applied["status"] == "applied"
    assert applied["result"]["polluted_email_rows_removed"] == 4

    storage = PIIndexStorage(database)
    biomedical = storage.get_pi_record(OLD_ID)
    business = storage.get_pi_record(NEW_ID)
    assert biomedical is not None and business is not None
    assert biomedical.department == "School of Biomedical Sciences"
    assert biomedical.emails == ["yangliu9@hku.hk"]
    assert biomedical.external_ids["orcid"] == "0000-0001-7187-9196"
    assert business.department == "HKU Business School"
    assert business.emails == ["yangliu5@hku.hk"]
    assert business.external_ids == {}
    assert storage.resolve_person_id(NEW_ID) == NEW_ID
    assert storage.find_existing_duplicate(business) is None
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM pi_observations WHERE person_id=?", (NEW_ID,)
    ).fetchone()[0] == 2
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM official_publication_fingerprints WHERE person_id=?",
        (NEW_ID,),
    ).fetchone()[0] == 1
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM official_publication_source_claims WHERE person_id=?",
        (NEW_ID,),
    ).fetchone()[0] == 1
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM official_publication_refresh_state WHERE person_id=?",
        (NEW_ID,),
    ).fetchone()[0] == 1
    assert storage.conn.execute(
        "SELECT openalex_author_id FROM openalex_author_links WHERE person_id=?",
        (OLD_ID,),
    ).fetchone()[0] == "A5033745753"
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM openalex_person_works WHERE person_id=?", (NEW_ID,)
    ).fetchone()[0] == 0
    assert storage.conn.execute(
        """
        SELECT COUNT(*) FROM vector_dirty_queue
        WHERE entity_kind='openalex_works_sync' AND entity_id=? AND status='pending'
        """,
        (NEW_ID,),
    ).fetchone()[0] == 1
    assert storage.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    storage.close()

    second = apply_reviewed_identity_split(database, MANIFEST, apply=True)
    assert second["status"] == "already_applied"


def test_reviewed_split_rolls_back_every_row_on_transaction_failure(tmp_path):
    database = tmp_path / "pool.db"
    _seed_database(database)
    storage = PIIndexStorage(database)
    storage.conn.execute(
        """
        CREATE TRIGGER abort_reviewed_split_refresh
        BEFORE DELETE ON official_publication_refresh_state
        BEGIN
          SELECT RAISE(ABORT, 'forced split failure');
        END
        """
    )
    storage.conn.commit()
    storage.close()
    before = database.read_bytes()

    with pytest.raises(Exception, match="forced split failure"):
        apply_reviewed_identity_split(database, MANIFEST, apply=True)

    # SQLite may rewrite transaction bookkeeping, so verify logical rollback
    # rather than requiring byte-for-byte WAL/page identity after a failed write.
    storage = PIIndexStorage(database)
    assert storage.get_pi_record(NEW_ID) is None
    assert storage.resolve_person_id(NEW_ID) == OLD_ID
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM pi_observations WHERE person_id=?", (OLD_ID,)
    ).fetchone()[0] == 4
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM official_publication_fingerprints WHERE person_id=?",
        (OLD_ID,),
    ).fetchone()[0] == 1
    assert not storage.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviewed_identity_splits'"
    ).fetchone()
    assert storage.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    storage.close()
    assert before
