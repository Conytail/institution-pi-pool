from __future__ import annotations

from dataclasses import dataclass, field
import csv
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import urlparse

from ..models import CanonicalPIRecord, PIContactVerdict
from ..sources.crossref import CrossrefClient
from ..sources.openalex import OpenAlexClient
from ..storage import PIIndexStorage
from .applicant_parser import load_applicant_text
from .candidate_retrieval import has_research_evidence, record_text
from .semantic import paper_relevance_score, research_intent_text, semantic_similarity, semantic_vector, shared_terms


STOPWORDS = {
    "about",
    "across",
    "after",
    "also",
    "america",
    "analysis",
    "and",
    "application",
    "applications",
    "approach",
    "applicant",
    "background",
    "based",
    "between",
    "candidate",
    "computer",
    "data",
    "degree",
    "develop",
    "europe",
    "for",
    "from",
    "human",
    "includes",
    "interested",
    "into",
    "learning",
    "machine",
    "method",
    "methods",
    "model",
    "models",
    "phd",
    "pipeline",
    "pipelines",
    "proposal",
    "prospective",
    "projects",
    "python",
    "regions",
    "research",
    "reliable",
    "science",
    "systems",
    "target",
    "the",
    "this",
    "through",
    "using",
    "with",
}

METHOD_TERMS = [
    "machine learning",
    "deep learning",
    "reinforcement learning",
    "natural language processing",
    "computer vision",
    "robotics",
    "optimization",
    "data engineering",
    "distributed systems",
    "visual computing",
    "human-ai interaction",
    "human ai interaction",
    "reliable ml",
]

DOMAIN_TERMS = [
    "healthcare",
    "education",
    "robotics",
    "sustainability",
    "smart city",
    "cybersecurity",
    "finance",
    "manufacturing",
    "visual computing",
    "human-ai interaction",
    "human ai interaction",
]

DATA_TYPE_TERMS = [
    "image",
    "images",
    "video",
    "sensor",
    "text",
    "graph",
    "time series",
    "tabular",
    "multimodal",
    "speech",
    "robot",
    "pipeline",
]


@dataclass
class ProposalTopics:
    core_research_problem: str
    methods: list[str]
    application_domain: str
    data_type: str
    keywords: list[str]
    expanded_search_queries: list[str]


@dataclass
class Paper:
    title: str
    abstract: str
    year: int | None
    authors: list[dict[str, Any]]
    institutions: list[str]
    openalex_id: str | None = None
    doi: str | None = None
    source: str = "openalex"
    relevance_score: float = 0.0


@dataclass
class AuthorMatchEvidence:
    paper_title: str
    author_name: str
    pi_name: str
    name_score: float
    author_institutions: list[str]
    source_id: str
    doi: str | None


@dataclass
class PaperBacktraceResult:
    record: CanonicalPIRecord
    verdict: PIContactVerdict | None
    paper_backtrace_score: float
    matched_papers: list[Paper] = field(default_factory=list)
    matched_author_evidence: list[AuthorMatchEvidence] = field(default_factory=list)
    publication_topic_clusters: list[str] = field(default_factory=list)
    profile_topic_score: float = 0.0
    institution_fit_score: float = 1.0
    semantic_fallback_score: float = 0.0
    research_fit_score: float = 0.0
    overall_score: float = 0.0
    final_research_fit_score: float = 0.0
    reason_for_match: str = ""
    risk_flags: list[str] = field(default_factory=list)
    recommended_action: str = "review"


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z][a-z0-9+-]{2,}", (text or "").lower())
        if token not in STOPWORDS
    ]


def _phrases(text: str, choices: list[str]) -> list[str]:
    lower = (text or "").lower()
    found = []
    for choice in choices:
        if choice in lower and choice not in found:
            found.append(choice)
    return found


def _top_keywords(text: str, limit: int = 14) -> list[str]:
    counts: dict[str, int] = {}
    for token in _tokens(text):
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _count in ranked[:limit]]


