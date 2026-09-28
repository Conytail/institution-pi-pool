from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path
import statistics
import time
from typing import Any, Iterable

import numpy as np
from scipy.sparse import csr_matrix

from .authorship_weight_experiment import (
    build_authorship_cases,
    paired_source_bootstrap,
    source_ndcg,
    summarize_authorship_rows,
)
from .research_profile_experiment import (
    SparseVector,
    _vector_bytes,
    _write_csv,
    centroid,
    encode_experiment_texts,
    ndcg_at_k,
    normalize_vector,
    utc_now_iso,
)


STRATEGIES = ("career256", "paper_max256", "paper_top3_256", "hybrid_top10")


@dataclass
class InstitutionContext:
    institution_id: str
    candidates: list[str]
    cases: list[dict[str, Any]]
    query_matrix: csr_matrix
    career_matrix: csr_matrix
    paper_matrix: csr_matrix
    paper_spans: list[tuple[int, int]]


def _combined_query(
    proposal: SparseVector,
    cv: SparseVector,
    proposal_weight: float = 0.8,
) -> SparseVector:
    combined: defaultdict[str, float] = defaultdict(float)
    for term, value in proposal.items():
        combined[term] += proposal_weight * value
    for term, value in cv.items():
        combined[term] += (1.0 - proposal_weight) * value
    return dict(combined)


def _vectors_to_matrix(
    vectors: Iterable[SparseVector],
    term_indexes: dict[str, int],
) -> csr_matrix:
    vectors = list(vectors)
    rows: list[int] = []
    columns: list[int] = []
    values: list[float] = []
    for row_index, vector in enumerate(vectors):
        for term, value in vector.items():
            column = term_indexes.get(term)
            if column is None:
                continue
            rows.append(row_index)
            columns.append(column)
            values.append(value)
    return csr_matrix(
        (values, (rows, columns)),
        shape=(len(vectors), len(term_indexes)),
        dtype=np.float64,
    )


def top_k_mean_by_span(
    scores: np.ndarray,
    spans: list[tuple[int, int]],
    k: int,
) -> np.ndarray:
    output = np.zeros(len(spans), dtype=np.float64)
    for candidate_index, (start, end) in enumerate(spans):
        values = scores[start:end]
        if values.size == 0:
            continue
        keep = min(k, values.size)
        if keep == values.size:
            output[candidate_index] = float(values.mean())
        else:
            output[candidate_index] = float(
                np.partition(values, values.size - keep)[-keep:].mean()
            )
    return output


def _descending_indexes(scores: np.ndarray, candidates: list[str]) -> list[int]:
    return sorted(
        range(len(candidates)),
        key=lambda index: (float(scores[index]), candidates[index]),
        reverse=True,
    )


def score_query(
    context: InstitutionContext,
    case_index: int,
    strategy: str,
    shortlist_size: int,
) -> tuple[list[str], int, int]:
    query = context.query_matrix[case_index]
    if strategy == "career256":
        career_scores = np.asarray(query.dot(context.career_matrix.T).toarray()[0])
        career_order = _descending_indexes(career_scores, context.candidates)
        return [context.candidates[index] for index in career_order], len(context.candidates), 0

    if strategy in {"paper_max256", "paper_top3_256"}:
        paper_scores = np.asarray(query.dot(context.paper_matrix.T).toarray()[0])
        k = 1 if strategy == "paper_max256" else 3
        scores = top_k_mean_by_span(paper_scores, context.paper_spans, k)
        order = _descending_indexes(scores, context.candidates)
        return [context.candidates[index] for index in order], 0, context.paper_matrix.shape[0]

    if strategy != "hybrid_top10":
        raise ValueError(f"unsupported strategy: {strategy}")

    career_scores = np.asarray(query.dot(context.career_matrix.T).toarray()[0])
    career_order = _descending_indexes(career_scores, context.candidates)
    shortlist = career_order[: min(shortlist_size, len(career_order))]
    paper_indexes: list[int] = []
    local_spans: list[tuple[int, int]] = []
    for candidate_index in shortlist:
        start, end = context.paper_spans[candidate_index]
        local_start = len(paper_indexes)
        paper_indexes.extend(range(start, end))
        local_spans.append((local_start, len(paper_indexes)))
    if paper_indexes:
        selected_matrix = context.paper_matrix[paper_indexes]
        selected_scores = np.asarray(query.dot(selected_matrix.T).toarray()[0])
        paper_top3 = top_k_mean_by_span(selected_scores, local_spans, 3)
    else:
        paper_top3 = np.zeros(len(shortlist), dtype=np.float64)
    hybrid_scores = {
        candidate_index: max(float(career_scores[candidate_index]), float(paper_top3[index]))
        for index, candidate_index in enumerate(shortlist)
    }
    reranked = sorted(
        shortlist,
        key=lambda index: (hybrid_scores[index], context.candidates[index]),
        reverse=True,
    )
    shortlist_set = set(shortlist)
    order = reranked + [index for index in career_order if index not in shortlist_set]
    return [context.candidates[index] for index in order], len(context.candidates), len(paper_indexes)


