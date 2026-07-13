from __future__ import annotations

import csv
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from .models import (
    CanonicalPIRecord,
    EmailEvidence,
    InstitutionRecord,
    PIContactVerdict,
    PersonEvidence,
    RawSourceRecord,
    utc_now_iso,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _normalize_key_part(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def dedupe_key_for_record(record: CanonicalPIRecord) -> str:
    emails = ",".join(sorted(e.lower() for e in record.emails))
    external = json.dumps(record.external_ids or {}, sort_keys=True)
    return "|".join(
        [
            _normalize_key_part(record.institution_id),
            _normalize_key_part(record.display_name),
            _normalize_key_part(record.profile_url),
            _normalize_key_part(emails),
            _normalize_key_part(external),
        ]
    )


class PIIndexStorage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.init_db()

    def close(self) -> None:
        self.conn.close()

    def init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS institutions (
                institution_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                aliases_json TEXT NOT NULL,
                country TEXT,
                region TEXT,
                ror_id TEXT,
                homepage_url TEXT,
                official_domains_json TEXT NOT NULL,
                qs_rank INTEGER,
                qs_year INTEGER,
                source TEXT,
                status TEXT,
                record_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS raw_sources (
                source_url TEXT NOT NULL,
                institution_id TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                source_type TEXT,
                http_status INTEGER,
                content_hash TEXT,
                parser_used TEXT,
                crawl_method TEXT,
                error_reason TEXT,
                record_json TEXT NOT NULL,
                PRIMARY KEY (source_url, institution_id, fetched_at)
            );

            CREATE TABLE IF NOT EXISTS person_evidence (
                evidence_id TEXT PRIMARY KEY,
                person_temp_id TEXT,
                institution_id TEXT,
                field_name TEXT,
                field_value TEXT,
                source_url TEXT,
                source_type TEXT,
                extraction_method TEXT,
                extracted_at TEXT,
                confidence REAL,
                evidence_text TEXT,
                content_hash TEXT,
                record_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS canonical_pi_records (
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
                pi_supervisor_confidence TEXT,
                topic_match_confidence TEXT,
                likely_supervisor_candidate TEXT,
                current_affiliation_confidence TEXT,
                dedupe_key TEXT,
                record_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS email_evidence (
                email TEXT NOT NULL,
                source_url TEXT NOT NULL,
                person_id TEXT,
                source_type TEXT,
                domain_aligned INTEGER,
                official_source INTEGER,
                extracted_at TEXT,
                confidence REAL,
                verdict TEXT,
                association TEXT,
                record_json TEXT NOT NULL,
                PRIMARY KEY (email, source_url, person_id)
            );

            CREATE TABLE IF NOT EXISTS contact_verdicts (
                person_id TEXT PRIMARY KEY,
                verdict TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                recommended_action TEXT,
                last_live_checked_at TEXT,
                contact_confidence TEXT,
                pi_supervisor_confidence TEXT,
                topic_match_confidence TEXT,
                likely_supervisor_candidate TEXT,
                current_affiliation_confidence TEXT,
                record_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crawl_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                institution_id TEXT,
                source_url TEXT,
                stage TEXT,
                reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS match_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                applicant_source TEXT,
                person_id TEXT,
                display_name TEXT,
                institution_name TEXT,
                match_score REAL,
                institution_fit_score REAL,
                research_fit_score REAL,
                supervisor_validity_score REAL,
                topic_score REAL,
                supervision_score REAL,
                contact_score REAL,
                institution_score REAL,
                total_score REAL,
                topic_overlap TEXT,
                contact_verdict TEXT,
                explanation TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ingestion_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                institution_id TEXT,
                institution_name TEXT,
                config_name TEXT,
                pages_attempted INTEGER,
                pages_successfully_fetched INTEGER,
                pages_failed INTEGER,
                people_extracted INTEGER,
                emails_extracted INTEGER,
                status TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS duplicates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                institution_id TEXT,
                group_key TEXT,
                kept_person_id TEXT,
                duplicate_person_id TEXT,
                reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS parse_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                institution_id TEXT,
                source_url TEXT,
                parser_name TEXT,
                candidate_blocks INTEGER,
                people_extracted INTEGER,
                filtered_blocks INTEGER,
                created_at TEXT NOT NULL
            );
            """
        )
        self._ensure_schema_columns()
        self.conn.commit()

    def _ensure_schema_columns(self) -> None:
        columns: dict[str, list[tuple[str, str]]] = {
            "canonical_pi_records": [
                ("contact_confidence", "TEXT"),
                ("pi_supervisor_confidence", "TEXT"),
                ("topic_match_confidence", "TEXT"),
                ("likely_supervisor_candidate", "TEXT"),
                ("current_affiliation_confidence", "TEXT"),
                ("dedupe_key", "TEXT"),
            ],
            "email_evidence": [
                ("person_id", "TEXT"),
                ("association", "TEXT"),
            ],
            "contact_verdicts": [
                ("contact_confidence", "TEXT"),
                ("pi_supervisor_confidence", "TEXT"),
                ("topic_match_confidence", "TEXT"),
                ("likely_supervisor_candidate", "TEXT"),
                ("current_affiliation_confidence", "TEXT"),
            ],
            "match_results": [
                ("institution_fit_score", "REAL"),
                ("research_fit_score", "REAL"),
                ("supervisor_validity_score", "REAL"),
                ("topic_score", "REAL"),
                ("supervision_score", "REAL"),
                ("contact_score", "REAL"),
                ("institution_score", "REAL"),
                ("total_score", "REAL"),
            ],
        }
        for table, desired in columns.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, type_name in desired:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {type_name}")

    def upsert_institution(self, record: InstitutionRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO institutions
            (institution_id, name, aliases_json, country, region, ror_id, homepage_url,
             official_domains_json, qs_rank, qs_year, source, status, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(institution_id) DO UPDATE SET
                name=excluded.name,
                aliases_json=excluded.aliases_json,
                country=excluded.country,
                region=excluded.region,
                ror_id=excluded.ror_id,
                homepage_url=excluded.homepage_url,
                official_domains_json=excluded.official_domains_json,
                qs_rank=excluded.qs_rank,
                qs_year=excluded.qs_year,
                source=excluded.source,
                status=excluded.status,
                record_json=excluded.record_json
            """,
            (
                record.institution_id,
                record.name,
                _json(record.aliases),
                record.country,
                record.region,
                record.ror_id,
                record.homepage_url,
                _json(record.official_domains),
                record.qs_rank,
                record.qs_year,
                record.source,
                record.status,
                record.to_json(),
            ),
        )
        self.conn.commit()

    def insert_raw_source(self, record: RawSourceRecord) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO raw_sources
            (source_url, institution_id, fetched_at, source_type, http_status,
             content_hash, parser_used, crawl_method, error_reason, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.source_url,
                record.institution_id,
                record.fetched_at,
                record.source_type,
                record.http_status,
                record.content_hash,
                record.parser_used,
                record.crawl_method,
                record.error_reason,
                record.to_json(),
            ),
        )
        self.conn.commit()

    def insert_person_evidence(self, record: PersonEvidence) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO person_evidence
            (evidence_id, person_temp_id, institution_id, field_name, field_value,
             source_url, source_type, extraction_method, extracted_at, confidence,
             evidence_text, content_hash, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.evidence_id,
                record.person_temp_id,
                record.institution_id,
                record.field_name,
                record.field_value,
                record.source_url,
                record.source_type,
                record.extraction_method,
                record.extracted_at,
                record.confidence,
                record.evidence_text,
                record.content_hash,
                record.to_json(),
            ),
        )
        self.conn.commit()

    def upsert_pi_record(self, record: CanonicalPIRecord) -> None:
        dedupe_key = dedupe_key_for_record(record)
        self.conn.execute(
            """
            INSERT INTO canonical_pi_records
             (person_id, display_name, institution_id, institution_name, title,
             department, profile_url, emails_json, research_areas_json,
             contact_confidence, pi_supervisor_confidence, topic_match_confidence,
             likely_supervisor_candidate, current_affiliation_confidence, dedupe_key,
             record_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(person_id) DO UPDATE SET
                display_name=excluded.display_name,
                institution_id=excluded.institution_id,
                institution_name=excluded.institution_name,
                title=excluded.title,
                department=excluded.department,
                profile_url=excluded.profile_url,
                emails_json=excluded.emails_json,
                research_areas_json=excluded.research_areas_json,
                contact_confidence=excluded.contact_confidence,
                pi_supervisor_confidence=excluded.pi_supervisor_confidence,
                topic_match_confidence=excluded.topic_match_confidence,
                likely_supervisor_candidate=excluded.likely_supervisor_candidate,
                current_affiliation_confidence=excluded.current_affiliation_confidence,
                dedupe_key=excluded.dedupe_key,
                record_json=excluded.record_json,
                updated_at=excluded.updated_at
            """,
            (
                record.person_id,
                record.display_name,
                record.institution_id,
                record.institution_name,
                record.title,
                record.department,
                record.profile_url,
                _json(record.emails),
                _json(record.research_areas),
                record.contact_confidence,
                record.pi_supervisor_confidence,
                record.topic_match_confidence,
                record.likely_supervisor_candidate,
                record.current_affiliation_confidence,
                dedupe_key,
                record.to_json(),
                utc_now_iso(),
            ),
        )
        self.conn.commit()

    def insert_email_evidence(self, record: EmailEvidence) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO email_evidence
            (email, source_url, person_id, source_type, domain_aligned, official_source,
             extracted_at, confidence, verdict, association, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.email.lower(),
                record.source_url,
                record.person_id,
                record.source_type,
                int(record.domain_aligned),
                int(record.official_source),
                record.extracted_at,
                record.confidence,
                record.verdict,
                record.association,
                record.to_json(),
            ),
        )
        self.conn.commit()

    def upsert_contact_verdict(self, record: PIContactVerdict) -> None:
        self.conn.execute(
            """
            INSERT INTO contact_verdicts
            (person_id, verdict, reasons_json, recommended_action, last_live_checked_at,
             contact_confidence, pi_supervisor_confidence, topic_match_confidence,
             likely_supervisor_candidate, current_affiliation_confidence, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(person_id) DO UPDATE SET
                verdict=excluded.verdict,
                reasons_json=excluded.reasons_json,
                recommended_action=excluded.recommended_action,
                last_live_checked_at=excluded.last_live_checked_at,
                contact_confidence=excluded.contact_confidence,
                pi_supervisor_confidence=excluded.pi_supervisor_confidence,
                topic_match_confidence=excluded.topic_match_confidence,
                likely_supervisor_candidate=excluded.likely_supervisor_candidate,
                current_affiliation_confidence=excluded.current_affiliation_confidence,
                record_json=excluded.record_json
            """,
            (
                record.person_id,
                record.verdict,
                _json(record.reasons),
                record.recommended_action,
                record.last_live_checked_at,
                record.contact_confidence,
                record.pi_supervisor_confidence,
                record.topic_match_confidence,
                record.likely_supervisor_candidate,
                record.current_affiliation_confidence,
                record.to_json(),
            ),
        )
        self.conn.commit()

    def record_crawl_error(self, institution_id: str | None, source_url: str | None, stage: str, reason: str) -> None:
        self.conn.execute(
            """
            INSERT INTO crawl_errors (institution_id, source_url, stage, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (institution_id, source_url, stage, reason, utc_now_iso()),
        )
        self.conn.commit()

    def insert_match_result(
        self,
        applicant_source: str,
        person_id: str,
        display_name: str,
        institution_name: str,
        match_score: float,
        topic_score: float,
        supervision_score: float,
        contact_score: float,
        institution_score: float,
        total_score: float,
        topic_overlap: str,
        contact_verdict: str,
        explanation: str,
        institution_fit_score: float | None = None,
        research_fit_score: float | None = None,
        supervisor_validity_score: float | None = None,
    ) -> None:
        institution_fit_score = institution_score if institution_fit_score is None else institution_fit_score
        research_fit_score = topic_score if research_fit_score is None else research_fit_score
        supervisor_validity_score = supervision_score if supervisor_validity_score is None else supervisor_validity_score
        self.conn.execute(
            """
            INSERT INTO match_results
            (applicant_source, person_id, display_name, institution_name, match_score,
             institution_fit_score, research_fit_score, supervisor_validity_score,
             topic_score, supervision_score, contact_score, institution_score, total_score,
             topic_overlap, contact_verdict, explanation, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                applicant_source,
                person_id,
                display_name,
                institution_name,
                match_score,
                institution_fit_score,
                research_fit_score,
                supervisor_validity_score,
                topic_score,
                supervision_score,
                contact_score,
                institution_score,
                total_score,
                topic_overlap,
                contact_verdict,
                explanation,
                utc_now_iso(),
            ),
        )
        self.conn.commit()

    def iter_pi_records(self) -> Iterable[CanonicalPIRecord]:
        rows = self.conn.execute("SELECT record_json FROM canonical_pi_records").fetchall()
        for row in rows:
            yield CanonicalPIRecord(**json.loads(row["record_json"]))

    def get_contact_verdicts(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT person_id, verdict FROM contact_verdicts").fetchall()
        return {row["person_id"]: row["verdict"] for row in rows}

    def get_contact_verdict_records(self) -> dict[str, PIContactVerdict]:
        rows = self.conn.execute("SELECT record_json FROM contact_verdicts").fetchall()
        return {data["person_id"]: PIContactVerdict(**data) for data in (json.loads(row["record_json"]) for row in rows)}

    def get_pi_record(self, person_id: str) -> CanonicalPIRecord | None:
        row = self.conn.execute("SELECT record_json FROM canonical_pi_records WHERE person_id=?", (person_id,)).fetchone()
        if not row:
            return None
        return CanonicalPIRecord(**json.loads(row["record_json"]))

    def find_existing_duplicate(self, record: CanonicalPIRecord) -> tuple[str, str] | None:
        rows = self.conn.execute(
            "SELECT person_id, record_json FROM canonical_pi_records WHERE institution_id=?",
            (record.institution_id,),
        ).fetchall()
        record_name = _normalize_key_part(record.display_name)
        record_emails = set(e.lower() for e in record.emails)
        record_profile = _normalize_key_part(record.profile_url)
        for row in rows:
            existing = CanonicalPIRecord(**json.loads(row["record_json"]))
            if row["person_id"] == record.person_id:
                continue
            if _normalize_key_part(existing.display_name) != record_name:
                continue
            existing_emails = set(e.lower() for e in existing.emails)
            if record_profile and record_profile == _normalize_key_part(existing.profile_url):
                return existing.person_id, "same_normalized_name_and_profile_url"
            if record_emails and existing_emails and record_emails.intersection(existing_emails):
                return existing.person_id, "same_normalized_name_and_email"
        return None

    def record_duplicate(self, institution_id: str, group_key: str, kept_person_id: str, duplicate_person_id: str, reason: str) -> None:
        self.conn.execute(
            """
            INSERT INTO duplicates (institution_id, group_key, kept_person_id, duplicate_person_id, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (institution_id, group_key, kept_person_id, duplicate_person_id, reason, utc_now_iso()),
        )
        self.conn.commit()

    def record_parse_metric(
        self,
        institution_id: str,
        source_url: str,
        parser_name: str,
        candidate_blocks: int,
        people_extracted: int,
        filtered_blocks: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO parse_metrics
            (institution_id, source_url, parser_name, candidate_blocks, people_extracted, filtered_blocks, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                institution_id,
                source_url,
                parser_name,
                candidate_blocks,
                people_extracted,
                filtered_blocks,
                utc_now_iso(),
            ),
        )
        self.conn.commit()

    def record_ingestion_run(
        self,
        institution_id: str,
        institution_name: str,
        config_name: str,
        pages_attempted: int,
        pages_successfully_fetched: int,
        pages_failed: int,
        people_extracted: int,
        emails_extracted: int,
        status: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO ingestion_runs
            (institution_id, institution_name, config_name, pages_attempted, pages_successfully_fetched,
             pages_failed, people_extracted, emails_extracted, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                institution_id,
                institution_name,
                config_name,
                pages_attempted,
                pages_successfully_fetched,
                pages_failed,
                people_extracted,
                emails_extracted,
                status,
                utc_now_iso(),
            ),
        )
        self.conn.commit()

    def audit_counts(self) -> dict[str, Any]:
        counts: dict[str, Any] = {}
        for key, sql in {
            "institutions_imported": "SELECT COUNT(*) FROM institutions",
            "institutions_selected_for_batch": "SELECT COUNT(DISTINCT institution_id) FROM ingestion_runs",
            "institutions_attempted_crawl": "SELECT COUNT(DISTINCT institution_id) FROM raw_sources",
            "institutions_successfully_crawled": "SELECT COUNT(DISTINCT institution_id) FROM raw_sources WHERE http_status BETWEEN 200 AND 299",
            "institutions_failed": """
                SELECT COUNT(*) FROM (
                    SELECT institution_id
                    FROM ingestion_runs
                    GROUP BY institution_id
                    HAVING SUM(pages_successfully_fetched)=0
                )
            """,
            "pages_attempted": "SELECT COUNT(*) FROM raw_sources",
            "pages_successfully_fetched": "SELECT COUNT(*) FROM raw_sources WHERE http_status BETWEEN 200 AND 299",
            "pages_failed": "SELECT COUNT(*) FROM raw_sources WHERE http_status IS NULL OR http_status < 200 OR http_status >= 300",
            "candidate_people_extracted": "SELECT COUNT(DISTINCT person_temp_id) FROM person_evidence",
            "canonical_pi_records_created": "SELECT COUNT(*) FROM canonical_pi_records",
            "emails_extracted": "SELECT COUNT(DISTINCT email) FROM email_evidence",
            "high_confidence_contactable": "SELECT COUNT(*) FROM contact_verdicts WHERE verdict='high_confidence_contactable'",
            "likely_supervisor_candidates": "SELECT COUNT(*) FROM contact_verdicts WHERE likely_supervisor_candidate='true'",
            "ambiguous_records": "SELECT COUNT(*) FROM contact_verdicts WHERE contact_confidence IN ('none','low') OR likely_supervisor_candidate IN ('unknown','false')",
            "stale_risk": "SELECT COUNT(*) FROM contact_verdicts WHERE verdict='stale_risk'",
            "failures": "SELECT COUNT(*) FROM crawl_errors",
            "ambiguous_non_person_blocks_filtered": "SELECT COALESCE(SUM(filtered_blocks), 0) FROM parse_metrics",
            "duplicate_records": "SELECT COUNT(*) FROM duplicates",
        }.items():
            counts[key] = self.conn.execute(sql).fetchone()[0]
        rows = self.conn.execute(
            "SELECT reason, COUNT(*) AS n FROM crawl_errors GROUP BY reason ORDER BY n DESC, reason"
        ).fetchall()
        counts["failure_reasons"] = {row["reason"]: row["n"] for row in rows}
        return counts

    def write_audit_sample(self, out_path: str | Path, sample_size: int = 60) -> int:
        requested = [
            (
                20,
                """
                SELECT p.person_id
                FROM canonical_pi_records p JOIN contact_verdicts c ON c.person_id=p.person_id
                WHERE c.verdict='high_confidence_contactable'
                ORDER BY RANDOM() LIMIT 20
                """,
            ),
            (
                20,
                """
                SELECT p.person_id
                FROM canonical_pi_records p
                WHERE p.title LIKE '%Professor%' OR p.title LIKE '%Reader%' OR p.title LIKE '%Lecturer%'
                ORDER BY RANDOM() LIMIT 20
                """,
            ),
            (
                10,
                """
                SELECT p.person_id
                FROM canonical_pi_records p
                WHERE p.title IS NULL
                   OR (p.title NOT LIKE '%Professor%' AND p.title NOT LIKE '%Reader%' AND p.title NOT LIKE '%Lecturer%')
                ORDER BY RANDOM() LIMIT 10
                """,
            ),
            (
                10,
                """
                SELECT p.person_id
                FROM canonical_pi_records p JOIN contact_verdicts c ON c.person_id=p.person_id
                WHERE c.verdict!='high_confidence_contactable'
                   OR c.contact_confidence IN ('none','low')
                   OR c.likely_supervisor_candidate IN ('unknown','false')
                ORDER BY RANDOM() LIMIT 10
                """,
            ),
        ]
        selected: list[str] = []
        seen: set[str] = set()
        for _limit, sql in requested:
            for row in self.conn.execute(sql).fetchall():
                person_id = row["person_id"]
                if person_id not in seen:
                    selected.append(person_id)
                    seen.add(person_id)
        if len(selected) < sample_size:
            rows = self.conn.execute(
                """
                SELECT person_id FROM canonical_pi_records
                ORDER BY RANDOM()
                """
            ).fetchall()
            for row in rows:
                person_id = row["person_id"]
                if person_id not in seen:
                    selected.append(person_id)
                    seen.add(person_id)
                if len(selected) >= sample_size:
                    break

        fieldnames = [
            "person_id",
            "display_name",
            "title",
            "department",
            "institution_name",
            "email",
            "verdict",
            "contact_confidence",
            "pi_supervisor_confidence",
            "topic_match_confidence",
            "likely_supervisor_candidate",
            "source_url",
            "evidence_text",
            "extraction_method",
            "parser_used",
            "source_type",
            "domain_aligned",
            "official_source",
            "supervisor_signal",
            "reasons",
            "manually_verified_name_email_pair",
            "manually_verified_title",
            "manually_verified_pi_or_supervisor",
            "manually_verified_current_affiliation",
            "audit_error_type",
            "audit_notes",
        ]
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for person_id in selected[:sample_size]:
                writer.writerow(self._audit_row_for_person(person_id, fieldnames))
        return min(len(selected), sample_size)

    def _audit_row_for_person(self, person_id: str, fieldnames: list[str]) -> dict[str, Any]:
        pi_row = self.conn.execute("SELECT record_json FROM canonical_pi_records WHERE person_id=?", (person_id,)).fetchone()
        verdict_row = self.conn.execute("SELECT record_json FROM contact_verdicts WHERE person_id=?", (person_id,)).fetchone()
        if not pi_row:
            return {name: "" for name in fieldnames} | {"person_id": person_id}
        pi = CanonicalPIRecord(**json.loads(pi_row["record_json"]))
        verdict = PIContactVerdict(**json.loads(verdict_row["record_json"])) if verdict_row else None
        email_row = self.conn.execute(
            """
            SELECT record_json FROM email_evidence
            WHERE person_id=?
            ORDER BY official_source DESC, domain_aligned DESC, confidence DESC, email
            LIMIT 1
            """,
            (person_id,),
        ).fetchone()
        email_ev = EmailEvidence(**json.loads(email_row["record_json"])) if email_row else None

        evidence_rows = []
        if pi.source_evidence_ids:
            placeholders = ",".join("?" for _ in pi.source_evidence_ids)
            evidence_rows = self.conn.execute(
                f"SELECT record_json FROM person_evidence WHERE evidence_id IN ({placeholders})",
                tuple(pi.source_evidence_ids),
            ).fetchall()
        evidences = [PersonEvidence(**json.loads(row["record_json"])) for row in evidence_rows]
        preferred = next((e for e in evidences if e.field_name == "emails"), None) or next(iter(evidences), None)
        reasons = verdict.reasons if verdict else []
        row = {name: "" for name in fieldnames}
        row.update(
            {
                "person_id": pi.person_id,
                "display_name": pi.display_name,
                "title": pi.title or "",
                "department": pi.department or "",
                "institution_name": pi.institution_name,
                "email": email_ev.email if email_ev else (pi.emails[0] if pi.emails else ""),
                "verdict": verdict.verdict if verdict else "unverified",
                "contact_confidence": verdict.contact_confidence if verdict else pi.contact_confidence,
                "pi_supervisor_confidence": verdict.pi_supervisor_confidence if verdict else pi.pi_supervisor_confidence,
                "topic_match_confidence": verdict.topic_match_confidence if verdict else pi.topic_match_confidence,
                "likely_supervisor_candidate": verdict.likely_supervisor_candidate if verdict else pi.likely_supervisor_candidate,
                "source_url": preferred.source_url if preferred else (email_ev.source_url if email_ev else pi.profile_url or ""),
                "evidence_text": preferred.evidence_text if preferred else "",
                "extraction_method": preferred.extraction_method if preferred else "",
                "parser_used": preferred.extraction_method if preferred else "",
                "source_type": preferred.source_type if preferred else (email_ev.source_type if email_ev else ""),
                "domain_aligned": "" if email_ev is None else str(email_ev.domain_aligned).lower(),
                "official_source": "" if email_ev is None else str(email_ev.official_source).lower(),
                "supervisor_signal": "; ".join(pi.supervision_signals),
                "reasons": "; ".join(reasons),
            }
        )
        return row

    def export(self, out_dir: str | Path) -> None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        self._export_query(
            out / "institutions.csv",
            "SELECT institution_id, name, country, region, ror_id, homepage_url, qs_rank, qs_year, source, status FROM institutions ORDER BY name",
        )
        self._export_jsonl(out / "pi_records.jsonl", "SELECT record_json FROM canonical_pi_records ORDER BY institution_name, display_name")
        self._export_query(
            out / "contact_verdicts.csv",
            """
            SELECT person_id, verdict, contact_confidence, pi_supervisor_confidence,
                   topic_match_confidence, likely_supervisor_candidate,
                   current_affiliation_confidence, reasons_json AS reasons,
                   recommended_action, last_live_checked_at
            FROM contact_verdicts ORDER BY verdict, person_id
            """,
        )
        self._export_query(
            out / "high_confidence_contactable.csv",
            """
            SELECT c.person_id, p.display_name, p.institution_name, p.title,
                   p.emails_json AS emails, c.verdict, c.contact_confidence,
                   c.pi_supervisor_confidence, c.likely_supervisor_candidate,
                   c.recommended_action
            FROM contact_verdicts c JOIN canonical_pi_records p ON p.person_id=c.person_id
            WHERE c.verdict='high_confidence_contactable'
            ORDER BY p.institution_name, p.display_name
            """,
        )
        self._export_query(
            out / "verified_supervisor_candidates.csv",
            """
            SELECT p.person_id, p.display_name, p.title, p.department, p.institution_name,
                   p.profile_url, p.emails_json AS emails, p.research_areas_json AS research_areas,
                   c.verdict, c.contact_confidence, c.pi_supervisor_confidence,
                   c.topic_match_confidence, c.likely_supervisor_candidate,
                   c.current_affiliation_confidence, c.reasons_json AS reasons
            FROM canonical_pi_records p
            JOIN contact_verdicts c ON c.person_id=p.person_id
            WHERE c.likely_supervisor_candidate='true'
              AND c.contact_confidence IN ('high', 'medium')
              AND c.current_affiliation_confidence != 'low'
              AND c.verdict NOT IN ('retired_or_emeritus_risk', 'current_affiliation_conflict')
            ORDER BY p.institution_name, p.display_name
            """,
        )
        self._export_query(
            out / "plausible_supervisor_review_queue.csv",
            """
            SELECT p.person_id, p.display_name, p.title, p.department, p.institution_name,
                   p.profile_url, p.emails_json AS emails, p.research_areas_json AS research_areas,
                   c.verdict, c.contact_confidence, c.pi_supervisor_confidence,
                   c.topic_match_confidence, c.likely_supervisor_candidate,
                   c.current_affiliation_confidence, c.reasons_json AS reasons
            FROM canonical_pi_records p
            JOIN contact_verdicts c ON c.person_id=p.person_id
            WHERE c.likely_supervisor_candidate='unknown'
               OR c.pi_supervisor_confidence='medium'
               OR c.verdict IN ('stale_risk', 'current_affiliation_conflict', 'no_official_email')
            ORDER BY p.institution_name, p.display_name
            """,
        )
        self._export_query(
            out / "contactable_non_supervisor.csv",
            """
            SELECT p.person_id, p.display_name, p.title, p.department, p.institution_name,
                   p.profile_url, p.emails_json AS emails, c.verdict,
                   c.contact_confidence, c.pi_supervisor_confidence,
                   c.likely_supervisor_candidate, c.reasons_json AS reasons
            FROM canonical_pi_records p
            JOIN contact_verdicts c ON c.person_id=p.person_id
            WHERE c.contact_confidence IN ('high', 'medium')
              AND c.likely_supervisor_candidate='false'
            ORDER BY p.institution_name, p.display_name
            """,
        )
        self._export_query(
            out / "stale_risk.csv",
            """
            SELECT c.person_id, p.display_name, p.institution_name, p.title,
                   p.emails_json AS emails, c.verdict, c.contact_confidence,
                   c.pi_supervisor_confidence, c.likely_supervisor_candidate,
                   c.recommended_action
            FROM contact_verdicts c JOIN canonical_pi_records p ON p.person_id=c.person_id
            WHERE c.verdict IN ('stale_risk', 'current_affiliation_conflict')
            ORDER BY p.institution_name, p.display_name
            """,
        )
        self._export_query(
            out / "failures.csv",
            "SELECT institution_id, source_url, stage, reason, created_at FROM crawl_errors ORDER BY created_at",
        )
        self._export_jsonl(out / "evidence.jsonl", "SELECT record_json FROM person_evidence ORDER BY institution_id, person_temp_id")
        self._export_query(
            out / "match_results.csv",
            """
            SELECT applicant_source, person_id, display_name, institution_name, match_score,
                   institution_fit_score, research_fit_score, supervisor_validity_score,
                   topic_score, supervision_score, contact_score, institution_score, total_score,
                   topic_overlap, contact_verdict, explanation, created_at
            FROM match_results ORDER BY created_at, total_score DESC
            """,
        )
        self._export_query(
            out / "supervisor_candidates.csv",
            """
            SELECT p.person_id, p.display_name, p.title, p.department, p.institution_name,
                   p.profile_url, p.emails_json AS emails, p.research_areas_json AS research_areas,
                   c.verdict, c.contact_confidence, c.pi_supervisor_confidence,
                   c.topic_match_confidence, c.likely_supervisor_candidate,
                   c.current_affiliation_confidence, c.reasons_json AS reasons
            FROM canonical_pi_records p
            JOIN contact_verdicts c ON c.person_id=p.person_id
            WHERE c.contact_confidence IN ('high', 'medium')
              AND c.current_affiliation_confidence != 'low'
              AND c.verdict NOT IN ('retired_or_emeritus_risk', 'current_affiliation_conflict')
              AND (
                  c.likely_supervisor_candidate='true'
                  OR (c.likely_supervisor_candidate='unknown' AND c.pi_supervisor_confidence='medium')
              )
            ORDER BY p.institution_name, p.display_name
            """,
        )
        self._export_query(
            out / "duplicates.csv",
            """
            SELECT institution_id, group_key, kept_person_id, duplicate_person_id, reason, created_at
            FROM duplicates ORDER BY created_at, group_key
            """,
        )
        self._export_query(
            out / "parse_metrics.csv",
            """
            SELECT institution_id, source_url, parser_name, candidate_blocks,
                   people_extracted, filtered_blocks, created_at
            FROM parse_metrics ORDER BY created_at, source_url, parser_name
            """,
        )
        self.export_institution_quality_report(out / "institution_quality_report.csv")

    def export_institution_quality_report(self, path: str | Path) -> None:
        rows = self.conn.execute(
            """
            SELECT
                i.name AS institution_name,
                COALESCE(MAX(r.config_name), i.source) AS config_name,
                COUNT(DISTINCT rs.source_url) AS pages_attempted,
                COUNT(DISTINCT CASE WHEN rs.http_status BETWEEN 200 AND 299 THEN rs.source_url END) AS pages_successfully_fetched,
                COUNT(DISTINCT p.person_id) AS people_extracted,
                COUNT(DISTINCT ee.email) AS emails_extracted,
                COUNT(DISTINCT CASE WHEN c.verdict='high_confidence_contactable' THEN c.person_id END) AS high_confidence_contactable,
                COUNT(DISTINCT CASE WHEN c.likely_supervisor_candidate='true' THEN c.person_id END) AS likely_supervisor_candidates,
                COUNT(DISTINCT CASE WHEN c.likely_supervisor_candidate='unknown' OR c.pi_supervisor_confidence='medium' THEN c.person_id END) AS plausible_supervisor_review_queue,
                COUNT(DISTINCT CASE WHEN c.verdict IN ('stale_risk', 'current_affiliation_conflict') THEN c.person_id END) AS stale_risk,
                COUNT(DISTINCT CASE WHEN c.contact_confidence IN ('none','low') OR c.likely_supervisor_candidate IN ('unknown','false') THEN c.person_id END) AS ambiguous_records,
                COUNT(DISTINCT ce.id) AS failures,
                COALESCE((
                    SELECT SUM(pm.filtered_blocks)
                    FROM parse_metrics pm
                    WHERE pm.institution_id=i.institution_id
                ), 0) AS ambiguous_non_person_blocks_filtered,
                COALESCE((
                    SELECT COUNT(*)
                    FROM duplicates d
                    WHERE d.institution_id=i.institution_id
                ), 0) AS duplicate_records,
                COALESCE((
                    SELECT pe.extraction_method
                    FROM person_evidence pe
                    WHERE pe.institution_id=i.institution_id
                    GROUP BY pe.extraction_method
                    ORDER BY COUNT(*) DESC, pe.extraction_method
                    LIMIT 1
                ), '') AS dominant_parser_used
            FROM institutions i
            LEFT JOIN ingestion_runs r ON r.institution_id=i.institution_id
            LEFT JOIN raw_sources rs ON rs.institution_id=i.institution_id
            LEFT JOIN canonical_pi_records p ON p.institution_id=i.institution_id
            LEFT JOIN email_evidence ee ON ee.person_id=p.person_id
            LEFT JOIN contact_verdicts c ON c.person_id=p.person_id
            LEFT JOIN crawl_errors ce ON ce.institution_id=i.institution_id
            GROUP BY i.institution_id, i.name, i.source
            ORDER BY i.name
            """
        ).fetchall()
        fieldnames = [
            "institution_name",
            "config_name",
            "pages_attempted",
            "pages_successfully_fetched",
            "people_extracted",
            "emails_extracted",
            "high_confidence_contactable",
            "likely_supervisor_candidates",
            "plausible_supervisor_review_queue",
            "stale_risk",
            "ambiguous_records",
            "failures",
            "ambiguous_non_person_blocks_filtered",
            "duplicate_records",
            "dominant_parser_used",
            "notes",
        ]
        with Path(path).open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                data = dict(row)
                notes = []
                if data["people_extracted"] and data["high_confidence_contactable"] > data["people_extracted"] * 0.75:
                    notes.append("high contactable ratio; inspect sample")
                if data["failures"]:
                    notes.append("crawl failures present")
                data["notes"] = "; ".join(notes)
                writer.writerow(data)

    def _export_query(self, path: Path, sql: str) -> None:
        rows = self.conn.execute(sql).fetchall()
        if rows:
            fieldnames = rows[0].keys()
        else:
            probe = self.conn.execute(sql + " LIMIT 0") if "LIMIT" not in sql.upper() else self.conn.execute(sql)
            fieldnames = probe.description and [d[0] for d in probe.description] or []
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))

    def _export_jsonl(self, path: Path, sql: str) -> None:
        rows = self.conn.execute(sql).fetchall()
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(row["record_json"] + "\n")