def extract_proposal_topics(proposal_text: str) -> ProposalTopics:
    methods = _phrases(proposal_text, METHOD_TERMS)
    domains = _phrases(proposal_text, DOMAIN_TERMS)
    data_types = _phrases(proposal_text, DATA_TYPE_TERMS)
    keywords = _top_keywords(proposal_text)
    semantic_terms = [term.replace("_", " ") for term in semantic_vector(proposal_text, max_features=24)]
    first_sentence = re.split(r"(?<=[.!?])\s+", proposal_text.strip())[0] if proposal_text.strip() else ""
    core_problem = first_sentence[:240] or "Research fit from applicant proposal"
    application_domain = ", ".join(domains[:4]) if domains else ""
    data_type = ", ".join(data_types[:4]) if data_types else ""

    query_parts = []
    if methods:
        query_parts.append(" ".join(methods[:3]))
    if domains:
        query_parts.append(" ".join(domains[:2]))
    if data_types:
        query_parts.append(" ".join(data_types[:2]))
    if keywords:
        query_parts.append(" ".join(keywords[:6]))
    if semantic_terms:
        query_parts.append(" ".join(semantic_terms[:8]))

    expanded = []
    for part in query_parts:
        if part and part not in expanded:
            expanded.append(part)
    combined = " ".join((methods + domains + keywords + semantic_terms)[:10]).strip()
    if combined and combined not in expanded:
        expanded.insert(0, combined)
    if not expanded and proposal_text.strip():
        expanded.append(proposal_text.strip()[:180])
    return ProposalTopics(core_problem, methods, application_domain, data_type, keywords, expanded[:6])


def _abstract_from_openalex(work: dict[str, Any]) -> str:
    index = work.get("abstract_inverted_index") or {}
    if not index:
        return ""
    positioned: list[tuple[int, str]] = []
    for word, positions in index.items():
        for pos in positions or []:
            positioned.append((int(pos), word))
    return " ".join(word for _pos, word in sorted(positioned))


def _openalex_author_info(authorship: dict[str, Any]) -> dict[str, Any]:
    author = authorship.get("author") or {}
    institutions = []
    for inst in authorship.get("institutions") or []:
        name = inst.get("display_name")
        if name:
            institutions.append(name)
    for raw in authorship.get("raw_affiliation_strings") or []:
        if raw and raw not in institutions:
            institutions.append(raw)
    return {
        "name": author.get("display_name") or "",
        "openalex_id": author.get("id"),
        "institutions": institutions,
    }


def _paper_from_openalex(work: dict[str, Any], query: str, proposal_terms: set[str], proposal_text: str = "") -> Paper:
    abstract = _abstract_from_openalex(work)
    title = work.get("display_name") or work.get("title") or ""
    text = f"{title} {abstract}"
    relevance = paper_relevance_score(proposal_text, text) if proposal_text else lexical_similarity(query, text, proposal_terms)
    authors = [_openalex_author_info(authorship) for authorship in work.get("authorships") or []]
    institutions = sorted({inst for author in authors for inst in author.get("institutions", [])})
    return Paper(
        title=title,
        abstract=abstract,
        year=work.get("publication_year"),
        authors=authors,
        institutions=institutions,
        openalex_id=work.get("id"),
        doi=work.get("doi"),
        relevance_score=relevance,
    )


def _paper_from_crossref(item: dict[str, Any], query: str, proposal_terms: set[str], proposal_text: str = "") -> Paper:
    title = " ".join(item.get("title") or []) if isinstance(item.get("title"), list) else item.get("title") or ""
    abstract = re.sub(r"<[^>]+>", " ", item.get("abstract") or "")
    issued = item.get("issued", {}).get("date-parts", [[None]])
    year = issued[0][0] if issued and issued[0] else None
    authors = []
    for author in item.get("author") or []:
        name = " ".join(part for part in [author.get("given"), author.get("family")] if part)
        affiliations = [aff.get("name") for aff in author.get("affiliation") or [] if aff.get("name")]
        authors.append({"name": name, "openalex_id": None, "institutions": affiliations})
    institutions = sorted({inst for author in authors for inst in author.get("institutions", [])})
    return Paper(
        title=title,
        abstract=abstract,
        year=year,
        authors=authors,
        institutions=institutions,
        doi=item.get("DOI"),
        source="crossref",
        relevance_score=(
            paper_relevance_score(proposal_text, f"{title} {abstract}")
            if proposal_text
            else lexical_similarity(query, f"{title} {abstract}", proposal_terms)
        ),
    )