def build_contexts(
    pis: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    profile_works: dict[str, list[dict[str, Any]]],
    vectors_by_work: dict[str, SparseVector],
    query_vectors: dict[str, tuple[SparseVector, SparseVector]],
    feature_limit: int,
) -> tuple[dict[str, InstitutionContext], dict[str, SparseVector], float]:
    pi_by_id = {pi["person_id"]: pi for pi in pis if pi["person_id"] in profile_works}
    candidates_by_institution: defaultdict[str, list[str]] = defaultdict(list)
    cases_by_institution: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for pi_id, pi in pi_by_id.items():
        candidates_by_institution[pi["institution_id"]].append(pi_id)
    for case in cases:
        cases_by_institution[case["institution_id"]].append(case)

    aggregation_started = time.perf_counter()
    careers = {
        pi_id: centroid(
            [vectors_by_work[work["id"]] for work in works if vectors_by_work.get(work["id"])],
            feature_limit,
        )
        for pi_id, works in profile_works.items()
    }
    aggregation_ms = (time.perf_counter() - aggregation_started) * 1000.0

    contexts: dict[str, InstitutionContext] = {}
    for institution_id, institution_cases in sorted(cases_by_institution.items()):
        candidates = sorted(candidates_by_institution[institution_id])
        query_rows = [
            _combined_query(*query_vectors[case["case_id"]]) for case in institution_cases
        ]
        paper_rows: list[SparseVector] = []
        paper_spans: list[tuple[int, int]] = []
        for pi_id in candidates:
            start = len(paper_rows)
            paper_rows.extend(
                vectors_by_work[work["id"]]
                for work in profile_works[pi_id]
                if vectors_by_work.get(work["id"])
            )
            paper_spans.append((start, len(paper_rows)))
        career_rows = [careers[pi_id] for pi_id in candidates]
        terms = sorted(
            {
                term
                for vector in [*query_rows, *career_rows, *paper_rows]
                for term in vector
            }
        )
        term_indexes = {term: index for index, term in enumerate(terms)}
        contexts[institution_id] = InstitutionContext(
            institution_id=institution_id,
            candidates=candidates,
            cases=institution_cases,
            query_matrix=_vectors_to_matrix(query_rows, term_indexes),
            career_matrix=_vectors_to_matrix(career_rows, term_indexes),
            paper_matrix=_vectors_to_matrix(paper_rows, term_indexes),
            paper_spans=paper_spans,
        )
    return contexts, careers, aggregation_ms


