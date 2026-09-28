import csv

from pi_index.models import (
    CanonicalPIRecord,
    OfficialPublicationFingerprint,
    PIContactVerdict,
)
from pi_index.storage import PIIndexStorage


def _pi(person_id: str, *, research_areas: list[str] | None = None) -> CanonicalPIRecord:
    return CanonicalPIRecord(
        person_id=person_id,
        display_name=f"Person {person_id}",
        given_name=None,
        family_name=None,
        aliases=[],
        institution_id="inst_example",
        institution_name="Example University",
        ror_id=None,
        department="Computing",
        title="Lecturer",
        profile_url=f"https://example.edu/people/{person_id}",
        lab_url=None,
        emails=[],
        research_areas=research_areas or [],
        publications_summary={"official_fingerprint_count": 99},
        external_ids={},
        source_evidence_ids=[],
        last_checked_at="2026-07-14T00:00:00+00:00",
    )


def _fingerprint(person_id: str, fingerprint_id: str, title: str) -> OfficialPublicationFingerprint:
    return OfficialPublicationFingerprint(
        fingerprint_id=fingerprint_id,
        person_id=person_id,
        institution_id="inst_example",
        title=title,
        citation_text=f"{title}. Journal of Examples, 2025.",
        source_url=f"https://example.edu/people/{person_id}",
        run_id="run-1",
        first_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_at="2026-07-14T00:00:00+00:00",
        last_seen_run_id="run-1",
        publication_year=2025,
    )


def test_publication_reads_ignore_legacy_pollution_without_implicitly_deleting_it(tmp_path):
    db_path = tmp_path / "pool.db"
    storage = PIIndexStorage(db_path)
    storage.upsert_pi_record(_pi("polluted"))
    storage.upsert_publication_fingerprint(
        _fingerprint("polluted", "fp_polluted", "Research output per year")
    )

    assert storage.publication_summary("polluted") == {
        "official_fingerprint_count": 0,
        "official_fingerprint_latest_year": None,
        "official_fingerprint_doi_count": 0,
    }
    assert storage.publication_text_by_person(["polluted"]) == {}
    assert storage.audit_counts()["research_evidence_ready"] == 0
    storage.close()

    reopened = PIIndexStorage(db_path)
    assert reopened.conn.execute(
        "SELECT COUNT(*) FROM official_publication_fingerprints"
    ).fetchone()[0] == 1
    reopened.close()


def test_explicit_purge_removes_invalid_rows_and_refreshes_canonical_summary(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    storage.upsert_pi_record(_pi("polluted"))
    storage.upsert_publication_fingerprint(
        _fingerprint("polluted", "fp_polluted", "Research output per year")
    )

    result = storage.purge_invalid_publication_fingerprints("inst_example")

    assert result == {"scanned": 1, "deleted": 1, "people_refreshed": 1}
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM official_publication_fingerprints"
    ).fetchone()[0] == 0
    assert storage.get_pi_record("polluted").publications_summary == {
        "official_fingerprint_count": 0,
        "official_fingerprint_latest_year": None,
        "official_fingerprint_doi_count": 0,
    }


def test_publication_text_returns_title_and_citation_only_for_meaningful_works(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    storage.upsert_pi_record(_pi("researcher"))
    storage.upsert_publication_fingerprint(
        _fingerprint(
            "researcher",
            "fp_meaningful",
            "Causal Representation Learning for Single-Cell Biology",
        )
    )

    text = storage.publication_text_by_person(["researcher"])

    assert len(text["researcher"]) == 1
    assert "Causal Representation Learning for Single-Cell Biology" in text["researcher"][0]
    assert "Journal of Examples" in text["researcher"][0]
    assert storage.publication_summary("researcher")["official_fingerprint_count"] == 1


def test_research_evidence_exports_do_not_promote_polluted_fingerprints(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    storage.upsert_pi_record(_pi("polluted"))
    storage.upsert_publication_fingerprint(
        _fingerprint("polluted", "fp_polluted", "Research output per year")
    )
    storage.upsert_contact_verdict(
        PIContactVerdict(
            person_id="polluted",
            verdict="no_official_email",
            reasons=["No person-local email."],
            recommended_action="Review.",
            last_live_checked_at="2026-07-14T00:00:00+00:00",
            contact_confidence="none",
            topic_match_confidence="low",
            current_affiliation_confidence="unknown",
        )
    )

    output = tmp_path / "exports"
    storage.export(output)
    with (output / "research_evidence_ready.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        ready = list(csv.DictReader(handle))
    with (output / "research_evidence_review_queue.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        review = list(csv.DictReader(handle))

    assert ready == []
    assert [row["person_id"] for row in review] == ["polluted"]


def test_audit_sample_stratifies_by_research_evidence_not_appointment_title(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    for index in range(30):
        researcher = _pi(f"researcher_{index}", research_areas=["causal learning"])
        researcher.title = "Executive Officer"
        storage.upsert_pi_record(researcher)
    for index in range(30):
        no_evidence = _pi(f"no_evidence_{index}")
        no_evidence.title = "Professor"
        storage.upsert_pi_record(no_evidence)

    output = tmp_path / "audit_sample.csv"
    assert storage.write_audit_sample(output, sample_size=30) == 30
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert [row["research_evidence_status"] for row in rows[:20]] == ["present"] * 20
    assert [row["research_evidence_status"] for row in rows[20:]] == ["missing"] * 10
    assert {row["title"] for row in rows[:20]} == {"Executive Officer"}
    assert {row["title"] for row in rows[20:]} == {"Professor"}
