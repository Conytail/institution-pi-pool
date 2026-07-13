from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import time
from typing import Any, Iterable

import numpy as np
from scipy.sparse import csr_matrix

from .research_profile_experiment import (
    CachedOpenAlex,
    SparseVector,
    _write_csv,
    cosine,
    encode_experiment_texts,
    ndcg_at_k,
    normalize_vector,
    semantic_tokens,
    utc_now_iso,
    work_text,
)


AUTHOR_POSITIONS = {"first", "middle", "last"}
FIRST_WEIGHTS = (0.75, 1.0, 1.25, 1.5)
LAST_WEIGHTS = (0.75, 1.0, 1.25, 1.5)
CORRESPONDING_WEIGHTS = (1.0, 1.25, 1.5, 2.0)
SOLO_WEIGHTS = (1.0, 1.25, 1.5)
MIDDLE_MEDIUM_WEIGHTS = (0.75, 1.0)
MIDDLE_LARGE_WEIGHTS = (0.5, 0.75, 1.0)


def _openalex_short_id(value: str | None) -> str:
    return (value or "").rstrip("/").split("/")[-1]


def author_count_band(author_count: int) -> str:
    if author_count <= 1:
        return "1"
    if author_count <= 4:
        return "2-4"
    if author_count <= 10:
        return "5-10"
    return "11+"


def author_role(work: dict[str, Any]) -> str:
    if int(work.get("author_count") or 0) == 1:
        return "solo"
    position = work.get("author_position") or "unknown"
    return position if position in AUTHOR_POSITIONS else "unknown"


def publication_age_band(year: int, reference_year: int) -> str:
    age = max(0, reference_year - year)
    if age <= 2:
        return "0-3"
    if age <= 7:
        return "4-8"
    return "9+"


def normalize_work_type(raw_type: str | None, source_type: str | None = None) -> str:
    raw = (raw_type or "").lower()
    source = (source_type or "").lower()
    if raw == "preprint" or source == "repository":
        return "preprint"
    if source in {"conference", "proceedings"}:
        return "conference"
    if raw == "article" or source == "journal":
        return "journal"
    return "other" if raw or source else "unknown"


def _load_raw_author_works(cache_dir: Path, author_id: str) -> dict[str, dict[str, Any]]:
    candidates = sorted(cache_dir.glob(f"works_{author_id}_*.json"))
    if not candidates:
        return {}
    payload = json.loads(candidates[-1].read_text(encoding="utf-8"))
    return {
        raw["id"]: raw
        for raw in payload.get("results") or []
        if raw.get("id")
    }


def _target_authorship(raw_work: dict[str, Any], author_id: str) -> dict[str, Any] | None:
    return next(
        (
            authorship
            for authorship in raw_work.get("authorships") or []
            if _openalex_short_id((authorship.get("author") or {}).get("id")) == author_id
        ),
        None,
    )


def _work_type_map_from_payload(payload: dict[str, Any]) -> dict[str, tuple[str | None, str | None]]:
    output: dict[str, tuple[str | None, str | None]] = {}
    for work in payload.get("results") or []:
        source = ((work.get("primary_location") or {}).get("source") or {})
        if work.get("id"):
            output[work["id"]] = (work.get("type"), source.get("type"))
    return output


def fetch_work_types(
    client: CachedOpenAlex,
    author_id: str,
    start_year: int,
    max_works: int,
) -> dict[str, tuple[str | None, str | None]]:
    payload = client.get(
        "works",
        {
            "filter": f"author.id:{author_id},from_publication_date:{start_year}-01-01",
            "per_page": min(100, max_works),
            "sort": "publication_date:desc",
            "select": "id,type,primary_location",
        },
        f"authorship_work_types_{author_id}_{start_year}_{max_works}",
    )
    return _work_type_map_from_payload(payload)