def evaluate_strategy(
    contexts: dict[str, InstitutionContext],
    strategy: str,
    shortlist_size: int,
    collect_rows: bool = True,
) -> tuple[list[dict[str, Any]], float, float, float]:
    rows: list[dict[str, Any]] = []
    pi_comparisons = 0
    paper_comparisons = 0
    case_count = 0
    started = time.perf_counter()
    for context in contexts.values():
        for case_index, case in enumerate(context.cases):
            ranked, compared_pis, compared_papers = score_query(
                context, case_index, strategy, shortlist_size
            )
            pi_comparisons += compared_pis
            paper_comparisons += compared_papers
            case_count += 1
            if not collect_rows:
                continue
            source_rank = ranked.index(case["source_pi_id"]) + 1
            positives = set(case["positive_pi_ids"])
            rows.append(
                {
                    "strategy": strategy,
                    "case_id": case["case_id"],
                    "source_pi_id": case["source_pi_id"],
                    "institution_id": case["institution_id"],
                    "institution_name": case["institution_name"],
                    "source_pi_rank": source_rank,
                    "source_pi_reciprocal_rank": 1.0 / source_rank,
                    "source_pi_ndcg_at_10": source_ndcg(source_rank),
                    "multi_positive_ndcg_at_10": ndcg_at_k(ranked, positives, 10),
                    "candidate_count": len(ranked),
                    "pi_vectors_compared": compared_pis,
                    "paper_vectors_compared": compared_papers,
                    "author_role": case.get("author_role") or "unknown",
                    "corresponding_confirmed": case.get("corresponding_confirmed") or "unknown",
                    "author_count_band": case.get("author_count_band") or "unknown",
                    "work_type": case.get("work_type") or "unknown",
                    "publication_age_band": case.get("publication_age_band") or "unknown",
                    "primary_openalex_field_name": case.get("primary_openalex_field_name") or "Unknown",
                }
            )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return (
        rows,
        elapsed_ms,
        pi_comparisons / max(case_count, 1),
        paper_comparisons / max(case_count, 1),
    )


