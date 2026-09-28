from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from pi_index.cli import main
from pi_index.pipeline.audit_pi_quality import (
    build_pi_quality_audit,
    incomplete_name_reasons,
    run_pi_quality_audit,
)


def _record(
    person_id: str,
    name: str,
    school_id: str,
    school: str,
    *,
    title: str | None = "Professor",
    emails: list[str] | None = None,
    profile: str | None = "https://example.edu/person",
    research: list[str] | None = None,
    status: str = "active",
) -> tuple:
    emails = emails or []
    research = research or []
    payload = {
        "person_id": person_id,
        "display_name": name,
        "institution_id": school_id,
        "institution_name": school,
        "title": title,
        "emails": emails,
        "profile_url": profile,
        "research_areas": research,
        "external_ids": {"orcid": f"0000-{person_id}"},
        "source_evidence_ids": [f"ev-{person_id}"],
        "membership_status": status,
    }
    return (
        person_id,
        name,
        school_id,
        school,
        title,
        profile,
        json.dumps(emails),
        json.dumps(research),
        status,
        json.dumps(payload),
    )


def _quality_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE canonical_pi_records (
            person_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            institution_id TEXT NOT NULL,
            institution_name TEXT NOT NULL,
            title TEXT,
            profile_url TEXT,
            emails_json TEXT NOT NULL,
            research_areas_json TEXT NOT NULL,
            membership_status TEXT,
            record_json TEXT NOT NULL
        );
        CREATE TABLE official_publication_fingerprints (
            person_id TEXT NOT NULL,
            title TEXT,
            citation_text TEXT,
            publication_year INTEGER,
            doi TEXT,
            publication_url TEXT
        );
        CREATE TABLE pi_identity_aliases (
            alias_person_id TEXT PRIMARY KEY,
            canonical_person_id TEXT NOT NULL
        );
        CREATE TABLE email_evidence (
            email TEXT NOT NULL,
            person_id TEXT
        );
        """
    )
    biography_title = (
        "Catherine Chan is a distinguished scholar who joined the university in 2012. "
        "She received her doctorate overseas and currently leads several major research projects."
    )
    rows = [
        _record(
            "p1",
            "Jane Doe",
            "a",
            "University A",
            emails=["shared@a.edu", "alias@a.edu"],
            research=[],
        ),
        _record(
            "p2",
            "Jane Doe",
            "a",
            "University A",
            title=biography_title,
            emails=["shared@a.edu"],
            profile=None,
            research=["machine learning"],
        ),
        _record(
            "p3",
            "Alex",
            "a",
            "University A",
            title=None,
            emails=[],
            profile=None,
            research=[],
        ),
        _record(
            "p4",
            "Inactive Person",
            "a",
            "University A",
            emails=[],
            profile=None,
            research=[],
            status="inactive",
        ),
        _record(
            "p5",
            "Bo Li",
            "b",
            "University B",
            emails=["bo@b.edu"],
            research=[],
        ),
    ]
    conn.executemany(
        """
        INSERT INTO canonical_pi_records
          (person_id, display_name, institution_id, institution_name, title,
           profile_url, emails_json, research_areas_json, membership_status, record_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.executemany(
        """
        INSERT INTO official_publication_fingerprints
          (person_id, title, citation_text, publication_year, doi, publication_url)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "p1",
                "An Interesting Study of Neural Systems",
                "Jane Doe (2025). An Interesting Study of Neural Systems.",
                2025,
                "10.1000/jane",
                "https://a.edu/publications/jane",
            ),
            ("p3", "Publications", "Publications", None, None, None),
            (
                "p5",
                "Advanced Materials for Sustainable Energy Storage",
                "Bo Li (2024). Advanced Materials for Sustainable Energy Storage.",
                2024,
                None,
                "https://b.edu/publications/bo",
            ),
        ],
    )
    conn.execute(
        "INSERT INTO pi_identity_aliases VALUES (?, ?)",
        ("p1-old", "p1"),
    )
    conn.executemany(
        "INSERT INTO email_evidence VALUES (?, ?)",
        [("alias@a.edu", "p1"), ("alias@a.edu", "p1-old")],
    )
    conn.commit()
    return conn


def test_quality_audit_counts_evidence_and_identity_groups(tmp_path):
    db = tmp_path / "quality.db"
    conn = _quality_db(db)
    report = build_pi_quality_audit(conn, database=db, samples=3)
    conn.close()

    assert report["summary"] == {
        "schools": 2,
        "active": 4,
        "no_email": 1,
        "no_title": 1,
        "no_profile": 2,
        "no_research_areas": 3,
        "meaningful_publication_people": 2,
        "no_research_evidence": 1,
        "suspicious_title": 1,
        "incomplete_name": 1,
        "exact_same_name_groups": 1,
        "shared_email_groups": 2,
        "shared_email_same_person_alias_groups": 1,
        "shared_email_cross_canonical_groups": 1,
    }
    school_a = next(
        school for school in report["schools"] if school["institution_id"] == "a"
    )
    assert school_a["metrics"]["active"] == 3
    assert school_a["metrics"]["no_research_evidence"] == 1
    assert school_a["metrics"]["exact_same_name_groups"] == 1
    assert school_a["metrics"]["shared_email_groups"] == 2
    assert school_a["samples"]["incomplete_name"][0]["name"] == "Alex"
    assert school_a["samples"]["incomplete_name"][0]["reasons"] == ["single_token"]
    assert school_a["samples"]["no_email"][0]["source_ids"] == {
        "canonical_person_id": "p3",
        "external_ids": {"orcid": "0000-p3"},
        "evidence_ids": ["ev-p3"],
        "evidence_id_count": 1,
    }

    classifications = {
        group["email"]: group["classification"]
        for group in report["shared_email_groups"]["samples"]
    }
    assert classifications == {
        "alias@a.edu": "same_person_alias",
        "shared@a.edu": "cross_canonical",
    }


def test_incomplete_name_audit_does_not_treat_two_character_cjk_names_as_initials():
    assert incomplete_name_reasons("張 誠") == []
    assert incomplete_name_reasons("林 响") == []
    assert incomplete_name_reasons("王 寧") == []
    assert incomplete_name_reasons("A B") == ["initials_only"]


def test_quality_audit_writes_json_markdown_and_cli(tmp_path, capsys):
    db = tmp_path / "quality.db"
    _quality_db(db).close()

    report, paths = run_pi_quality_audit(db, tmp_path / "direct", samples=2)
    assert report["sample_limit"] == 2
    assert Path(paths["json"]).is_file()
    assert Path(paths["markdown"]).is_file()
    markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
    assert "# PI data quality audit" in markdown
    assert "same-person alias `1`; cross-canonical `1`" in markdown
    assert "source IDs=p3" in markdown

    result = main(
        [
            "audit-pi-quality",
            "--db",
            str(db),
            "--out",
            str(tmp_path / "cli"),
            "--samples",
            "3",
        ]
    )
    assert result == 0
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["summary"]["active"] == 4
    assert Path(stdout["json"]).is_file()
    assert Path(stdout["markdown"]).is_file()