def enrich_pi_works(
    pi: dict[str, Any],
    raw_by_id: dict[str, dict[str, Any]],
    type_by_id: dict[str, tuple[str | None, str | None]] | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    author_id = _openalex_short_id(pi.get("openalex_author_id"))
    coverage = Counter()
    enriched_works: list[dict[str, Any]] = []
    for work in pi.get("works") or []:
        raw = raw_by_id.get(work.get("id")) or {}
        authorships = raw.get("authorships") or []
        target = _target_authorship(raw, author_id)
        position = (target or {}).get("author_position") or "unknown"
        if position not in AUTHOR_POSITIONS:
            position = "unknown"
        corresponding = "true" if (target or {}).get("is_corresponding") is True else "unknown"
        topics = raw.get("topics") or []
        primary_topic = topics[0] if topics else {}
        field = primary_topic.get("field") or {}
        domain = primary_topic.get("domain") or {}
        raw_type, source_type = (type_by_id or {}).get(
            work.get("id"),
            (raw.get("type"), ((raw.get("primary_location") or {}).get("source") or {}).get("type")),
        )
        enriched = {
            **work,
            "author_position": position,
            "corresponding_confirmed": corresponding,
            "author_count": len(authorships),
            "primary_openalex_field_id": field.get("id"),
            "primary_openalex_field_name": field.get("display_name") or "Unknown",
            "primary_openalex_domain_id": domain.get("id"),
            "primary_openalex_domain_name": domain.get("display_name") or "Unknown",
            "work_type": normalize_work_type(raw_type, source_type),
        }
        enriched["author_role"] = author_role(enriched)
        enriched["author_count_band"] = author_count_band(enriched["author_count"])
        enriched_works.append(enriched)
        coverage["works"] += 1
        coverage[f"position_{position}"] += 1
        coverage[f"corresponding_{corresponding}"] += 1
        coverage[f"work_type_{enriched['work_type']}"] += 1
        if field.get("id"):
            coverage["field_known"] += 1
    return {**pi, "works": enriched_works}, dict(coverage)


def prepare_authorship_dataset(args: argparse.Namespace) -> dict[str, Any]:
    source_path = Path(args.dataset)
    source = json.loads(source_path.read_text(encoding="utf-8"))
    cache_dir = Path(args.openalex_cache)
    client = CachedOpenAlex(cache_dir) if args.refresh_work_types else None
    output_pis: list[dict[str, Any]] = []
    aggregate_coverage = Counter()
    refreshed_authors = 0
    for pi in source.get("pis") or []:
        author_id = _openalex_short_id(pi.get("openalex_author_id"))
        raw_by_id = _load_raw_author_works(cache_dir, author_id)
        type_by_id: dict[str, tuple[str | None, str | None]] = {}
        if client is not None and (
            client.remaining_credits is None or client.remaining_credits > args.reserve_credits
        ):
            type_by_id = fetch_work_types(client, author_id, args.start_year, args.max_works)
            refreshed_authors += 1
        enriched, coverage = enrich_pi_works(pi, raw_by_id, type_by_id)
        output_pis.append(enriched)
        aggregate_coverage.update(coverage)

    output = {
        **{key: value for key, value in source.items() if key != "pis"},
        "generated_at": utc_now_iso(),
        "source_dataset": str(source_path.resolve()),
        "authorship_schema_version": 1,
        "work_type_authors_refreshed": refreshed_authors,
        "remaining_openalex_credits": client.remaining_credits if client else None,
        "authorship_coverage": dict(aggregate_coverage),
        "pis": output_pis,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "output": str(output_path.resolve()),
        "pi_count": len(output_pis),
        "work_count": aggregate_coverage["works"],
        "authorship_coverage": dict(aggregate_coverage),
        "work_type_authors_refreshed": refreshed_authors,
        "remaining_openalex_credits": client.remaining_credits if client else None,
    }


def _dedupe_works(works: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for work in works:
        work_id = work.get("id")
        if not work_id or work_id in seen:
            continue
        if len(semantic_tokens(work_text(work))) < 6:
            continue
        seen.add(work_id)
        output.append(work)
    output.sort(key=lambda work: (int(work.get("year") or 0), work.get("id") or ""), reverse=True)
    return output


def _balance_cell(work: dict[str, Any]) -> tuple[str, str, str]:
    return (
        author_role(work),
        work.get("corresponding_confirmed") or "unknown",
        author_count_band(int(work.get("author_count") or 0)),
    )


def build_authorship_cases(
    pis: list[dict[str, Any]],
    max_proposals: int = 3,
    cv_work_count: int = 2,
    min_profile_works: int = 8,
    seed: int = 20260712,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], set[str]]:
    eligible_by_pi = {pi["person_id"]: _dedupe_works(pi.get("works") or []) for pi in pis}
    capacity = {
        pi_id: max(0, min(max_proposals, len(works) - cv_work_count - min_profile_works))
        for pi_id, works in eligible_by_pi.items()
    }
    selected: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    selected_work_ids: set[str] = set()
    cell_counts: Counter[tuple[str, str, str]] = Counter()
    rng = random.Random(seed)
    pi_order = sorted(eligible_by_pi)
    rng.shuffle(pi_order)
    for round_index in range(max_proposals):
        round_order = pi_order[round_index:] + pi_order[:round_index]
        for pi_id in round_order:
            if len(selected[pi_id]) >= capacity[pi_id]:
                continue
            used_roles = Counter(author_role(work) for work in selected[pi_id])
            candidates = [
                work
                for work in eligible_by_pi[pi_id]
                if work["id"] not in selected_work_ids
            ]
            if not candidates:
                continue
            chosen = min(
                candidates,
                key=lambda work: (
                    cell_counts[_balance_cell(work)],
                    used_roles[author_role(work)],
                    -int(work.get("year") or 0),
                    work.get("id") or "",
                ),
            )
            selected[pi_id].append(chosen)
            selected_work_ids.add(chosen["id"])
            cell_counts[_balance_cell(chosen)] += 1

    cv_by_pi: dict[str, list[dict[str, Any]]] = {}
    heldout_ids = set(selected_work_ids)
    for pi_id in pi_order:
        if not selected[pi_id]:
            continue
        available = [
            work
            for work in eligible_by_pi[pi_id]
            if work["id"] not in selected_work_ids
        ]
        unique = [work for work in available if work["id"] not in heldout_ids]
        fallback = [work for work in available if work["id"] in heldout_ids]
        cv_works = (unique + fallback)[:cv_work_count]
        if len(cv_works) < cv_work_count:
            continue
        cv_by_pi[pi_id] = cv_works
        heldout_ids.update(work["id"] for work in cv_works)

    profiles: dict[str, list[dict[str, Any]]] = {}
    for pi_id, works in eligible_by_pi.items():
        training = [work for work in works if work["id"] not in heldout_ids]
        if len(training) >= min_profile_works:
            profiles[pi_id] = training

    pi_by_id = {pi["person_id"]: pi for pi in pis}
    author_to_pi = {
        _openalex_short_id(pi.get("openalex_author_id")): pi["person_id"]
        for pi in pis
        if pi["person_id"] in profiles
    }
    reference_year = max(
        (int(work.get("year") or 0) for works in eligible_by_pi.values() for work in works),
        default=0,
    )
    cases: list[dict[str, Any]] = []
    for pi_id, proposals in selected.items():
        if pi_id not in profiles or pi_id not in cv_by_pi:
            continue
        pi = pi_by_id[pi_id]
        cv_text = " ".join(work_text(work) for work in cv_by_pi[pi_id])
        for proposal in proposals:
            positives = sorted(
                {
                    author_to_pi[author_id]
                    for author_id in proposal.get("author_ids") or []
                    if author_id in author_to_pi
                    and pi_by_id[author_to_pi[author_id]]["institution_id"] == pi["institution_id"]
                }
            )
            if pi_id not in positives:
                positives.append(pi_id)
            cases.append(
                {
                    "case_id": f"{pi_id}::{_openalex_short_id(proposal['id'])}",
                    "source_pi_id": pi_id,
                    "institution_id": pi["institution_id"],
                    "institution_name": pi["institution_name"],
                    "proposal_work_id": proposal["id"],
                    "proposal_text": work_text(proposal),
                    "cv_text": cv_text,
                    "positive_pi_ids": positives,
                    "author_role": author_role(proposal),
                    "author_position": proposal.get("author_position") or "unknown",
                    "corresponding_confirmed": proposal.get("corresponding_confirmed") or "unknown",
                    "author_count": int(proposal.get("author_count") or 0),
                    "author_count_band": author_count_band(int(proposal.get("author_count") or 0)),
                    "work_type": proposal.get("work_type") or "unknown",
                    "publication_age_band": publication_age_band(
                        int(proposal.get("year") or 0), reference_year
                    ),
                    "primary_openalex_field_id": proposal.get("primary_openalex_field_id"),
                    "primary_openalex_field_name": proposal.get("primary_openalex_field_name") or "Unknown",
                    "primary_openalex_domain_id": proposal.get("primary_openalex_domain_id"),
                    "primary_openalex_domain_name": proposal.get("primary_openalex_domain_name") or "Unknown",
                }
            )
    cases.sort(key=lambda case: case["case_id"])
    return cases, profiles, heldout_ids


@dataclass(frozen=True)
class AuthorshipWeightConfig:
    scheme: str
    first_weight: float = 1.0
    last_weight: float = 1.0
    corresponding_weight: float = 1.0
    solo_weight: float = 1.0
    middle_medium_weight: float = 1.0
    middle_large_weight: float = 1.0

    @property
    def config_id(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    @property
    def deviation(self) -> float:
        return sum(abs(value - 1.0) for key, value in asdict(self).items() if key != "scheme")


def generate_weight_configs() -> list[AuthorshipWeightConfig]:
    equal = AuthorshipWeightConfig("equal")
    configs = [equal]
    for first in FIRST_WEIGHTS:
        for last in LAST_WEIGHTS:
            for corresponding in CORRESPONDING_WEIGHTS:
                for solo in SOLO_WEIGHTS:
                    for middle_medium in MIDDLE_MEDIUM_WEIGHTS:
                        for middle_large in MIDDLE_LARGE_WEIGHTS:
                            config = AuthorshipWeightConfig(
                                "role_aware",
                                first,
                                last,
                                corresponding,
                                solo,
                                middle_medium,
                                middle_large,
                            )
                            if config.deviation > 0:
                                configs.append(config)
    return configs


def publication_weight(work: dict[str, Any], config: AuthorshipWeightConfig) -> float:
    role = author_role(work)
    if role == "solo":
        weight = config.solo_weight
    elif role == "first":
        weight = config.first_weight
    elif role == "last":
        weight = config.last_weight
    else:
        weight = 1.0
    if work.get("corresponding_confirmed") == "true":
        weight *= config.corresponding_weight
    elif role == "middle":
        band = author_count_band(int(work.get("author_count") or 0))
        if band == "5-10":
            weight *= config.middle_medium_weight
        elif band == "11+":
            weight *= config.middle_large_weight
    return max(0.5, min(2.5, weight))


@dataclass
class ProfileGroup:
    field_id: str
    author_role: str
    corresponding_confirmed: str
    author_count_band: str
    author_count: int
    vector_sum: SparseVector
    work_count: int


def build_profile_groups(
    profile_works: dict[str, list[dict[str, Any]]],
    vectors_by_work: dict[str, SparseVector],
    feature_limit: int = 256,
    group_by_field: bool = False,
) -> dict[str, list[ProfileGroup]]:
    output: dict[str, list[ProfileGroup]] = {}
    for pi_id, works in profile_works.items():
        grouped: dict[tuple[str, str, str, str], tuple[defaultdict[str, float], int, int]] = {}
        for work in works:
            vector = normalize_vector(vectors_by_work.get(work["id"], {}), feature_limit)
            if not vector:
                continue
            field_id = (
                work.get("primary_openalex_field_id") or "unknown"
                if group_by_field
                else "*"
            )
            role = author_role(work)
            corresponding = work.get("corresponding_confirmed") or "unknown"
            band = author_count_band(int(work.get("author_count") or 0))
            key = (field_id, role, corresponding, band)
            if key not in grouped:
                grouped[key] = (defaultdict(float), 0, int(work.get("author_count") or 0))
            vector_sum, count, author_count = grouped[key]
            for term, value in vector.items():
                vector_sum[term] += value
            grouped[key] = (vector_sum, count + 1, max(author_count, int(work.get("author_count") or 0)))
        output[pi_id] = [
            ProfileGroup(
                field_id=key[0],
                author_role=key[1],
                corresponding_confirmed=key[2],
                author_count_band=key[3],
                author_count=author_count,
                vector_sum=dict(vector_sum),
                work_count=count,
            )
            for key, (vector_sum, count, author_count) in grouped.items()
        ]
    return output


@dataclass
class NumericProfileBasis:
    groups: list[ProfileGroup]
    terms: list[str]
    matrix: np.ndarray


@dataclass
class InstitutionMatrixContext:
    candidates: list[str]
    term_indexes: dict[str, int]
    case_indexes: dict[str, int]
    proposal_matrix: csr_matrix
    cv_matrix: csr_matrix


def build_evaluation_matrix_context(
    cases: list[dict[str, Any]],
    numeric_bases: dict[str, NumericProfileBasis],
    query_vectors: dict[str, tuple[SparseVector, SparseVector]],
    institution_candidates: dict[str, list[str]],
) -> dict[str, InstitutionMatrixContext]:
    cases_by_institution: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        cases_by_institution[case["institution_id"]].append(case)
    output: dict[str, InstitutionMatrixContext] = {}
    for institution_id, institution_cases in cases_by_institution.items():
        candidates = institution_candidates.get(institution_id, [])
        terms = sorted(
            {
                term
                for pi_id in candidates
                for term in numeric_bases[pi_id].terms
            }
            | {
                term
                for case in institution_cases
                for vector in query_vectors[case["case_id"]]
                for term in vector
            }
        )
        term_indexes = {term: index for index, term in enumerate(terms)}
        case_indexes = {
            case["case_id"]: index for index, case in enumerate(institution_cases)
        }

        def query_matrix(vector_index: int) -> csr_matrix:
            rows: list[int] = []
            columns: list[int] = []
            values: list[float] = []
            for row_index, case in enumerate(institution_cases):
                vector = query_vectors[case["case_id"]][vector_index]
                for term, value in vector.items():
                    column = term_indexes.get(term)
                    if column is not None:
                        rows.append(row_index)
                        columns.append(column)
                        values.append(value)
            return csr_matrix(
                (values, (rows, columns)),
                shape=(len(institution_cases), len(terms)),
                dtype=np.float64,
            )

        output[institution_id] = InstitutionMatrixContext(
            candidates=candidates,
            term_indexes=term_indexes,
            case_indexes=case_indexes,
            proposal_matrix=query_matrix(0),
            cv_matrix=query_matrix(1),
        )
    return output


def evaluate_profiles_matrix(
    cases: list[dict[str, Any]],
    profiles: dict[str, SparseVector],
    context: dict[str, InstitutionMatrixContext],
    config_id: str,
    encoder: str,
    scheme: str,
) -> list[dict[str, Any]]:
    cases_by_institution: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        cases_by_institution[case["institution_id"]].append(case)
    rows_by_case: dict[str, dict[str, Any]] = {}
    for institution_id, institution_cases in cases_by_institution.items():
        matrix_context = context[institution_id]
        profile_rows: list[int] = []
        profile_columns: list[int] = []
        profile_values: list[float] = []
        for row_index, pi_id in enumerate(matrix_context.candidates):
            for term, value in profiles[pi_id].items():
                column = matrix_context.term_indexes.get(term)
                if column is not None:
                    profile_rows.append(row_index)
                    profile_columns.append(column)
                    profile_values.append(value)
        profile_matrix = csr_matrix(
            (profile_values, (profile_rows, profile_columns)),
            shape=(len(matrix_context.candidates), len(matrix_context.term_indexes)),
            dtype=np.float64,
        )
        row_indexes = [matrix_context.case_indexes[case["case_id"]] for case in institution_cases]
        score_matrix = (
            0.8 * matrix_context.proposal_matrix[row_indexes].dot(profile_matrix.T)
            + 0.2 * matrix_context.cv_matrix[row_indexes].dot(profile_matrix.T)
        ).toarray()
        for case_index, case in enumerate(institution_cases):
            scored = sorted(
                zip(matrix_context.candidates, score_matrix[case_index]),
                key=lambda item: (float(item[1]), item[0]),
                reverse=True,
            )
            ranked = [pi_id for pi_id, _score in scored]
            source_rank = ranked.index(case["source_pi_id"]) + 1
            positives = set(case["positive_pi_ids"])
            rows_by_case[case["case_id"]] = {
                "config_id": config_id,
                "encoder": encoder,
                "scheme": scheme,
                "case_id": case["case_id"],
                "source_pi_id": case["source_pi_id"],
                "institution_id": case["institution_id"],
                "institution_name": case["institution_name"],
                "source_pi_rank": source_rank,
                "source_pi_reciprocal_rank": 1.0 / source_rank,
                "source_pi_ndcg_at_10": source_ndcg(source_rank),
                "multi_positive_ndcg_at_10": ndcg_at_k(ranked, positives, 10),
                "candidate_count": len(ranked),
                "author_role": case["author_role"],
                "author_position": case["author_position"],
                "corresponding_confirmed": case["corresponding_confirmed"],
                "author_count_band": case["author_count_band"],
                "work_type": case["work_type"],
                "publication_age_band": case["publication_age_band"],
                "primary_openalex_field_id": case.get("primary_openalex_field_id") or "unknown",
                "primary_openalex_field_name": case.get("primary_openalex_field_name") or "Unknown",
                "primary_openalex_domain_name": case.get("primary_openalex_domain_name") or "Unknown",
            }
    return [rows_by_case[case["case_id"]] for case in cases if case["case_id"] in rows_by_case]


def build_numeric_profile_bases(
    groups_by_pi: dict[str, list[ProfileGroup]],
) -> dict[str, NumericProfileBasis]:
    output: dict[str, NumericProfileBasis] = {}
    for pi_id, groups in groups_by_pi.items():
        terms = sorted({term for group in groups for term in group.vector_sum})
        term_indexes = {term: index for index, term in enumerate(terms)}
        matrix = np.zeros((len(groups), len(terms)), dtype=np.float64)
        for group_index, group in enumerate(groups):
            for term, value in group.vector_sum.items():
                matrix[group_index, term_indexes[term]] = value
        output[pi_id] = NumericProfileBasis(groups=groups, terms=terms, matrix=matrix)
    return output


def weighted_numeric_career_vector(
    basis: NumericProfileBasis,
    global_config: AuthorshipWeightConfig,
    field_overrides: dict[str, AuthorshipWeightConfig] | None = None,
    feature_limit: int = 256,
) -> SparseVector:
    if not basis.groups or not basis.terms:
        return {}
    weights = np.asarray(
        [
            publication_weight(
                {
                    "author_role": group.author_role,
                    "author_position": group.author_role,
                    "corresponding_confirmed": group.corresponding_confirmed,
                    "author_count": group.author_count,
                },
                (field_overrides or {}).get(group.field_id, global_config),
            )
            for group in basis.groups
        ],
        dtype=np.float64,
    )
    scores = weights @ basis.matrix
    nonzero = np.flatnonzero(scores)
    if nonzero.size == 0:
        return {}
    keep_count = min(feature_limit, nonzero.size)
    if keep_count < nonzero.size:
        local = np.argpartition(np.abs(scores[nonzero]), -keep_count)[-keep_count:]
        selected = nonzero[local]
    else:
        selected = nonzero
    selected = sorted(
        (int(index) for index in selected),
        key=lambda index: (-abs(float(scores[index])), basis.terms[index]),
    )
    norm = math.sqrt(sum(float(scores[index]) ** 2 for index in selected))
    if norm <= 0:
        return {}
    return {
        basis.terms[index]: float(scores[index]) / norm
        for index in selected
    }


def weighted_career_vector(
    groups: list[ProfileGroup],
    global_config: AuthorshipWeightConfig,
    field_overrides: dict[str, AuthorshipWeightConfig] | None = None,
    feature_limit: int = 256,
) -> SparseVector:
    combined: defaultdict[str, float] = defaultdict(float)
    total_weight = 0.0
    for group in groups:
        config = (field_overrides or {}).get(group.field_id, global_config)
        proxy = {
            "author_role": group.author_role,
            "author_position": group.author_role,
            "corresponding_confirmed": group.corresponding_confirmed,
            "author_count": group.author_count,
        }
        weight = publication_weight(proxy, config)
        total_weight += weight * group.work_count
        for term, value in group.vector_sum.items():
            combined[term] += weight * value
    if total_weight <= 0:
        return {}
    return normalize_vector(
        {term: value / total_weight for term, value in combined.items()},
        feature_limit,
    )


def source_ndcg(rank: int, k: int = 10) -> float:
    if rank < 1 or rank > k:
        return 0.0
    return 1.0 / math.log2(rank + 1)


def summarize_authorship_rows(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {
            "cases": 0,
            "source_pi_ndcg_at_10": 0.0,
            "source_pi_mrr": 0.0,
            "source_pi_hit_at_1": 0.0,
            "source_pi_hit_at_3": 0.0,
            "source_pi_hit_at_5": 0.0,
            "source_pi_hit_at_10": 0.0,
            "multi_positive_ndcg_at_10": 0.0,
            "mean_source_rank": 0.0,
            "source_pi_ndcg_se": 0.0,
            "source_pi_count": 0,
        }
    by_source: defaultdict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_source[row["source_pi_id"]].append(row["source_pi_ndcg_at_10"])
    source_means = [statistics.fmean(values) for values in by_source.values()]
    return {
        "cases": len(rows),
        "source_pi_ndcg_at_10": statistics.fmean(row["source_pi_ndcg_at_10"] for row in rows),
        "source_pi_mrr": statistics.fmean(row["source_pi_reciprocal_rank"] for row in rows),
        "source_pi_hit_at_1": statistics.fmean(row["source_pi_rank"] <= 1 for row in rows),
        "source_pi_hit_at_3": statistics.fmean(row["source_pi_rank"] <= 3 for row in rows),
        "source_pi_hit_at_5": statistics.fmean(row["source_pi_rank"] <= 5 for row in rows),
        "source_pi_hit_at_10": statistics.fmean(row["source_pi_rank"] <= 10 for row in rows),
        "multi_positive_ndcg_at_10": statistics.fmean(
            row["multi_positive_ndcg_at_10"] for row in rows
        ),
        "mean_source_rank": statistics.fmean(row["source_pi_rank"] for row in rows),
        "source_pi_ndcg_se": (
            statistics.stdev(source_means) / math.sqrt(len(source_means))
            if len(source_means) > 1
            else 0.0
        ),
        "source_pi_count": len(source_means),
    }


def evaluate_profiles_reference(
    cases: list[dict[str, Any]],
    profiles: dict[str, SparseVector],
    query_vectors: dict[str, tuple[SparseVector, SparseVector]],
    institution_candidates: dict[str, list[str]],
    config_id: str,
    encoder: str,
    scheme: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        candidates = institution_candidates.get(case["institution_id"], [])
        if case["source_pi_id"] not in candidates:
            continue
        proposal_vector, cv_vector = query_vectors[case["case_id"]]
        scored = sorted(
            (
                (
                    pi_id,
                    0.8 * cosine(proposal_vector, profiles[pi_id])
                    + 0.2 * cosine(cv_vector, profiles[pi_id]),
                )
                for pi_id in candidates
            ),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )
        ranked = [pi_id for pi_id, _score in scored]
        source_rank = ranked.index(case["source_pi_id"]) + 1
        positives = set(case["positive_pi_ids"])
        rows.append(
            {
                "config_id": config_id,
                "encoder": encoder,
                "scheme": scheme,
                "case_id": case["case_id"],
                "source_pi_id": case["source_pi_id"],
                "institution_id": case["institution_id"],
                "institution_name": case["institution_name"],
                "source_pi_rank": source_rank,
                "source_pi_reciprocal_rank": 1.0 / source_rank,
                "source_pi_ndcg_at_10": source_ndcg(source_rank),
                "multi_positive_ndcg_at_10": ndcg_at_k(ranked, positives, 10),
                "candidate_count": len(ranked),
                "author_role": case["author_role"],
                "author_position": case["author_position"],
                "corresponding_confirmed": case["corresponding_confirmed"],
                "author_count_band": case["author_count_band"],
                "work_type": case["work_type"],
                "publication_age_band": case["publication_age_band"],
                "primary_openalex_field_id": case.get("primary_openalex_field_id") or "unknown",
                "primary_openalex_field_name": case.get("primary_openalex_field_name") or "Unknown",
                "primary_openalex_domain_name": case.get("primary_openalex_domain_name") or "Unknown",
            }
        )
    return rows


def evaluate_profiles(
    cases: list[dict[str, Any]],
    profiles: dict[str, SparseVector],
    query_vectors: dict[str, tuple[SparseVector, SparseVector]],
    institution_candidates: dict[str, list[str]],
    config_id: str,
    encoder: str,
    scheme: str,
) -> list[dict[str, Any]]:
    cases_by_institution: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        cases_by_institution[case["institution_id"]].append(case)

    rows: list[dict[str, Any]] = []
    for institution_id, institution_cases in cases_by_institution.items():
        candidates = institution_candidates.get(institution_id, [])
        candidate_index = {pi_id: index for index, pi_id in enumerate(candidates)}
        inverted: defaultdict[str, list[tuple[int, float]]] = defaultdict(list)
        for pi_index, pi_id in enumerate(candidates):
            for term, value in profiles[pi_id].items():
                inverted[term].append((pi_index, value))

        for case in institution_cases:
            if case["source_pi_id"] not in candidate_index:
                continue
            proposal_vector, cv_vector = query_vectors[case["case_id"]]
            scores = [0.0] * len(candidates)
            for term, query_value in proposal_vector.items():
                for pi_index, profile_value in inverted.get(term, ()):
                    scores[pi_index] += 0.8 * query_value * profile_value
            for term, query_value in cv_vector.items():
                for pi_index, profile_value in inverted.get(term, ()):
                    scores[pi_index] += 0.2 * query_value * profile_value
            scored = sorted(
                zip(candidates, scores),
                key=lambda item: (item[1], item[0]),
                reverse=True,
            )
            ranked = [pi_id for pi_id, _score in scored]
            source_rank = ranked.index(case["source_pi_id"]) + 1
            positives = set(case["positive_pi_ids"])
            rows.append(
                {
                    "config_id": config_id,
                    "encoder": encoder,
                    "scheme": scheme,
                    "case_id": case["case_id"],
                    "source_pi_id": case["source_pi_id"],
                    "institution_id": case["institution_id"],
                    "institution_name": case["institution_name"],
                    "source_pi_rank": source_rank,
                    "source_pi_reciprocal_rank": 1.0 / source_rank,
                    "source_pi_ndcg_at_10": source_ndcg(source_rank),
                    "multi_positive_ndcg_at_10": ndcg_at_k(ranked, positives, 10),
                    "candidate_count": len(ranked),
                    "author_role": case["author_role"],
                    "author_position": case["author_position"],
                    "corresponding_confirmed": case["corresponding_confirmed"],
                    "author_count_band": case["author_count_band"],
                    "work_type": case["work_type"],
                    "publication_age_band": case["publication_age_band"],
                    "primary_openalex_field_id": case.get("primary_openalex_field_id") or "unknown",
                    "primary_openalex_field_name": case.get("primary_openalex_field_name") or "Unknown",
                    "primary_openalex_domain_name": case.get("primary_openalex_domain_name") or "Unknown",
                }
            )
    return rows


def _config_result(
    config: AuthorshipWeightConfig,
    encoder: str,
    rows: list[dict[str, Any]],
    build_ms: float,
    scoring_ms: float,
    pi_count: int,
) -> dict[str, Any]:
    return {
        "config_id": config.config_id,
        "encoder": encoder,
        **asdict(config),
        "weight_deviation": config.deviation,
        **summarize_authorship_rows(rows),
        "pi_count": pi_count,
        "build_ms_per_pi": round(build_ms / max(pi_count, 1), 4),
        "query_ms_per_case": round(scoring_ms / max(len(rows), 1), 4),
    }


def select_role_config(results: list[dict[str, Any]]) -> dict[str, Any]:
    role_results = [row for row in results if row["scheme"] == "role_aware"]
    return max(
        role_results,
        key=lambda row: (
            row["source_pi_ndcg_at_10"],
            row["source_pi_hit_at_5"],
            row["multi_positive_ndcg_at_10"],
            -row["weight_deviation"],
        ),
    )


def paired_source_bootstrap(
    selected_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    samples: int = 4000,
    seed: int = 20260712,
) -> dict[str, float]:
    selected: defaultdict[str, list[float]] = defaultdict(list)
    baseline: defaultdict[str, list[float]] = defaultdict(list)
    for row in selected_rows:
        selected[row["source_pi_id"]].append(row["source_pi_ndcg_at_10"])
    for row in baseline_rows:
        baseline[row["source_pi_id"]].append(row["source_pi_ndcg_at_10"])
    deltas = [
        statistics.fmean(selected[pi_id]) - statistics.fmean(baseline[pi_id])
        for pi_id in sorted(set(selected).intersection(baseline))
    ]
    if not deltas:
        return {"mean_delta": 0.0, "ci_low": 0.0, "ci_high": 0.0, "source_pi_count": 0}
    rng = random.Random(seed)
    boot = sorted(
        statistics.fmean(rng.choice(deltas) for _index in range(len(deltas)))
        for _sample in range(samples)
    )
    return {
        "mean_delta": statistics.fmean(deltas),
        "ci_low": boot[int(0.025 * (len(boot) - 1))],
        "ci_high": boot[int(0.975 * (len(boot) - 1))],
        "source_pi_count": len(deltas),
    }


def stratified_comparison(
    encoder: str,
    selected_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    dimensions = (
        "author_role",
        "corresponding_confirmed",
        "author_count_band",
        "work_type",
        "publication_age_band",
        "primary_openalex_field_name",
        "institution_name",
    )
    output: list[dict[str, Any]] = []
    for dimension in dimensions:
        values = sorted({str(row.get(dimension) or "Unknown") for row in baseline_rows})
        for value in values:
            selected_group = [row for row in selected_rows if str(row.get(dimension) or "Unknown") == value]
            baseline_group = [row for row in baseline_rows if str(row.get(dimension) or "Unknown") == value]
            selected_summary = summarize_authorship_rows(selected_group)
            baseline_summary = summarize_authorship_rows(baseline_group)
            output.append(
                {
                    "encoder": encoder,
                    "dimension": dimension,
                    "value": value,
                    "cases": baseline_summary["cases"],
                    "baseline_source_pi_ndcg_at_10": baseline_summary["source_pi_ndcg_at_10"],
                    "selected_source_pi_ndcg_at_10": selected_summary["source_pi_ndcg_at_10"],
                    "source_pi_ndcg_delta": (
                        selected_summary["source_pi_ndcg_at_10"]
                        - baseline_summary["source_pi_ndcg_at_10"]
                    ),
                    "baseline_source_pi_hit_at_5": baseline_summary["source_pi_hit_at_5"],
                    "selected_source_pi_hit_at_5": selected_summary["source_pi_hit_at_5"],
                    "source_pi_hit_at_5_delta": (
                        selected_summary["source_pi_hit_at_5"]
                        - baseline_summary["source_pi_hit_at_5"]
                    ),
                    "baseline_multi_positive_ndcg_at_10": baseline_summary[
                        "multi_positive_ndcg_at_10"
                    ],
                    "selected_multi_positive_ndcg_at_10": selected_summary[
                        "multi_positive_ndcg_at_10"
                    ],
                }
            )
    return output


def field_support_rows(
    pis: list[dict[str, Any]],
    min_pis: int = 100,
    min_institutions: int = 5,
) -> list[dict[str, Any]]:
    field_pis: defaultdict[str, set[str]] = defaultdict(set)
    field_institutions: defaultdict[str, set[str]] = defaultdict(set)
    field_names: dict[str, str] = {}
    for pi in pis:
        fields = {
            (work.get("primary_openalex_field_id"), work.get("primary_openalex_field_name"))
            for work in pi.get("works") or []
            if work.get("primary_openalex_field_id")
        }
        for field_id, field_name in fields:
            field_pis[field_id].add(pi["person_id"])
            field_institutions[field_id].add(pi["institution_id"])
            field_names[field_id] = field_name or "Unknown"
    return [
        {
            "field_id": field_id,
            "field_name": field_names[field_id],
            "pi_count": len(field_pis[field_id]),
            "institution_count": len(field_institutions[field_id]),
            "field_aware_eligible": (
                len(field_pis[field_id]) >= min_pis
                and len(field_institutions[field_id]) >= min_institutions
            ),
        }
        for field_id in sorted(field_pis)
    ]


def _adjacent_values(value: float, values: tuple[float, ...]) -> list[float]:
    index = values.index(value)
    output = [value]
    if index > 0:
        output.append(values[index - 1])
    if index + 1 < len(values):
        output.append(values[index + 1])
    return output


def field_neighbor_configs(config: AuthorshipWeightConfig) -> list[AuthorshipWeightConfig]:
    candidates: dict[str, AuthorshipWeightConfig] = {}
    factors = (
        ("first_weight", FIRST_WEIGHTS),
        ("last_weight", LAST_WEIGHTS),
        ("middle_medium_weight", MIDDLE_MEDIUM_WEIGHTS),
        ("middle_large_weight", MIDDLE_LARGE_WEIGHTS),
    )
    for field_name, values in factors:
        for value in _adjacent_values(getattr(config, field_name), values):
            candidate = replace(config, scheme="field_aware", **{field_name: value})
            candidates[candidate.config_id] = candidate
    return list(candidates.values())


def build_institution_folds(
    cases: list[dict[str, Any]],
    requested_folds: int = 5,
) -> list[set[str]]:
    counts = Counter(case["institution_id"] for case in cases)
    fold_count = min(requested_folds, len(counts))
    if fold_count <= 0:
        return []
    folds: list[set[str]] = [set() for _index in range(fold_count)]
    fold_sizes = [0] * fold_count
    for institution_id, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        target = min(range(fold_count), key=lambda index: (fold_sizes[index], index))
        folds[target].add(institution_id)
        fold_sizes[target] += count
    return folds


def grouped_fold_results(
    encoder: str,
    config_results: list[dict[str, Any]],
    case_rows_by_config: dict[str, list[dict[str, Any]]],
    equal_config_id: str,
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    folds = build_institution_folds(cases, 5)
    output: list[dict[str, Any]] = []
    for fold_index, heldout_institutions in enumerate(folds, start=1):
        training_results: list[dict[str, Any]] = []
        for result in config_results:
            rows = [
                row
                for row in case_rows_by_config[result["config_id"]]
                if row["institution_id"] not in heldout_institutions
            ]
            training_results.append({**result, **summarize_authorship_rows(rows)})
        selected = select_role_config(training_results)
        selected_heldout = [
            row
            for row in case_rows_by_config[selected["config_id"]]
            if row["institution_id"] in heldout_institutions
        ]
        equal_heldout = [
            row
            for row in case_rows_by_config[equal_config_id]
            if row["institution_id"] in heldout_institutions
        ]
        selected_summary = summarize_authorship_rows(selected_heldout)
        equal_summary = summarize_authorship_rows(equal_heldout)
        output.append(
            {
                "encoder": encoder,
                "fold": fold_index,
                "heldout_institution_ids": ";".join(sorted(heldout_institutions)),
                "selected_config_id": selected["config_id"],
                **{key: selected[key] for key in asdict(AuthorshipWeightConfig("equal"))},
                "selected_first_weight": selected["first_weight"],
                "selected_last_weight": selected["last_weight"],
                "selected_corresponding_weight": selected["corresponding_weight"],
                "selected_solo_weight": selected["solo_weight"],
                "selected_middle_medium_weight": selected["middle_medium_weight"],
                "selected_middle_large_weight": selected["middle_large_weight"],
                "heldout_cases": selected_summary["cases"],
                "equal_source_pi_ndcg_at_10": equal_summary["source_pi_ndcg_at_10"],
                "selected_source_pi_ndcg_at_10": selected_summary["source_pi_ndcg_at_10"],
                "source_pi_ndcg_delta": (
                    selected_summary["source_pi_ndcg_at_10"]
                    - equal_summary["source_pi_ndcg_at_10"]
                ),
                "equal_source_pi_hit_at_5": equal_summary["source_pi_hit_at_5"],
                "selected_source_pi_hit_at_5": selected_summary["source_pi_hit_at_5"],
                "source_pi_hit_at_5_delta": (
                    selected_summary["source_pi_hit_at_5"]
                    - equal_summary["source_pi_hit_at_5"]
                ),
                "equal_multi_positive_ndcg_at_10": equal_summary["multi_positive_ndcg_at_10"],
                "selected_multi_positive_ndcg_at_10": selected_summary[
                    "multi_positive_ndcg_at_10"
                ],
            }
        )
    return output


def fixed_config_fold_results(
    encoder: str,
    config_id: str,
    selected_rows: list[dict[str, Any]],
    equal_rows: list[dict[str, Any]],
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for fold_index, heldout_institutions in enumerate(
        build_institution_folds(cases, 5), start=1
    ):
        selected_summary = summarize_authorship_rows(
            [row for row in selected_rows if row["institution_id"] in heldout_institutions]
        )
        equal_summary = summarize_authorship_rows(
            [row for row in equal_rows if row["institution_id"] in heldout_institutions]
        )
        output.append(
            {
                "encoder": encoder,
                "fold": fold_index,
                "heldout_institution_ids": ";".join(sorted(heldout_institutions)),
                "config_id": config_id,
                "heldout_cases": selected_summary["cases"],
                "equal_source_pi_ndcg_at_10": equal_summary["source_pi_ndcg_at_10"],
                "selected_source_pi_ndcg_at_10": selected_summary["source_pi_ndcg_at_10"],
                "source_pi_ndcg_delta": (
                    selected_summary["source_pi_ndcg_at_10"]
                    - equal_summary["source_pi_ndcg_at_10"]
                ),
                "equal_source_pi_hit_at_5": equal_summary["source_pi_hit_at_5"],
                "selected_source_pi_hit_at_5": selected_summary["source_pi_hit_at_5"],
                "source_pi_hit_at_5_delta": (
                    selected_summary["source_pi_hit_at_5"]
                    - equal_summary["source_pi_hit_at_5"]
                ),
            }
        )
    return output


def phase_two_readiness(pis: list[dict[str, Any]]) -> dict[str, Any]:
    institutions = {pi["institution_id"] for pi in pis}
    domain_pis: defaultdict[str, set[str]] = defaultdict(set)
    for pi in pis:
        for work in pi.get("works") or []:
            domain = work.get("primary_openalex_domain_id")
            if domain:
                domain_pis[domain].add(pi["person_id"])
    domains_with_100_pis = sum(len(pi_ids) >= 100 for pi_ids in domain_pis.values())
    reasons: list[str] = []
    if len(pis) < 500:
        reasons.append(f"requires 500 PIs; found {len(pis)}")
    if len(institutions) < 25:
        reasons.append(f"requires 25 institutions; found {len(institutions)}")
    if domains_with_100_pis < 4:
        reasons.append(
            f"requires four OpenAlex domains with >=100 PIs; found {domains_with_100_pis}"
        )
    return {
        "ready": not reasons,
        "pi_count": len(pis),
        "institution_count": len(institutions),
        "domains_with_100_pis": domains_with_100_pis,
        "reasons": reasons,
    }


def acceptance_gate(
    selected: dict[str, Any],
    equal: dict[str, Any],
    bootstrap: dict[str, float],
    fold_rows: list[dict[str, Any]],
    strata: list[dict[str, Any]],
    phase_two: dict[str, Any],
) -> dict[str, Any]:
    ndcg_delta = selected["source_pi_ndcg_at_10"] - equal["source_pi_ndcg_at_10"]
    hit_delta = selected["source_pi_hit_at_5"] - equal["source_pi_hit_at_5"]
    positive_folds = sum(row["source_pi_ndcg_delta"] > 0 for row in fold_rows)
    supported_regressions = [
        row
        for row in strata
        if row["cases"] >= 100 and row["source_pi_ndcg_delta"] < -0.02
    ]
    checks = {
        "phase_two_ready": bool(phase_two["ready"]),
        "ndcg_delta_at_least_0_005": ndcg_delta >= 0.005,
        "bootstrap_ci_low_above_zero": bootstrap["ci_low"] > 0,
        "positive_in_at_least_four_of_five_folds": (
            len(fold_rows) >= 5 and positive_folds >= 4
        ),
        "hit_at_5_drop_within_0_01": hit_delta >= -0.01,
        "no_supported_stratum_regression_over_0_02": not supported_regressions,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "source_pi_ndcg_delta": ndcg_delta,
        "source_pi_hit_at_5_delta": hit_delta,
        "positive_fold_count": positive_folds,
        "fold_count": len(fold_rows),
        "supported_stratum_regressions": supported_regressions,
    }


def learn_field_overrides(
    base_config: AuthorshipWeightConfig,
    eligible_field_ids: list[str],
    cases: list[dict[str, Any]],
    numeric_bases: dict[str, NumericProfileBasis] | dict[str, list[ProfileGroup]],
    query_vectors: dict[str, tuple[SparseVector, SparseVector]],
    institution_candidates: dict[str, list[str]],
    encoder: str,
    matrix_context: dict[str, InstitutionMatrixContext] | None = None,
) -> tuple[dict[str, AuthorshipWeightConfig], list[dict[str, Any]]]:
    if numeric_bases and isinstance(next(iter(numeric_bases.values())), list):
        numeric_bases = build_numeric_profile_bases(numeric_bases)  # type: ignore[arg-type]
    overrides: dict[str, AuthorshipWeightConfig] = {}
    audit: list[dict[str, Any]] = []
    for field_id in eligible_field_ids:
        field_cases = [case for case in cases if case.get("primary_openalex_field_id") == field_id]
        if not field_cases:
            continue
        current = replace(base_config, scheme="field_aware")
        current_score = -1.0
        for _pass in range(2):
            best_candidate = current
            best_summary: dict[str, float] | None = None
            for candidate in field_neighbor_configs(current):
                candidate_overrides = {**overrides, field_id: candidate}
                profiles = {
                    pi_id: weighted_numeric_career_vector(
                        basis, base_config, candidate_overrides, 256
                    )
                    for pi_id, basis in numeric_bases.items()
                }
                if matrix_context is not None:
                    rows = evaluate_profiles_matrix(
                        field_cases,
                        profiles,
                        matrix_context,
                        candidate.config_id,
                        encoder,
                        "field_aware",
                    )
                else:
                    rows = evaluate_profiles(
                        field_cases,
                        profiles,
                        query_vectors,
                        institution_candidates,
                        candidate.config_id,
                        encoder,
                        "field_aware",
                    )
                summary = summarize_authorship_rows(rows)
                if (
                    summary["source_pi_ndcg_at_10"],
                    summary["source_pi_hit_at_5"],
                    -candidate.deviation,
                ) > (
                    best_summary["source_pi_ndcg_at_10"] if best_summary else current_score,
                    best_summary["source_pi_hit_at_5"] if best_summary else -1.0,
                    -best_candidate.deviation,
                ):
                    best_candidate = candidate
                    best_summary = summary
            if best_summary is None or best_summary["source_pi_ndcg_at_10"] <= current_score:
                break
            current = best_candidate
            current_score = best_summary["source_pi_ndcg_at_10"]
        overrides[field_id] = current
        audit.append(
            {
                "encoder": encoder,
                "field_id": field_id,
                "config_id": current.config_id,
                **asdict(current),
                "field_source_pi_ndcg_at_10": current_score,
                "field_case_count": len(field_cases),
            }
        )
    return overrides, audit


def _evaluate_numeric_config(
    config: AuthorshipWeightConfig,
    encoder: str,
    cases: list[dict[str, Any]],
    numeric_bases: dict[str, NumericProfileBasis],
    query_vectors: dict[str, tuple[SparseVector, SparseVector]],
    institution_candidates: dict[str, list[str]],
    matrix_context: dict[str, InstitutionMatrixContext] | None = None,
) -> tuple[list[dict[str, Any]], float, float]:
    build_started = time.perf_counter()
    profiles = {
        pi_id: weighted_numeric_career_vector(basis, config, feature_limit=256)
        for pi_id, basis in numeric_bases.items()
    }
    build_ms = (time.perf_counter() - build_started) * 1000.0
    scoring_started = time.perf_counter()
    if matrix_context is not None:
        rows = evaluate_profiles_matrix(
            cases,
            profiles,
            matrix_context,
            config.config_id,
            encoder,
            config.scheme,
        )
    else:
        rows = evaluate_profiles(
            cases,
            profiles,
            query_vectors,
            institution_candidates,
            config.config_id,
            encoder,
            config.scheme,
        )
    scoring_ms = (time.perf_counter() - scoring_started) * 1000.0
    return rows, build_ms, scoring_ms


def evaluate_single_authorship_config(
    encoder: str,
    config: AuthorshipWeightConfig,
    cases: list[dict[str, Any]],
    profile_works: dict[str, list[dict[str, Any]]],
    pi_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    vectors_by_work, query_vectors = encode_experiment_texts(
        profile_works, cases, encoder
    )
    groups_by_pi = build_profile_groups(
        profile_works, vectors_by_work, 256, group_by_field=False
    )
    numeric_bases = build_numeric_profile_bases(groups_by_pi)
    institution_candidates: defaultdict[str, list[str]] = defaultdict(list)
    for pi_id in groups_by_pi:
        institution_candidates[pi_by_id[pi_id]["institution_id"]].append(pi_id)
    for pi_ids in institution_candidates.values():
        pi_ids.sort()
    matrix_context = build_evaluation_matrix_context(
        cases, numeric_bases, query_vectors, institution_candidates
    )
    rows, _build_ms, _scoring_ms = _evaluate_numeric_config(
        config,
        encoder,
        cases,
        numeric_bases,
        query_vectors,
        institution_candidates,
        matrix_context,
    )
    return rows


def _materialized_grouped_fold_results(
    encoder: str,
    selections: list[dict[str, Any]],
    case_rows_by_config: dict[str, list[dict[str, Any]]],
    equal_config_id: str,
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for fold_index, (heldout_institutions, selected) in enumerate(
        zip(build_institution_folds(cases, 5), selections), start=1
    ):
        selected_heldout = [
            row
            for row in case_rows_by_config[selected["config_id"]]
            if row["institution_id"] in heldout_institutions
        ]
        equal_heldout = [
            row
            for row in case_rows_by_config[equal_config_id]
            if row["institution_id"] in heldout_institutions
        ]
        selected_summary = summarize_authorship_rows(selected_heldout)
        equal_summary = summarize_authorship_rows(equal_heldout)
        output.append(
            {
                "encoder": encoder,
                "fold": fold_index,
                "heldout_institution_ids": ";".join(sorted(heldout_institutions)),
                "selected_config_id": selected["config_id"],
                "selected_first_weight": selected["first_weight"],
                "selected_last_weight": selected["last_weight"],
                "selected_corresponding_weight": selected["corresponding_weight"],
                "selected_solo_weight": selected["solo_weight"],
                "selected_middle_medium_weight": selected["middle_medium_weight"],
                "selected_middle_large_weight": selected["middle_large_weight"],
                "heldout_cases": selected_summary["cases"],
                "equal_source_pi_ndcg_at_10": equal_summary["source_pi_ndcg_at_10"],
                "selected_source_pi_ndcg_at_10": selected_summary["source_pi_ndcg_at_10"],
                "source_pi_ndcg_delta": (
                    selected_summary["source_pi_ndcg_at_10"]
                    - equal_summary["source_pi_ndcg_at_10"]
                ),
                "equal_source_pi_hit_at_5": equal_summary["source_pi_hit_at_5"],
                "selected_source_pi_hit_at_5": selected_summary["source_pi_hit_at_5"],
                "source_pi_hit_at_5_delta": (
                    selected_summary["source_pi_hit_at_5"]
                    - equal_summary["source_pi_hit_at_5"]
                ),
                "equal_multi_positive_ndcg_at_10": equal_summary[
                    "multi_positive_ndcg_at_10"
                ],
                "selected_multi_positive_ndcg_at_10": selected_summary[
                    "multi_positive_ndcg_at_10"
                ],
            }
        )
    return output


def run_encoder_experiment(
    encoder: str,
    cases: list[dict[str, Any]],
    profile_works: dict[str, list[dict[str, Any]]],
    pi_by_id: dict[str, dict[str, Any]],
    configs: list[AuthorshipWeightConfig],
    field_support: list[dict[str, Any]],
    phase_two: dict[str, Any],
) -> dict[str, Any]:
    vectors_by_work, query_vectors = encode_experiment_texts(profile_works, cases, encoder)
    groups_by_pi = build_profile_groups(profile_works, vectors_by_work, 256, group_by_field=False)
    numeric_bases = build_numeric_profile_bases(groups_by_pi)
    institution_candidates: defaultdict[str, list[str]] = defaultdict(list)
    for pi_id in groups_by_pi:
        institution_candidates[pi_by_id[pi_id]["institution_id"]].append(pi_id)
    for pi_ids in institution_candidates.values():
        pi_ids.sort()
    matrix_context = build_evaluation_matrix_context(
        cases, numeric_bases, query_vectors, institution_candidates
    )

    config_results: list[dict[str, Any]] = []
    case_rows_by_config: dict[str, list[dict[str, Any]]] = {}
    fold_training_results: list[list[dict[str, Any]]] = [
        [] for _fold in build_institution_folds(cases, 5)
    ]
    fold_institutions = build_institution_folds(cases, 5)
    equal_config = next(config for config in configs if config.scheme == "equal")
    for config_index, config in enumerate(configs, start=1):
        rows, build_ms, scoring_ms = _evaluate_numeric_config(
            config,
            encoder,
            cases,
            numeric_bases,
            query_vectors,
            institution_candidates,
            matrix_context,
        )
        result = _config_result(
            config, encoder, rows, build_ms, scoring_ms, len(numeric_bases)
        )
        config_results.append(result)
        for fold_index, heldout_institutions in enumerate(fold_institutions):
            training_rows = [
                row for row in rows if row["institution_id"] not in heldout_institutions
            ]
            fold_training_results[fold_index].append(
                {**result, **summarize_authorship_rows(training_rows)}
            )
        if config.config_id == equal_config.config_id:
            case_rows_by_config[config.config_id] = rows
        if config_index % 100 == 0:
            print(f"{encoder}: evaluated {config_index}/{len(configs)} configs", flush=True)

    equal_result = next(row for row in config_results if row["config_id"] == equal_config.config_id)
    selected_result = select_role_config(config_results)
    selected_config = next(
        config for config in configs if config.config_id == selected_result["config_id"]
    )
    fold_selections = [select_role_config(results) for results in fold_training_results]
    config_by_id = {config.config_id: config for config in configs}
    required_config_ids = {
        selected_config.config_id,
        *(selection["config_id"] for selection in fold_selections),
    }
    for config_id in sorted(required_config_ids):
        if config_id not in case_rows_by_config:
            rows, _build_ms, _scoring_ms = _evaluate_numeric_config(
                config_by_id[config_id],
                encoder,
                cases,
                numeric_bases,
                query_vectors,
                institution_candidates,
                matrix_context,
            )
            case_rows_by_config[config_id] = rows
    selected_rows = case_rows_by_config[selected_config.config_id]
    equal_rows = case_rows_by_config[equal_config.config_id]
    bootstrap = paired_source_bootstrap(selected_rows, equal_rows)
    strata = stratified_comparison(encoder, selected_rows, equal_rows)
    folds = _materialized_grouped_fold_results(
        encoder,
        fold_selections,
        case_rows_by_config,
        equal_config.config_id,
        cases,
    )
    gate = acceptance_gate(selected_result, equal_result, bootstrap, folds, strata, phase_two)

    eligible_fields = [
        row["field_id"] for row in field_support if row["field_aware_eligible"]
    ]
    field_rows: list[dict[str, Any]] = []
    field_case_rows: list[dict[str, Any]] = []
    field_fold_rows: list[dict[str, Any]] = []
    field_result: dict[str, Any] | None = None
    field_bootstrap: dict[str, float] | None = None
    field_gate = {
        "passed": False,
        "status": "not_ready" if not phase_two["ready"] else "no_eligible_fields",
        "eligible_field_count": len(eligible_fields),
    }
    if phase_two["ready"] and eligible_fields:
        field_groups_by_pi = build_profile_groups(
            profile_works,
            vectors_by_work,
            256,
            group_by_field=True,
        )
        field_numeric_bases = build_numeric_profile_bases(field_groups_by_pi)
        overrides, field_rows = learn_field_overrides(
            selected_config,
            eligible_fields,
            cases,
            field_numeric_bases,
            query_vectors,
            institution_candidates,
            encoder,
            matrix_context,
        )
        build_started = time.perf_counter()
        profiles = {
            pi_id: weighted_numeric_career_vector(basis, selected_config, overrides, 256)
            for pi_id, basis in field_numeric_bases.items()
        }
        build_ms = (time.perf_counter() - build_started) * 1000.0
        scoring_started = time.perf_counter()
        field_case_rows = evaluate_profiles_matrix(
            cases,
            profiles,
            matrix_context,
            f"field-{selected_config.config_id}",
            encoder,
            "field_aware",
        )
        scoring_ms = (time.perf_counter() - scoring_started) * 1000.0
        field_summary = summarize_authorship_rows(field_case_rows)
        field_result = {
            "config_id": f"field-{selected_config.config_id}",
            "encoder": encoder,
            "scheme": "field_aware",
            **field_summary,
            "pi_count": len(profiles),
            "build_ms_per_pi": build_ms / max(len(profiles), 1),
            "query_ms_per_case": scoring_ms / max(len(field_case_rows), 1),
        }
        field_bootstrap = paired_source_bootstrap(field_case_rows, selected_rows)
        field_delta = (
            field_result["source_pi_ndcg_at_10"]
            - selected_result["source_pi_ndcg_at_10"]
        )
        for fold_index, heldout_institutions in enumerate(
            build_institution_folds(cases, 5), start=1
        ):
            training_cases = [
                case
                for case in cases
                if case["institution_id"] not in heldout_institutions
            ]
            heldout_cases = [
                case for case in cases if case["institution_id"] in heldout_institutions
            ]
            fold_overrides, _fold_audit = learn_field_overrides(
                selected_config,
                eligible_fields,
                training_cases,
                field_numeric_bases,
                query_vectors,
                institution_candidates,
                encoder,
                matrix_context,
            )
            fold_profiles = {
                pi_id: weighted_numeric_career_vector(
                    basis, selected_config, fold_overrides, 256
                )
                for pi_id, basis in field_numeric_bases.items()
            }
            fold_field_rows = evaluate_profiles_matrix(
                heldout_cases,
                fold_profiles,
                matrix_context,
                f"field-fold-{fold_index}",
                encoder,
                "field_aware",
            )
            fold_role_rows = [
                row
                for row in selected_rows
                if row["institution_id"] in heldout_institutions
            ]
            field_summary = summarize_authorship_rows(fold_field_rows)
            role_summary = summarize_authorship_rows(fold_role_rows)
            field_fold_rows.append(
                {
                    "encoder": encoder,
                    "fold": fold_index,
                    "heldout_institution_ids": ";".join(sorted(heldout_institutions)),
                    "heldout_cases": field_summary["cases"],
                    "role_source_pi_ndcg_at_10": role_summary["source_pi_ndcg_at_10"],
                    "field_source_pi_ndcg_at_10": field_summary["source_pi_ndcg_at_10"],
                    "source_pi_ndcg_delta": (
                        field_summary["source_pi_ndcg_at_10"]
                        - role_summary["source_pi_ndcg_at_10"]
                    ),
                    "role_source_pi_hit_at_5": role_summary["source_pi_hit_at_5"],
                    "field_source_pi_hit_at_5": field_summary["source_pi_hit_at_5"],
                    "source_pi_hit_at_5_delta": (
                        field_summary["source_pi_hit_at_5"]
                        - role_summary["source_pi_hit_at_5"]
                    ),
                }
            )
        field_strata = stratified_comparison(encoder, field_case_rows, selected_rows)
        supported_regressions = [
            row
            for row in field_strata
            if row["cases"] >= 100 and row["source_pi_ndcg_delta"] < -0.02
        ]
        positive_folds = sum(row["source_pi_ndcg_delta"] > 0 for row in field_fold_rows)
        hit_delta = (
            field_result["source_pi_hit_at_5"]
            - selected_result["source_pi_hit_at_5"]
        )
        field_gate = {
            "passed": (
                field_delta >= 0.005
                and field_bootstrap["ci_low"] > 0
                and len(field_fold_rows) >= 5
                and positive_folds >= 4
                and hit_delta >= -0.01
                and not supported_regressions
            ),
            "status": "evaluated",
            "eligible_field_count": len(eligible_fields),
            "source_pi_ndcg_delta": field_delta,
            "source_pi_hit_at_5_delta": hit_delta,
            "positive_fold_count": positive_folds,
            "fold_count": len(field_fold_rows),
            "supported_stratum_regressions": supported_regressions,
            "bootstrap": field_bootstrap,
        }

    config_results.sort(
        key=lambda row: (
            row["scheme"] != "equal",
            -row["source_pi_ndcg_at_10"],
            -row["source_pi_hit_at_5"],
            row["weight_deviation"],
        )
    )
    all_case_rows = [row for rows in case_rows_by_config.values() for row in rows]
    return {
        "encoder": encoder,
        "config_results": config_results,
        "case_rows": all_case_rows,
        "stratified_rows": strata,
        "fold_rows": folds,
        "field_rows": field_rows,
        "field_case_rows": field_case_rows,
        "field_fold_rows": field_fold_rows,
        "equal_result": equal_result,
        "selected_result": selected_result,
        "selected_config": asdict(selected_config),
        "bootstrap": bootstrap,
        "acceptance_gate": gate,
        "field_result": field_result,
        "field_bootstrap": field_bootstrap,
        "field_gate": field_gate,
        "detailed_config_ids": sorted(case_rows_by_config),
    }


def robust_role_selection(encoder_runs: list[dict[str, Any]]) -> dict[str, Any]:
    by_encoder = {
        run["encoder"]: {
            row["config_id"]: row
            for row in run["config_results"]
            if row["scheme"] == "role_aware"
        }
        for run in encoder_runs
    }
    common_ids = set.intersection(*(set(rows) for rows in by_encoder.values()))
    candidates: list[dict[str, Any]] = []
    for config_id in sorted(common_ids):
        rows = [by_encoder[encoder][config_id] for encoder in sorted(by_encoder)]
        candidates.append(
            {
                "config_id": config_id,
                **{
                    key: rows[0][key]
                    for key in (
                        "first_weight",
                        "last_weight",
                        "corresponding_weight",
                        "solo_weight",
                        "middle_medium_weight",
                        "middle_large_weight",
                        "weight_deviation",
                    )
                },
                "worst_source_pi_ndcg_at_10": min(
                    row["source_pi_ndcg_at_10"] for row in rows
                ),
                "mean_source_pi_ndcg_at_10": statistics.fmean(
                    row["source_pi_ndcg_at_10"] for row in rows
                ),
                "worst_source_pi_hit_at_5": min(row["source_pi_hit_at_5"] for row in rows),
                **{
                    f"{encoder}_source_pi_ndcg_at_10": by_encoder[encoder][config_id][
                        "source_pi_ndcg_at_10"
                    ]
                    for encoder in sorted(by_encoder)
                },
            }
        )
    best = max(
        candidates,
        key=lambda row: (
            row["worst_source_pi_ndcg_at_10"],
            row["mean_source_pi_ndcg_at_10"],
            row["worst_source_pi_hit_at_5"],
            -row["weight_deviation"],
        ),
    )
    equivalent = [
        row
        for row in candidates
        if abs(
            row["worst_source_pi_ndcg_at_10"]
            - best["worst_source_pi_ndcg_at_10"]
        )
        <= 1e-12
        and abs(
            row["mean_source_pi_ndcg_at_10"]
            - best["mean_source_pi_ndcg_at_10"]
        )
        <= 1e-12
        and abs(row["worst_source_pi_hit_at_5"] - best["worst_source_pi_hit_at_5"])
        <= 1e-12
        and abs(row["weight_deviation"] - best["weight_deviation"]) <= 1e-12
    ]
    equivalent.sort(key=lambda row: row["config_id"])
    selected = equivalent[0]
    weight_fields = (
        "first_weight",
        "last_weight",
        "corresponding_weight",
        "solo_weight",
        "middle_medium_weight",
        "middle_large_weight",
    )
    selected = {
        **selected,
        "equivalent_config_ids": [row["config_id"] for row in equivalent],
        "equivalent_config_count": len(equivalent),
        "non_identifiable_weight_fields": [
            field
            for field in weight_fields
            if len({row[field] for row in equivalent}) > 1
        ],
    }
    return selected


def _recommendation_markdown(manifest: dict[str, Any]) -> str:
    robust = manifest["robust_role_config"]
    lines = [
        "# Authorship Weight Experiment Recommendation",
        "",
        f"Status: **{manifest['status']}**",
        "",
        "## Screening result",
        "",
        f"- Robust config: `{robust['config_id']}`",
        f"- First: `{robust['first_weight']}`",
        f"- Last: `{robust['last_weight']}`",
        f"- Confirmed corresponding multiplier: `{robust['corresponding_weight']}`",
        f"- Solo: `{robust['solo_weight']}`",
        f"- Middle 5-10 authors: `{robust['middle_medium_weight']}`",
        f"- Middle 11+ authors: `{robust['middle_large_weight']}`",
        f"- Equivalent top configurations: `{robust['equivalent_config_count']}`",
        "- Non-identifiable fields: `"
        + ", ".join(robust["non_identifiable_weight_fields"])
        + "`",
        "",
        "## Interpretation",
        "",
        "These weights measure PI retrieval utility only. They do not estimate actual scientific credit.",
        "",
        "## Robust comparison with Equal",
        "",
        "| Encoder | nDCG delta | Paired 95% CI | Hit@5 delta | Gate |",
        "|---|---:|---:|---:|---|",
    ]
    for encoder, result in manifest["robust_encoder_results"].items():
        gate = result["acceptance_gate"]
        bootstrap = result["paired_bootstrap"]
        lines.append(
            f"| {encoder} | {gate['source_pi_ndcg_delta']:+.4f} | "
            f"[{bootstrap['ci_low']:+.4f}, {bootstrap['ci_high']:+.4f}] | "
            f"{gate['source_pi_hit_at_5_delta']:+.4f} | "
            f"{'pass' if gate['passed'] else 'fail'} |"
        )
    lines.extend(
        [
            "",
            (
                "Phase two is complete; keep Equal in production because no alternative passed all gates."
                if manifest["phase_two_readiness"]["ready"]
                else "No production weighting change is authorized by stage one."
            ),
            "",
        "## Phase-two readiness",
        "",
        ]
    )
    readiness = manifest["phase_two_readiness"]
    lines.append(f"Ready: `{readiness['ready']}`")
    for reason in readiness["reasons"]:
        lines.append(f"- {reason}")
    return "\n".join(lines) + "\n"


def run_authorship_experiment(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    pis = dataset.get("pis") or []
    cases, profile_works, heldout_ids = build_authorship_cases(
        pis,
        max_proposals=args.max_proposals,
        cv_work_count=args.cv_works,
        min_profile_works=args.min_profile_works,
        seed=args.seed,
    )
    pi_by_id = {pi["person_id"]: pi for pi in pis if pi["person_id"] in profile_works}
    configs = generate_weight_configs()
    if args.max_configs > 0:
        configs = configs[: args.max_configs]
        if not any(config.scheme == "role_aware" for config in configs):
            configs.append(generate_weight_configs()[1])
    field_support = field_support_rows(
        pis,
        min_pis=args.field_min_pis,
        min_institutions=args.field_min_institutions,
    )
    phase_two = phase_two_readiness(pis)

    encoder_runs: list[dict[str, Any]] = []
    for encoder in args.encoders:
        encoder_runs.append(
            run_encoder_experiment(
                encoder,
                cases,
                profile_works,
                pi_by_id,
                configs,
                field_support,
                phase_two,
            )
        )
    robust = robust_role_selection(encoder_runs)
    robust_encoder_results: dict[str, dict[str, Any]] = {}
    robust_fold_rows: list[dict[str, Any]] = []
    robust_strata_rows: list[dict[str, Any]] = []
    for run in encoder_runs:
        robust_result = next(
            row
            for row in run["config_results"]
            if row["config_id"] == robust["config_id"]
        )
        robust_rows = [
            row for row in run["case_rows"] if row["config_id"] == robust["config_id"]
        ]
        if len(robust_rows) != len(cases):
            robust_config = next(
                config for config in configs if config.config_id == robust["config_id"]
            )
            robust_rows = evaluate_single_authorship_config(
                run["encoder"],
                robust_config,
                cases,
                profile_works,
                pi_by_id,
            )
            run["case_rows"] = [
                row
                for row in run["case_rows"]
                if row["config_id"] != robust["config_id"]
            ] + robust_rows
            run["detailed_config_ids"] = sorted(
                set(run["detailed_config_ids"]) | {robust["config_id"]}
            )
        equal_rows = [
            row
            for row in run["case_rows"]
            if row["config_id"] == run["equal_result"]["config_id"]
        ]
        bootstrap = paired_source_bootstrap(robust_rows, equal_rows)
        strata = stratified_comparison(run["encoder"], robust_rows, equal_rows)
        fixed_folds = fixed_config_fold_results(
            run["encoder"],
            robust["config_id"],
            robust_rows,
            equal_rows,
            cases,
        )
        gate = acceptance_gate(
            robust_result,
            run["equal_result"],
            bootstrap,
            run["fold_rows"],
            strata,
            phase_two,
        )
        robust_encoder_results[run["encoder"]] = {
            "result": robust_result,
            "paired_bootstrap": bootstrap,
            "acceptance_gate": gate,
            "acceptance_fold_method": "institution_grouped_nested_selection",
        }
        robust_fold_rows.extend(
            {**row, "fold_method": "fixed_robust_config_diagnostic"}
            for row in fixed_folds
        )
        robust_strata_rows.extend(strata)
    all_config_rows = [row for run in encoder_runs for row in run["config_results"]]
    all_case_rows = [row for run in encoder_runs for row in run["case_rows"]]
    all_strata_rows = [row for run in encoder_runs for row in run["stratified_rows"]]
    all_fold_rows = [row for run in encoder_runs for row in run["fold_rows"]]
    all_field_rows = [row for run in encoder_runs for row in run["field_rows"]]
    all_field_fold_rows = [row for run in encoder_runs for row in run["field_fold_rows"]]

    scheme_rows: list[dict[str, Any]] = []
    for run in encoder_runs:
        for label, result in (("equal", run["equal_result"]), ("selected_role", run["selected_result"])):
            scheme_rows.append(
                {
                    "encoder": run["encoder"],
                    "selection": label,
                    "config_id": result["config_id"],
                    "source_pi_ndcg_at_10": result["source_pi_ndcg_at_10"],
                    "source_pi_mrr": result["source_pi_mrr"],
                    "source_pi_hit_at_5": result["source_pi_hit_at_5"],
                    "multi_positive_ndcg_at_10": result["multi_positive_ndcg_at_10"],
                    "acceptance_passed": (
                        False if label == "equal" else run["acceptance_gate"]["passed"]
                    ),
                }
            )
        if run["field_result"]:
            result = run["field_result"]
            scheme_rows.append(
                {
                    "encoder": run["encoder"],
                    "selection": "field_aware",
                    "config_id": result["config_id"],
                    "source_pi_ndcg_at_10": result["source_pi_ndcg_at_10"],
                    "source_pi_mrr": result["source_pi_mrr"],
                    "source_pi_hit_at_5": result["source_pi_hit_at_5"],
                    "multi_positive_ndcg_at_10": result["multi_positive_ndcg_at_10"],
                    "acceptance_passed": run["field_gate"]["passed"],
                }
            )
        robust_result = robust_encoder_results[run["encoder"]]["result"]
        scheme_rows.append(
            {
                "encoder": run["encoder"],
                "selection": "robust_role",
                "config_id": robust_result["config_id"],
                "source_pi_ndcg_at_10": robust_result["source_pi_ndcg_at_10"],
                "source_pi_mrr": robust_result["source_pi_mrr"],
                "source_pi_hit_at_5": robust_result["source_pi_hit_at_5"],
                "multi_positive_ndcg_at_10": robust_result[
                    "multi_positive_ndcg_at_10"
                ],
                "acceptance_passed": robust_encoder_results[run["encoder"]][
                    "acceptance_gate"
                ]["passed"],
            }
        )

    _write_csv(output_dir / "config_results.csv", all_config_rows)
    _write_csv(output_dir / "case_results.csv", all_case_rows)
    _write_csv(output_dir / "stratified_results.csv", all_strata_rows)
    _write_csv(output_dir / "institution_fold_results.csv", all_fold_rows)
    _write_csv(output_dir / "robust_fold_results.csv", robust_fold_rows)
    _write_csv(output_dir / "robust_stratified_results.csv", robust_strata_rows)
    _write_csv(output_dir / "field_support.csv", field_support)
    _write_csv(output_dir / "field_override_results.csv", all_field_rows)
    _write_csv(output_dir / "field_fold_results.csv", all_field_fold_rows)
    _write_csv(output_dir / "scheme_comparison.csv", scheme_rows)

    gates_passed = all(
        result["acceptance_gate"]["passed"]
        for result in robust_encoder_results.values()
    )
    if gates_passed:
        status = "production_candidate"
    elif phase_two["ready"]:
        status = "phase2_no_change"
    else:
        status = "stage1_screening_only"
    manifest = {
        "generated_at": utc_now_iso(),
        "status": status,
        "dataset": str(Path(args.dataset).resolve()),
        "dataset_pi_count": len(pis),
        "eligible_profile_pi_count": len(profile_works),
        "institution_count": len({pi["institution_id"] for pi in pis}),
        "case_count": len(cases),
        "heldout_work_count": len(heldout_ids),
        "config_count_per_encoder": len(configs),
        "encoders": args.encoders,
        "profile_resolution": {
            "mode": "career",
            "feature_limit": 256,
            "recent_vector": False,
            "topic_clusters": False,
            "paper_rerank": False,
        },
        "robust_role_config": robust,
        "robust_encoder_results": robust_encoder_results,
        "phase_two_readiness": phase_two,
        "field_support": field_support,
        "encoder_results": {
            run["encoder"]: {
                "equal_result": run["equal_result"],
                "selected_result": run["selected_result"],
                "selected_config": run["selected_config"],
                "paired_bootstrap": run["bootstrap"],
                "acceptance_gate": run["acceptance_gate"],
                "field_gate": run["field_gate"],
            }
            for run in encoder_runs
        },
        "interpretation": (
            "Authorship weights measure institution-constrained PI retrieval utility only; "
            "they do not estimate actual scientific contribution."
        ),
    }
    (output_dir / "experiment_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "recommendation.md").write_text(
        _recommendation_markdown(manifest), encoding="utf-8"
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Authorship weighting benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Enrich cached works with target authorship roles")
    prepare.add_argument("--dataset", required=True)
    prepare.add_argument("--openalex-cache", required=True)
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--refresh-work-types", action="store_true")
    prepare.add_argument("--start-year", type=int, default=2012)
    prepare.add_argument("--max-works", type=int, default=100)
    prepare.add_argument("--reserve-credits", type=int, default=20)

    run = subparsers.add_parser("run", help="Run Equal, Role-aware and Field-aware ablations")
    run.add_argument("--dataset", required=True)
    run.add_argument("--output-dir", required=True)
    run.add_argument(
        "--encoders",
        nargs="+",
        choices=("production_terms", "tfidf"),
        default=("production_terms", "tfidf"),
    )
    run.add_argument("--max-proposals", type=int, default=3)
    run.add_argument("--cv-works", type=int, default=2)
    run.add_argument("--min-profile-works", type=int, default=8)
    run.add_argument("--field-min-pis", type=int, default=100)
    run.add_argument("--field-min-institutions", type=int, default=5)
    run.add_argument("--seed", type=int, default=20260712)
    run.add_argument("--max-configs", type=int, default=0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "prepare":
        result = prepare_authorship_dataset(args)
    else:
        result = run_authorship_experiment(args)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
