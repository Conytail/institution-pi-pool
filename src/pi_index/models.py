from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_id(prefix: str, *parts: object) -> str:
    raw = "\n".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def content_hash(text: str | bytes | None) -> str:
    if text is None:
        text = ""
    if isinstance(text, str):
        text = text.encode("utf-8", errors="ignore")
    return hashlib.sha256(text).hexdigest()


@dataclass
class ModelMixin:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


@dataclass
class InstitutionRecord(ModelMixin):
    institution_id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    country: str | None = None
    region: str | None = None
    ror_id: str | None = None
    homepage_url: str | None = None
    official_domains: list[str] = field(default_factory=list)
    qs_rank: int | None = None
    qs_year: int | None = None
    source: str = "config"
    status: str = "active"


@dataclass
class RawSourceRecord(ModelMixin):
    source_url: str
    source_type: str
    institution_id: str
    fetched_at: str
    http_status: int | None
    content_hash: str
    parser_used: str | None = None
    crawl_method: str = "configured_seed"
    error_reason: str | None = None


@dataclass
class PersonEvidence(ModelMixin):
    evidence_id: str
    person_temp_id: str
    institution_id: str
    field_name: str
    field_value: str
    source_url: str
    source_type: str
    extraction_method: str
    extracted_at: str
    confidence: float
    evidence_text: str
    content_hash: str


@dataclass
class CanonicalPIRecord(ModelMixin):
    person_id: str
    display_name: str
    given_name: str | None
    family_name: str | None
    aliases: list[str]
    institution_id: str
    institution_name: str
    ror_id: str | None
    department: str | None
    title: str | None
    profile_url: str | None
    lab_url: str | None
    emails: list[str]
    research_areas: list[str]
    publications_summary: dict[str, Any]
    external_ids: dict[str, Any]
    supervision_signals: list[str]
    source_evidence_ids: list[str]
    last_checked_at: str
    contact_confidence: str = "none"
    pi_supervisor_confidence: str = "unknown"
    topic_match_confidence: str = "unknown"
    likely_supervisor_candidate: str = "unknown"
    current_affiliation_confidence: str = "unknown"
    schema_version: int = 1


@dataclass
class EmailEvidence(ModelMixin):
    email: str
    source_url: str
    source_type: str
    domain_aligned: bool
    official_source: bool
    extracted_at: str
    confidence: float
    verdict: str
    person_id: str | None = None
    association: str = "person_local"


@dataclass
class PIContactVerdict(ModelMixin):
    person_id: str
    verdict: str
    reasons: list[str]
    recommended_action: str
    last_live_checked_at: str
    contact_confidence: str = "none"
    pi_supervisor_confidence: str = "unknown"
    topic_match_confidence: str = "unknown"
    likely_supervisor_candidate: str = "unknown"
    current_affiliation_confidence: str = "unknown"


@dataclass
class ParsedPerson(ModelMixin):
    name: str
    source_url: str
    source_type: str
    extraction_method: str
    evidence_text: str
    title: str | None = None
    department: str | None = None
    profile_url: str | None = None
    lab_url: str | None = None
    emails: list[str] = field(default_factory=list)
    ambiguous_emails: list[str] = field(default_factory=list)
    research_areas: list[str] = field(default_factory=list)
    external_ids: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.7
    email_association: str = "person_local"

    @property
    def person_temp_id(self) -> str:
        return stable_id("tmp_person", self.name.lower(), self.profile_url or self.source_url)