def _compact_manifest_bytes(works: list[dict[str, Any]]) -> int:
    manifest = [
        {
            "id": work.get("id"),
            "doi": work.get("doi"),
            "title": work.get("title"),
            "year": work.get("year"),
            "updated_date": work.get("updated_date"),
        }
        for work in works
    ]
    return len(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def storage_summary(
    dataset_path: Path,
    profile_works: dict[str, list[dict[str, Any]]],
    vectors_by_work: dict[str, SparseVector],
    careers: dict[str, SparseVector],
) -> dict[str, Any]:
    career_bytes = sum(_vector_bytes(vector) for vector in careers.values())
    ownership_paper_bytes = sum(
        _vector_bytes(vectors_by_work[work["id"]])
        for works in profile_works.values()
        for work in works
        if vectors_by_work.get(work["id"])
    )
    unique_work_ids = {
        work["id"]
        for works in profile_works.values()
        for work in works
        if vectors_by_work.get(work["id"])
    }
    deduplicated_paper_bytes = sum(
        _vector_bytes(vectors_by_work[work_id]) for work_id in unique_work_ids
    )
    manifest_bytes = sum(_compact_manifest_bytes(works) for works in profile_works.values())
    pi_count = len(profile_works)
    return {
        "pi_count": pi_count,
        "training_publication_rows": sum(len(works) for works in profile_works.values()),
        "unique_training_works": len(unique_work_ids),
        "career_vector_total_bytes": career_bytes,
        "career_vector_bytes_per_pi": career_bytes / max(pi_count, 1),
        "paper_vectors_by_pi_total_bytes": ownership_paper_bytes,
        "paper_vectors_by_pi_bytes_per_pi": ownership_paper_bytes / max(pi_count, 1),
        "paper_vectors_deduplicated_total_bytes": deduplicated_paper_bytes,
        "paper_vectors_deduplicated_bytes_per_pi": deduplicated_paper_bytes / max(pi_count, 1),
        "paper_to_career_storage_multiplier": ownership_paper_bytes / max(career_bytes, 1),
        "publication_manifest_total_bytes": manifest_bytes,
        "publication_manifest_bytes_per_pi": manifest_bytes / max(pi_count, 1),
        "source_dataset_bytes": dataset_path.stat().st_size,
        "source_dataset_bytes_per_pi": dataset_path.stat().st_size / max(pi_count, 1),
    }


def external_call_model(
    pis: list[dict[str, Any]],
    profile_works: dict[str, list[dict[str, Any]]],
    shortlist_size: int,
) -> list[dict[str, Any]]:
    all_rows = [work for pi in pis for work in pi.get("works") or []]
    unique_all = {work["id"]: work for work in all_rows if work.get("id")}
    training_rows = [work for works in profile_works.values() for work in works]
    doi_rows = sum(bool(work.get("doi")) for work in all_rows)
    unique_doi_count = len({work["doi"] for work in unique_all.values() if work.get("doi")})
    candidate_counts: defaultdict[str, int] = defaultdict(int)
    for pi in pis:
        if pi["person_id"] in profile_works:
            candidate_counts[pi["institution_id"]] += 1
    mean_candidates = statistics.fmean(candidate_counts.values()) if candidate_counts else 0.0
    mean_works = statistics.fmean(len(works) for works in profile_works.values())
    doi_coverage = doi_rows / max(len(all_rows), 1)
    return [
        {
            "mode": "precomputed_career_vector",
            "external_calls_per_match": 0,
            "full_refresh_calls": len(profile_works),
            "coverage": 1.0,
            "note": "One cached author-work-list request per PI; no request during matching.",
        },
        {
            "mode": "precomputed_paper_vectors",
            "external_calls_per_match": 0,
            "full_refresh_calls": len(profile_works),
            "coverage": 1.0,
            "note": "Paper vectors are generated during author-level sync; DOI is metadata only.",
        },
        {
            "mode": "doi_on_demand_full_institution",
            "external_calls_per_match": mean_candidates * mean_works * doi_coverage,
            "full_refresh_calls": unique_doi_count,
            "coverage": doi_coverage,
            "note": "Estimated one request per DOI; misses works without DOI and is not executed.",
        },
        {
            "mode": f"doi_on_demand_top_{shortlist_size}",
            "external_calls_per_match": min(shortlist_size, mean_candidates) * mean_works * doi_coverage,
            "full_refresh_calls": unique_doi_count,
            "coverage": doi_coverage,
            "note": "Estimated after career shortlist; still unnecessary when vectors are cached.",
        },
    ]


def _strategy_storage_bytes(
    strategy: str,
    storage: dict[str, Any],
) -> float:
    if strategy == "career256":
        return float(storage["career_vector_bytes_per_pi"])
    if strategy in {"paper_max256", "paper_top3_256"}:
        return float(storage["paper_vectors_by_pi_bytes_per_pi"])
    return float(storage["career_vector_bytes_per_pi"]) + float(
        storage["paper_vectors_by_pi_bytes_per_pi"]
    )


def _recommendation_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        "# PI Research Profile Granularity Experiment",
        "",
        f"Status: **{manifest['status']}**",
        "",
        "| Encoder | Strategy | nDCG@10 | MRR | Hit@5 | ms/query | bytes/PI | Delta vs career | 95% CI |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in manifest["strategy_results"]:
        ci = row["paired_bootstrap_vs_career"]
        lines.append(
            f"| {row['encoder']} | {row['strategy']} | "
            f"{row['source_pi_ndcg_at_10']:.6f} | {row['source_pi_mrr']:.6f} | "
            f"{row['source_pi_hit_at_5']:.6f} | {row['query_ms_per_case']:.4f} | "
            f"{row['research_vector_bytes_per_pi']:.0f} | {ci['mean_delta']:+.6f} | "
            f"[{ci['ci_low']:+.6f}, {ci['ci_high']:+.6f}] |"
        )
    lines.extend(
        [
            "",
            "## Decision",
            "",
            manifest["decision"],
            "",
            "DOI is not a matching-time dependency. Sync publications by confirmed OpenAlex Author ID, "
            "cache vectors and retain DOI/OpenAlex Work ID only for deduplication, provenance and refresh.",
            "",
            "The benchmark uses globally held-out publications as proposal proxies. It measures research "
            "retrieval, not final human judgement of researcher recommendation suitability.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    dataset_path = Path(args.dataset)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    pis = dataset.get("pis") or []
    cases, profile_works, heldout_ids = build_authorship_cases(
        pis,
        max_proposals=args.max_proposals,
        cv_work_count=args.cv_works,
        min_profile_works=args.min_profile_works,
        seed=args.seed,
    )

    all_results: list[dict[str, Any]] = []
    all_case_rows: list[dict[str, Any]] = []
    storage_by_encoder: dict[str, dict[str, Any]] = {}
    build_by_encoder: dict[str, dict[str, float]] = {}
    for encoder in args.encoders:
        encode_started = time.perf_counter()
        vectors_by_work, query_vectors = encode_experiment_texts(
            profile_works, cases, encoder
        )
        for work_id, vector in list(vectors_by_work.items()):
            vectors_by_work[work_id] = normalize_vector(vector, args.feature_limit)
        encode_ms = (time.perf_counter() - encode_started) * 1000.0

        context_started = time.perf_counter()
        contexts, careers, aggregation_ms = build_contexts(
            pis,
            cases,
            profile_works,
            vectors_by_work,
            query_vectors,
            args.feature_limit,
        )
        context_ms = (time.perf_counter() - context_started) * 1000.0
        storage = storage_summary(dataset_path, profile_works, vectors_by_work, careers)
        storage_by_encoder[encoder] = storage
        build_by_encoder[encoder] = {
            "publication_encoding_ms_total": encode_ms,
            "publication_encoding_ms_per_pi": encode_ms / max(len(profile_works), 1),
            "career_aggregation_ms_total": aggregation_ms,
            "career_aggregation_ms_per_pi": aggregation_ms / max(len(profile_works), 1),
            "matrix_context_ms_total": context_ms - aggregation_ms,
        }

        rows_by_strategy: dict[str, list[dict[str, Any]]] = {}
        for strategy in STRATEGIES:
            # Warm sparse kernels before measuring request latency.
            first_context = next(iter(contexts.values()))
            for case_index in range(min(3, len(first_context.cases))):
                score_query(first_context, case_index, strategy, args.shortlist_size)
            elapsed_values: list[float] = []
            rows: list[dict[str, Any]] = []
            mean_pis = 0.0
            mean_papers = 0.0
            for repeat in range(args.timing_repeats):
                current_rows, elapsed_ms, current_mean_pis, current_mean_papers = evaluate_strategy(
                    contexts,
                    strategy,
                    args.shortlist_size,
                    collect_rows=repeat == 0,
                )
                elapsed_values.append(elapsed_ms)
                if repeat == 0:
                    rows = current_rows
                    mean_pis = current_mean_pis
                    mean_papers = current_mean_papers
            rows_by_strategy[strategy] = rows
            all_case_rows.extend({**row, "encoder": encoder} for row in rows)
            summary = summarize_authorship_rows(rows)
            all_results.append(
                {
                    "encoder": encoder,
                    "strategy": strategy,
                    **summary,
                    "query_ms_per_case": statistics.median(elapsed_values) / max(len(cases), 1),
                    "query_timing_repeats": args.timing_repeats,
                    "pi_vectors_compared_per_case": mean_pis,
                    "paper_vectors_compared_per_case": mean_papers,
                    "research_vector_bytes_per_pi": _strategy_storage_bytes(strategy, storage),
                    "storage_multiplier_vs_career": _strategy_storage_bytes(strategy, storage)
                    / max(float(storage["career_vector_bytes_per_pi"]), 1.0),
                }
            )

        career_rows = rows_by_strategy["career256"]
        for result in [row for row in all_results if row["encoder"] == encoder]:
            strategy_rows = rows_by_strategy[result["strategy"]]
            result["paired_bootstrap_vs_career"] = paired_source_bootstrap(
                strategy_rows,
                career_rows,
                samples=args.bootstrap_samples,
                seed=args.seed,
            )

    call_rows = external_call_model(pis, profile_works, args.shortlist_size)
    _write_csv(output_dir / "strategy_results.csv", all_results)
    _write_csv(output_dir / "case_results.csv", all_case_rows)
    _write_csv(output_dir / "external_call_model.csv", call_rows)
    _write_csv(
        output_dir / "storage_results.csv",
        [{"encoder": encoder, **values} for encoder, values in storage_by_encoder.items()],
    )
    _write_csv(
        output_dir / "build_time_results.csv",
        [{"encoder": encoder, **values} for encoder, values in build_by_encoder.items()],
    )

    noncareer = [row for row in all_results if row["strategy"] != "career256"]
    qualifying = [
        row
        for row in noncareer
        if row["paired_bootstrap_vs_career"]["mean_delta"] >= args.min_ndcg_gain
        and row["paired_bootstrap_vs_career"]["ci_low"] > 0
        and row["source_pi_hit_at_5"]
        >= next(
            baseline["source_pi_hit_at_5"]
            for baseline in all_results
            if baseline["encoder"] == row["encoder"]
            and baseline["strategy"] == "career256"
        )
        - args.max_hit5_loss
    ]
    passed_both_encoders = {
        row["strategy"]
        for row in qualifying
        if all(
            any(
                candidate["strategy"] == row["strategy"]
                and candidate["encoder"] == encoder
                for candidate in qualifying
            )
            for encoder in args.encoders
        )
    }
    if passed_both_encoders:
        selected = min(
            (row for row in all_results if row["strategy"] in passed_both_encoders),
            key=lambda row: (
                row["storage_multiplier_vs_career"],
                row["query_ms_per_case"],
                -row["source_pi_ndcg_at_10"],
            ),
        )["strategy"]
        status = "paper_resolution_candidate"
        decision = (
            f"`{selected}` passed the predeclared accuracy gate under both frozen encoders. "
            "It is the lowest-cost qualifying paper-resolution strategy."
        )
    else:
        status = "career_vector_default"
        decision = (
            "No paper-level strategy produced a statistically reliable nDCG@10 gain of at least "
            f"{args.min_ndcg_gain:.3f} under both encoders. Keep one 256-feature career vector in "
            "the hot PI index; retain publication manifests and optional paper vectors outside the hot path."
        )

    all_publication_rows = [work for pi in pis for work in pi.get("works") or []]
    doi_coverage = sum(bool(work.get("doi")) for work in all_publication_rows) / max(
        len(all_publication_rows), 1
    )
    manifest = {
        "generated_at": utc_now_iso(),
        "status": status,
        "decision": decision,
        "dataset": str(dataset_path.resolve()),
        "dataset_pi_count": len(pis),
        "eligible_profile_pi_count": len(profile_works),
        "institution_count": len({pi["institution_id"] for pi in pis}),
        "case_count": len(cases),
        "heldout_work_count": len(heldout_ids),
        "feature_limit": args.feature_limit,
        "shortlist_size": args.shortlist_size,
        "strategies": list(STRATEGIES),
        "encoders": args.encoders,
        "quality_gate": {
            "minimum_ndcg_gain": args.min_ndcg_gain,
            "paired_bootstrap_ci_low_must_exceed_zero": True,
            "maximum_hit_at_5_loss": args.max_hit5_loss,
            "must_pass_both_encoders": True,
        },
        "doi": {
            "coverage": doi_coverage,
            "missing_fraction": 1.0 - doi_coverage,
            "required_during_matching": False,
            "recommended_identifier": "OpenAlex Work ID, with DOI retained when present",
        },
        "build_times": build_by_encoder,
        "storage": storage_by_encoder,
        "external_call_model": call_rows,
        "strategy_results": all_results,
        "limitations": [
            "Globally held-out publications are proposal proxies, not human-labelled applicant-to-PI judgements.",
            "Official institution pools are department or school subsets rather than complete university-wide pools.",
            "Publication histories start in 2012 and are capped at 100 works per PI in this dataset.",
            "External DOI latency is modelled rather than executed because network and rate limiting are not retrieval costs.",
        ],
    }
    (output_dir / "experiment_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "recommendation.md").write_text(
        _recommendation_markdown(manifest), encoding="utf-8"
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PI profile granularity benchmark")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--encoders",
        nargs="+",
        choices=("production_terms", "tfidf"),
        default=("production_terms", "tfidf"),
    )
    parser.add_argument("--feature-limit", type=int, default=256)
    parser.add_argument("--shortlist-size", type=int, default=10)
    parser.add_argument("--max-proposals", type=int, default=3)
    parser.add_argument("--cv-works", type=int, default=2)
    parser.add_argument("--min-profile-works", type=int, default=8)
    parser.add_argument("--timing-repeats", type=int, default=3)
    parser.add_argument("--bootstrap-samples", type=int, default=4000)
    parser.add_argument("--min-ndcg-gain", type=float, default=0.005)
    parser.add_argument("--max-hit5-loss", type=float, default=0.01)
    parser.add_argument("--seed", type=int, default=20260712)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = run_experiment(args)
    print(
        json.dumps(
            {
                "status": result["status"],
                "decision": result["decision"],
                "output_dir": str(Path(args.output_dir).resolve()),
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