def lexical_similarity(query: str, text: str, proposal_terms: set[str] | None = None) -> float:
    query_terms = set(_tokens(query))
    text_terms = set(_tokens(text))
    if proposal_terms:
        query_terms |= proposal_terms
    if not query_terms or not text_terms:
        return 0.0
    overlap = query_terms.intersection(text_terms)
    return round(min(1.0, len(overlap) / math.sqrt(len(query_terms) * max(1, min(len(text_terms), 80))) * 3.0), 4)


def _dedupe_papers(papers: Iterable[Paper]) -> list[Paper]:
    deduped: dict[str, Paper] = {}
    for paper in papers:
        key = (paper.openalex_id or paper.doi or paper.title).lower()
        if not key:
            continue
        existing = deduped.get(key)
        if not existing or paper.relevance_score > existing.relevance_score:
            deduped[key] = paper
    return sorted(deduped.values(), key=lambda item: item.relevance_score, reverse=True)


def retrieve_papers(
    topics: ProposalTopics,
    institution_name: str = "",
    proposal_text: str = "",
    openalex_client: OpenAlexClient | None = None,
    crossref_client: CrossrefClient | None = None,
    per_query: int = 8,
    max_papers: int = 30,
) -> list[Paper]:
    openalex = openalex_client or OpenAlexClient()
    crossref = crossref_client or CrossrefClient()
    proposal_terms = set(topics.keywords + topics.methods)

    institution_id = None
    if institution_name:
        for inst in openalex.search_institutions(institution_name, limit=3):
            if (inst.get("display_name") or "").lower() == institution_name.lower():
                institution_id = inst.get("id")
                break
        if not institution_id:
            institutions = openalex.search_institutions(institution_name, limit=1)
            institution_id = institutions[0].get("id") if institutions else None

    papers: list[Paper] = []
    for query in topics.expanded_search_queries:
        for work in openalex.search_works(query, limit=per_query, institution_id=institution_id):
            papers.append(_paper_from_openalex(work, query, proposal_terms, proposal_text))
        if len(papers) < per_query:
            for work in openalex.search_works(query, limit=max(3, per_query // 2)):
                papers.append(_paper_from_openalex(work, query, proposal_terms, proposal_text))

    if len(papers) < 5:
        for query in topics.expanded_search_queries[:2]:
            for item in crossref.works(query, rows=5):
                papers.append(_paper_from_crossref(item, query, proposal_terms, proposal_text))

    return _dedupe_papers(papers)[:max_papers]


def local_cached_publications(records: list[CanonicalPIRecord], topics: ProposalTopics, proposal_text: str = "") -> list[Paper]:
    papers: list[Paper] = []
    query = " ".join(topics.expanded_search_queries)
    proposal_terms = set(topics.keywords + topics.methods)
    for record in records:
        summary = record.publications_summary or {}
        candidates: list[str] = []
        for value in summary.values():
            if isinstance(value, str):
                candidates.append(value)
            elif isinstance(value, list):
                candidates.extend(str(item) for item in value)
        for title in candidates:
            title = re.sub(r"\s+", " ", title).strip()
            if not title:
                continue
            relevance = paper_relevance_score(proposal_text, title) if proposal_text else lexical_similarity(query, title, proposal_terms)
            if relevance <= 0:
                continue
            papers.append(
                Paper(
                    title=title,
                    abstract="",
                    year=None,
                    authors=[{"name": record.display_name, "openalex_id": None, "institutions": [record.institution_name]}],
                    institutions=[record.institution_name],
                    source="local_cache",
                    relevance_score=relevance,
                )
            )
    return _dedupe_papers(papers)


def normalize_person_name(name: str) -> str:
    name = re.sub(r"\b(prof|professor|associate professor|assistant professor|dr|ts|ir)\b\.?", "", name or "", flags=re.I)
    name = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    return " ".join(name.split())


def name_similarity(left: str, right: str) -> float:
    left_norm = normalize_person_name(left)
    right_norm = normalize_person_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_parts = left_norm.split()
    right_parts = right_norm.split()
    left_set = set(left_parts)
    right_set = set(right_parts)
    jaccard = len(left_set & right_set) / max(1, len(left_set | right_set))
    if left_parts[-1:] == right_parts[-1:] and left_parts[:1] == right_parts[:1]:
        jaccard = max(jaccard, 0.9)
    if left_norm in right_norm or right_norm in left_norm:
        jaccard = max(jaccard, 0.88)
    return round(jaccard, 4)


def normalize_institution_name(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def institution_fit_score(record: CanonicalPIRecord, selected_institution: str | None = None) -> float:
    if not selected_institution:
        return 1.0
    selected = normalize_institution_name(selected_institution)
    if selected in {
        normalize_institution_name(record.institution_id),
        normalize_institution_name(record.institution_name),
    }:
        return 1.0
    return 0.0


def _email_domains(record: CanonicalPIRecord) -> set[str]:
    domains = set()
    for email in record.emails:
        if "@" in email:
            domains.add(email.rsplit("@", 1)[1].lower())
    return domains


def _institution_slug_terms(record: CanonicalPIRecord) -> set[str]:
    generic = {"university", "college", "school", "institute", "technology", "of", "the", "and"}
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (record.institution_name or "").lower())
        if token not in generic and len(token) >= 4
    }


def has_institution_affiliation(institutions: list[str], record: CanonicalPIRecord) -> bool:
    text = " ".join(institutions).lower()
    institution_name = normalize_institution_name(record.institution_name)
    if institution_name and institution_name in normalize_institution_name(text):
        return True
    if any(domain in text for domain in _email_domains(record)):
        return True
    return any(term in text for term in _institution_slug_terms(record))


def official_institution_profile(record: CanonicalPIRecord) -> bool:
    profile = (record.profile_url or "").strip()
    if not profile:
        return False
    host = urlparse(profile).netloc.lower()
    if any(host == domain or host.endswith(f".{domain}") for domain in _email_domains(record)):
        return True
    return any(term in host for term in _institution_slug_terms(record))


def align_papers_to_pi_records(
    papers: list[Paper],
    records: list[CanonicalPIRecord],
    min_name_score: float = 0.86,
) -> dict[str, list[tuple[Paper, AuthorMatchEvidence]]]:
    matches: dict[str, list[tuple[Paper, AuthorMatchEvidence]]] = {}
    for paper in papers:
        for author in paper.authors:
            author_name = author.get("name") or ""
            institutions = author.get("institutions") or []
            for record in records:
                if not has_institution_affiliation(institutions, record):
                    continue
                score = name_similarity(author_name, record.display_name)
                if score < min_name_score or not official_institution_profile(record):
                    continue
                evidence = AuthorMatchEvidence(
                    paper_title=paper.title,
                    author_name=author_name,
                    pi_name=record.display_name,
                    name_score=score,
                    author_institutions=institutions[:4],
                    source_id=paper.openalex_id or "",
                    doi=paper.doi,
                )
                matches.setdefault(record.person_id, []).append((paper, evidence))
    return matches


def profile_topic_score(proposal_text: str, record: CanonicalPIRecord) -> float:
    parts = [
        record.department or "",
        " ".join(record.research_areas),
    ]
    return semantic_similarity(proposal_text, " ".join(parts))


def semantic_fallback_score(
    proposal_text: str,
    record: CanonicalPIRecord,
    official_publication_text: list[str] | str | None = None,
) -> float:
    return semantic_similarity(proposal_text, record_text(record, official_publication_text))


def paper_backtrace_score(papers: list[Paper], evidences: list[AuthorMatchEvidence]) -> float:
    if not papers:
        return 0.0
    best = max(paper.relevance_score for paper in papers)
    volume = min(0.15, 0.03 * len(papers)) * best
    author_quality = min(0.1, 0.03 * sum(ev.name_score for ev in evidences)) * best
    return round(min(1.0, best + volume + author_quality), 4)


def research_fit_score(paper_score: float, semantic_score: float, profile_score: float = 0.0) -> float:
    return round(min(1.0, max(paper_score, semantic_score, profile_score)), 4)


def overall_fit_score(institution_score: float, research_score: float) -> float:
    if institution_score <= 0:
        return 0.0
    return round(research_score, 4)


def topic_clusters(proposal_text: str, papers: list[Paper]) -> list[str]:
    paper_text = " ".join(f"{paper.title} {paper.abstract}" for paper in papers)
    return shared_terms(proposal_text, paper_text, limit=5)


def final_score(
    backtrace_score: float,
    profile_score: float,
    semantic_score: float | None = None,
) -> float:
    research_score = research_fit_score(backtrace_score, profile_score if semantic_score is None else semantic_score, profile_score)
    return overall_fit_score(1.0, research_score)


def recommended_action(result: PaperBacktraceResult) -> str:
    verdict = result.verdict
    if result.institution_fit_score <= 0:
        return "Exclude; outside selected institution PI pool."
    if result.research_fit_score <= 0:
        return "Do not promote; no paper, semantic, or profile research-fit evidence."
    if not verdict or verdict.contact_confidence not in {"high", "medium"}:
        return "Review contact/current affiliation before outreach."
    if result.research_fit_score >= 0.45:
        return "Prioritize for manual research-fit review and possible outreach."
    if result.paper_backtrace_score > 0:
        return "Keep as plausible research-fit candidate; inspect matched papers manually."
    return "Keep as semantic/profile research-fit candidate; no paper-backtrace evidence found."


def build_reason(result: PaperBacktraceResult) -> str:
    titles = [paper.title for paper in result.matched_papers[:3]]
    author_bits = [
        f"{ev.author_name} matched {ev.pi_name} ({ev.name_score:.2f})"
        for ev in result.matched_author_evidence[:3]
    ]
    return "; ".join(
        part
        for part in [
            f"Paper backtrace matched {len(result.matched_papers)} institution-affiliated paper(s)"
            if result.matched_papers
            else "No institution-aligned paper-backtrace evidence; scored from semantic/profile fallback",
            f"papers: {' | '.join(titles)}" if titles else "",
            f"author evidence: {' | '.join(author_bits)}" if author_bits else "",
        ]
        if part
    )


def _risk_flags(record: CanonicalPIRecord, verdict: PIContactVerdict | None, result: PaperBacktraceResult) -> list[str]:
    flags = []
    if not verdict:
        flags.append("missing_contact_verdict")
    else:
        if verdict.contact_confidence not in {"high", "medium"}:
            flags.append("low_contact_confidence")
        if verdict.current_affiliation_confidence == "low":
            flags.append("current_affiliation_risk")
    if not result.matched_papers:
        flags.append("no_backtraced_paper")
    if not official_institution_profile(record):
        flags.append("missing_official_profile_url")
    return flags


def run_paper_backtrace_match(
    applicant_path: str | Path,
    storage: PIIndexStorage,
    out_path: str | Path,
    target_pi_name: str | None = None,
    top_k: int = 30,
    institution: str | None = None,
) -> list[PaperBacktraceResult]:
    proposal_text = research_intent_text(load_applicant_text(applicant_path))
    topics = extract_proposal_topics(proposal_text)
    records = [
        record
        for record in storage.iter_pi_records()
        if institution_fit_score(record, institution) > 0
    ]
    if target_pi_name:
        target_norm = normalize_person_name(target_pi_name)
        records = [record for record in records if target_norm in normalize_person_name(record.display_name)]
    publication_text = storage.publication_text_by_person(record.person_id for record in records)
    records = [
        record
        for record in records
        if has_research_evidence(record, publication_text.get(record.person_id))
    ]
    if not records:
        write_paper_backtrace_csv([], out_path)
        return []
    verdicts = storage.get_contact_verdict_records()
    institution_name = institution or records[0].institution_name
    papers = retrieve_papers(topics, institution_name=institution_name, proposal_text=proposal_text)
    papers = _dedupe_papers([*papers, *local_cached_publications(records, topics, proposal_text=proposal_text)])
    paper_matches = align_papers_to_pi_records(papers, records)

    results: list[PaperBacktraceResult] = []
    for record in records:
        matched_pairs = paper_matches.get(record.person_id, [])
        matched_papers = _dedupe_papers(pair[0] for pair in matched_pairs)
        evidences = [pair[1] for pair in matched_pairs]
        verdict = verdicts.get(record.person_id)
        backtrace = paper_backtrace_score(matched_papers, evidences)
        profile_score = profile_topic_score(proposal_text, record)
        semantic_score = max(
            semantic_fallback_score(
                proposal_text,
                record,
                publication_text.get(record.person_id),
            ),
            profile_score,
        )
        institution_score = institution_fit_score(record, institution)
        research_score = research_fit_score(backtrace, semantic_score, profile_score)
        overall_score = overall_fit_score(institution_score, research_score)
        result = PaperBacktraceResult(
            record=record,
            verdict=verdict,
            paper_backtrace_score=backtrace,
            matched_papers=matched_papers,
            matched_author_evidence=evidences,
            publication_topic_clusters=topic_clusters(proposal_text, matched_papers),
            profile_topic_score=profile_score,
            institution_fit_score=institution_score,
            semantic_fallback_score=semantic_score,
            research_fit_score=research_score,
            overall_score=overall_score,
            final_research_fit_score=research_score,
        )
        result.reason_for_match = build_reason(result)
        result.risk_flags = _risk_flags(record, verdict, result)
        result.recommended_action = recommended_action(result)
        results.append(result)

    results = sorted(
        results,
        key=lambda item: (item.overall_score, item.research_fit_score),
        reverse=True,
    )[:top_k]
    write_paper_backtrace_csv(results, out_path)
    return results


def _email(record: CanonicalPIRecord) -> str:
    return record.emails[0] if record.emails else ""


def _papers_json(papers: list[Paper]) -> str:
    payload = [
        {
            "title": paper.title,
            "year": paper.year,
            "openalex_id": paper.openalex_id,
            "doi": paper.doi,
            "relevance_score": paper.relevance_score,
        }
        for paper in papers
    ]
    return json.dumps(payload, ensure_ascii=False)


def write_paper_backtrace_csv(results: list[PaperBacktraceResult], out_path: str | Path) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "pi_name",
        "title",
        "department",
        "email",
        "profile_url",
        "contact_confidence",
        "institution_fit_score",
        "research_fit_score",
        "overall_score",
        "paper_backtrace_score",
        "semantic_fallback_score",
        "profile_topic_score",
        "final_research_fit_score",
        "matched_papers",
        "reason_for_match",
        "risk_flags",
        "recommended_action",
        "matched_paper_titles",
        "matched_author_evidence",
        "publication_topic_clusters",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            verdict = result.verdict
            writer.writerow(
                {
                    "pi_name": result.record.display_name,
                    "title": result.record.title or "",
                    "department": result.record.department or "",
                    "email": _email(result.record),
                    "profile_url": result.record.profile_url or "",
                    "contact_confidence": verdict.contact_confidence if verdict else "none",
                    "institution_fit_score": result.institution_fit_score,
                    "research_fit_score": result.research_fit_score,
                    "overall_score": result.overall_score,
                    "paper_backtrace_score": result.paper_backtrace_score,
                    "semantic_fallback_score": result.semantic_fallback_score,
                    "profile_topic_score": result.profile_topic_score,
                    "final_research_fit_score": result.final_research_fit_score,
                    "matched_papers": _papers_json(result.matched_papers),
                    "reason_for_match": result.reason_for_match,
                    "risk_flags": "; ".join(result.risk_flags),
                    "recommended_action": result.recommended_action,
                    "matched_paper_titles": " | ".join(paper.title for paper in result.matched_papers[:5]),
                    "matched_author_evidence": json.dumps([ev.__dict__ for ev in result.matched_author_evidence], ensure_ascii=False),
                    "publication_topic_clusters": "; ".join(result.publication_topic_clusters),
                }
            )
