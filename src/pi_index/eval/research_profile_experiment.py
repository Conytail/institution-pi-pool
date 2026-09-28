from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import statistics
import time
from typing import Any, Iterable
from difflib import SequenceMatcher
import unicodedata

import requests

from ..match.semantic import LOW_SIGNAL_TERMS, STOPWORDS, semantic_vector as production_semantic_vector


TOKEN_RE = re.compile(r"[a-z][a-z0-9+-]{2,}")
NAME_RE = re.compile(r"[^a-z0-9]+")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    return " ".join(NAME_RE.sub(" ", folded.lower()).split())


def valid_evaluation_name(value: str) -> bool:
    name = (value or "").strip()
    if not 4 <= len(name) <= 80 or any(character.isdigit() for character in name):
        return False
    if any(not (character.isalpha() or character in " .'-") for character in name):
        return False
    words = [word for word in re.split(r"[ .'-]+", name) if word]
    return len(words) >= 2 and sum(character.isalpha() for character in name) >= 4


def name_similarity(left: str, right: str) -> float:
    left_norm = normalize_name(left)
    right_norm = normalize_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_parts = left_norm.split()
    right_parts = right_norm.split()
    if left_parts[-1] != right_parts[-1]:
        return 0.0
    sequence = SequenceMatcher(None, left_norm, right_norm).ratio()
    overlap = len(set(left_parts).intersection(right_parts)) / max(len(set(left_parts)), 1)
    return round(0.65 * sequence + 0.35 * overlap, 4)


def semantic_tokens(text: str) -> list[str]:
    return [
        token
        for token in TOKEN_RE.findall((text or "").lower())
        if token not in STOPWORDS
    ]


def term_counts(text: str) -> Counter[str]:
    tokens = semantic_tokens(text)
    counts: Counter[str] = Counter()
    for token in tokens:
        counts[token] += 0.25 if token in LOW_SIGNAL_TERMS else 1.0
    for index in range(len(tokens) - 1):
        left, right = tokens[index : index + 2]
        if left in LOW_SIGNAL_TERMS and right in LOW_SIGNAL_TERMS:
            continue
        counts[f"{left}_{right}"] += 1.35
    return counts


SparseVector = dict[str, float]


def normalize_vector(vector: SparseVector, limit: int | None = None) -> SparseVector:
    items = vector.items()
    if limit is not None:
        items = sorted(items, key=lambda item: (-abs(item[1]), item[0]))[:limit]
    result = {key: float(value) for key, value in items if value}
    norm = math.sqrt(sum(value * value for value in result.values()))
    if norm <= 0:
        return {}
    return {key: value / norm for key, value in result.items()}


def cosine(left: SparseVector, right: SparseVector) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(term, 0.0) for term, value in left.items())


def centroid(vectors: Iterable[SparseVector], limit: int) -> SparseVector:
    vectors = list(vectors)
    if not vectors:
        return {}
    combined: defaultdict[str, float] = defaultdict(float)
    for vector in vectors:
        for term, value in vector.items():
            combined[term] += value
    scale = 1.0 / len(vectors)
    return normalize_vector({term: value * scale for term, value in combined.items()}, limit)


def tfidf_vector(text: str, idf: dict[str, float]) -> SparseVector:
    counts = term_counts(text)
    if not counts:
        return {}
    weighted = {
        term: (1.0 + math.log(value)) * idf.get(term, 1.0)
        for term, value in counts.items()
        if value > 0
    }
    return normalize_vector(weighted)


def reconstruct_abstract(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    positioned: list[tuple[int, str]] = []
    for token, positions in index.items():
        positioned.extend((int(position), token) for position in positions)
    return " ".join(token for _position, token in sorted(positioned))


def work_text(work: dict[str, Any]) -> str:
    topics = " ".join(work.get("topics") or [])
    return " ".join(
        part for part in [work.get("title") or "", work.get("abstract") or "", topics] if part
    )


@dataclass(frozen=True)
class ProfileConfig:
    mode: str
    feature_limit: int
    recent_years: int
    cluster_count: str
    representatives_per_cluster: int
    paper_scope: str

    @property
    def config_id(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


@dataclass
class BuiltProfile:
    career: SparseVector
    recent: SparseVector
    clusters: list[SparseVector]
    representative_vectors: list[SparseVector]
    all_work_vectors: list[SparseVector]
    fingerprint_bytes: int


def _farthest_first(vectors: list[SparseVector], k: int) -> list[SparseVector]:
    if not vectors:
        return []
    selected = [vectors[0]]
    selected_indexes = {0}
    while len(selected) < min(k, len(vectors)):
        best_index = None
        best_distance = -1.0
        for index, vector in enumerate(vectors):
            if index in selected_indexes:
                continue
            distance = 1.0 - max(cosine(vector, current) for current in selected)
            if distance > best_distance:
                best_distance = distance
                best_index = index
        if best_index is None:
            break
        selected_indexes.add(best_index)
        selected.append(vectors[best_index])
    return selected


def spherical_kmeans(
    vectors: list[SparseVector],
    cluster_count: int,
    feature_limit: int,
    max_iterations: int = 4,
) -> tuple[list[SparseVector], list[int]]:
    if not vectors:
        return [], []
    k = max(1, min(cluster_count, len(vectors)))
    centers = _farthest_first(vectors, k)
    assignments = [-1] * len(vectors)
    for _iteration in range(max_iterations):
        next_assignments = [
            max(range(len(centers)), key=lambda index: cosine(vector, centers[index]))
            for vector in vectors
        ]
        if next_assignments == assignments:
            break
        assignments = next_assignments
        next_centers: list[SparseVector] = []
        remap: dict[int, int] = {}
        for cluster_index in range(len(centers)):
            members = [
                vector
                for vector, assignment in zip(vectors, assignments)
                if assignment == cluster_index
            ]
            if not members:
                continue
            remap[cluster_index] = len(next_centers)
            next_centers.append(centroid(members, feature_limit))
        assignments = [remap[assignment] for assignment in assignments]
        centers = next_centers
    return centers, assignments


def resolved_cluster_count(value: str, work_count: int) -> int:
    if value == "0":
        return 0
    if value == "adaptive":
        return max(2, min(12, int(round(math.sqrt(max(work_count, 1))))))
    return max(0, int(value))


def _vector_bytes(vector: SparseVector) -> int:
    return 32 + 8 * len(vector)


def build_profile(
    works: list[dict[str, Any]],
    vectors_by_work: dict[str, SparseVector],
    config: ProfileConfig,
) -> BuiltProfile:
    pairs = [
        (work, normalize_vector(vectors_by_work.get(work["id"], {}), config.feature_limit))
        for work in works
        if vectors_by_work.get(work["id"])
    ]
    vectors = [vector for _work, vector in pairs]
    career = centroid(vectors, config.feature_limit)
    latest_year = max((int(work.get("year") or 0) for work, _vector in pairs), default=0)
    cutoff = latest_year - config.recent_years + 1
    recent_vectors = [
        vector
        for work, vector in pairs
        if int(work.get("year") or 0) >= cutoff
    ]
    recent = centroid(recent_vectors or vectors, config.feature_limit)

    clusters: list[SparseVector] = []
    representatives: list[SparseVector] = []
    k = resolved_cluster_count(config.cluster_count, len(vectors))
    if config.mode == "career_recent_clusters" and k:
        cluster_vectors = vectors[:60]
        clusters, assignments = spherical_kmeans(cluster_vectors, k, config.feature_limit)
        for cluster_index, center in enumerate(clusters):
            members = [
                vector
                for vector, assignment in zip(cluster_vectors, assignments)
                if assignment == cluster_index
            ]
            ranked = sorted(members, key=lambda vector: cosine(vector, center), reverse=True)
            representatives.extend(ranked[: config.representatives_per_cluster])

    fingerprints = sum(96 + len((work.get("title") or "").encode("utf-8")) for work, _ in pairs)
    return BuiltProfile(
        career=career,
        recent=recent,
        clusters=clusters,
        representative_vectors=representatives,
        all_work_vectors=vectors,
        fingerprint_bytes=fingerprints,
    )


def profile_storage_components(profile: BuiltProfile, config: ProfileConfig) -> tuple[int, int]:
    research_profile = _vector_bytes(profile.career)
    if config.mode in {"career_recent", "career_recent_clusters"}:
        research_profile += _vector_bytes(profile.recent)
    if config.mode == "career_recent_clusters":
        research_profile += sum(_vector_bytes(vector) for vector in profile.clusters)
    if config.paper_scope == "representative":
        research_profile += sum(_vector_bytes(vector) for vector in profile.representative_vectors)
    elif config.paper_scope == "all":
        research_profile += sum(_vector_bytes(vector) for vector in profile.all_work_vectors)
    return profile.fingerprint_bytes, research_profile


def profile_storage_bytes(profile: BuiltProfile, config: ProfileConfig) -> int:
    manifest_bytes, research_profile_bytes = profile_storage_components(profile, config)
    return manifest_bytes + research_profile_bytes


def build_recent_vector(
    works: list[dict[str, Any]],
    vectors_by_work: dict[str, SparseVector],
    recent_years: int,
    feature_limit: int,
) -> SparseVector:
    pairs = [
        (work, normalize_vector(vectors_by_work.get(work["id"], {}), feature_limit))
        for work in works
        if vectors_by_work.get(work["id"])
    ]
    latest_year = max((int(work.get("year") or 0) for work, _vector in pairs), default=0)
    cutoff = latest_year - recent_years + 1
    vectors = [vector for work, vector in pairs if int(work.get("year") or 0) >= cutoff]
    return centroid(vectors or [vector for _work, vector in pairs], feature_limit)


def profile_score(
    proposal_vector: SparseVector,
    cv_vector: SparseVector,
    profile: BuiltProfile,
    config: ProfileConfig,
) -> float:
    proposal_components = [cosine(proposal_vector, profile.career)]
    cv_components = [cosine(cv_vector, profile.career)]
    if config.mode in {"career_recent", "career_recent_clusters"}:
        proposal_components.append(cosine(proposal_vector, profile.recent))
    if config.mode == "career_recent_clusters":
        proposal_components.extend(cosine(proposal_vector, cluster) for cluster in profile.clusters)
        cv_components.extend(cosine(cv_vector, cluster) for cluster in profile.clusters)
    semantic = 0.8 * max(proposal_components, default=0.0) + 0.2 * max(cv_components, default=0.0)

    paper_vectors: list[SparseVector] = []
    if config.paper_scope == "representative":
        paper_vectors = profile.representative_vectors
    elif config.paper_scope == "all":
        paper_vectors = profile.all_work_vectors
    similarities = sorted(
        (cosine(proposal_vector, vector) for vector in paper_vectors), reverse=True
    )[:3]
    paper = sum(similarities) / len(similarities) if similarities else 0.0
    return max(semantic, paper)


def _institution_rors(author: dict[str, Any]) -> set[str]:
    rors: set[str] = set()
    for affiliation in author.get("affiliations") or []:
        ror = (affiliation.get("institution") or {}).get("ror")
        if ror:
            rors.add(ror)
    for institution in author.get("last_known_institutions") or []:
        ror = institution.get("ror")
        if ror:
            rors.add(ror)
    return rors


def identity_candidate_score(pi: dict[str, Any], author: dict[str, Any]) -> float:
    names = [author.get("display_name") or "", *(author.get("display_name_alternatives") or [])]
    best_name = max((name_similarity(pi["display_name"], name) for name in names), default=0.0)
    affiliation = 1.0 if pi["ror_id"] in _institution_rors(author) else 0.0
    return round(0.7 * best_name + 0.3 * affiliation, 4)


class CachedOpenAlex:
    def __init__(self, cache_dir: Path, timeout: int = 30, min_interval: float = 0.12):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.min_interval = min_interval
        self.session = requests.Session()
        self.api_key = os.getenv("OPENALEX_API_KEY")
        self.last_request_at = 0.0
        self.remaining_credits: int | None = None
        self.network_requests = 0
        self.cache_hits = 0

    def get(self, endpoint: str, params: dict[str, Any], cache_key: str) -> dict[str, Any]:
        path = self.cache_dir / f"{cache_key}.json"
        if path.exists():
            self.cache_hits += 1
            return json.loads(path.read_text(encoding="utf-8"))
        request_params = dict(params)
        if self.api_key:
            request_params["api_key"] = self.api_key
        wait = self.min_interval - (time.monotonic() - self.last_request_at)
        if wait > 0:
            time.sleep(wait)
        url = f"https://api.openalex.org/{endpoint.lstrip('/')}"
        for attempt in range(5):
            self.network_requests += 1
            try:
                response = self.session.get(url, params=request_params, timeout=self.timeout)
            except requests.RequestException:
                time.sleep(2**attempt)
                continue
            self.last_request_at = time.monotonic()
            remaining = response.headers.get("X-RateLimit-Remaining")
            if remaining and remaining.isdigit():
                self.remaining_credits = int(remaining)
            if response.status_code == 200:
                payload = response.json()
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return payload
            if response.status_code in {403, 429} or response.status_code >= 500:
                time.sleep(2**attempt)
                continue
            response.raise_for_status()
        raise RuntimeError(f"OpenAlex request failed after retries: {endpoint} {request_params}")


def _load_sample_pis(
    db_path: Path,
    max_per_institution: int,
    seed: int,
    official_pool_mode: bool = False,
) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT p.person_id, p.display_name, p.institution_id, p.institution_name,
               p.title, p.research_areas_json, p.profile_url,
               c.current_affiliation_confidence, i.ror_id
        FROM canonical_pi_records p
        JOIN institutions i ON i.institution_id=p.institution_id
        JOIN contact_verdicts c ON c.person_id=p.person_id
        WHERE COALESCE(p.membership_status, 'active')!='inactive'
          AND (?=1 OR c.current_affiliation_confidence='high')
        ORDER BY p.institution_name, p.person_id
        """,
        (1 if official_pool_mode else 0,),
    ).fetchall()
    conn.close()
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        data = dict(row)
        name = data["display_name"] or ""
        if not valid_evaluation_name(name):
            continue
        data["research_areas"] = json.loads(data.pop("research_areas_json") or "[]")
        grouped[data["institution_id"]].append(data)
    selected: list[dict[str, Any]] = []
    for institution_id in sorted(grouped):
        candidates = grouped[institution_id]
        candidates.sort(
            key=lambda row: hashlib.sha256(
                f"{seed}:{row['person_id']}".encode("utf-8")
            ).hexdigest()
        )
        selected.extend(candidates[:max_per_institution])
    return selected


def _resolve_author(client: CachedOpenAlex, pi: dict[str, Any]) -> dict[str, Any] | None:
    cache_key = f"author_search_{hashlib.sha256((pi['person_id'] + pi['display_name']).encode()).hexdigest()[:16]}"
    payload = client.get(
        "authors",
        {
            "search": pi["display_name"],
            "per_page": 10,
            "select": "id,orcid,display_name,display_name_alternatives,works_count,affiliations,last_known_institutions,updated_date",
        },
        cache_key,
    )
    return _select_author_candidate(pi, payload.get("results") or [])


def _select_author_candidate(
    pi: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    scored = sorted(
        (
            (identity_candidate_score(pi, author), author)
            for author in candidates
        ),
        key=lambda item: (item[0], int(item[1].get("works_count") or 0)),
        reverse=True,
    )
    if not scored:
        return None
    score, author = scored[0]
    name_score = max(
        [
            name_similarity(pi["display_name"], author.get("display_name") or ""),
            *[
                name_similarity(pi["display_name"], alias)
                for alias in author.get("display_name_alternatives") or []
            ],
        ]
    )
    affiliation_match = pi["ror_id"] in _institution_rors(author)
    if score < 0.88 or name_score < 0.83 or not affiliation_match:
        return None
    return {**author, "identity_score": score, "name_score": round(name_score, 4)}


def _batch_search_name(value: str) -> str:
    cleaned = "".join(
        character if character.isalpha() or character in " .'-" else " "
        for character in (value or "")
    )
    return " ".join(cleaned.split())


def _resolve_author_batch(
    client: CachedOpenAlex,
    pis: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not pis:
        return {}
    ror_id = pis[0].get("ror_id")
    if not ror_id or any(pi.get("ror_id") != ror_id for pi in pis):
        raise ValueError("Batch identity resolution requires one shared institution ROR")
    names = sorted({_batch_search_name(pi["display_name"]) for pi in pis if pi.get("display_name")})
    names = [name for name in names if name]
    if not names:
        return {}
    digest = hashlib.sha256((ror_id + "\n" + "\n".join(names)).encode("utf-8")).hexdigest()[:20]
    payload = client.get(
        "authors",
        {
            "filter": (
                f"affiliations.institution.ror:{ror_id},"
                f"display_name.search:{'|'.join(names)}"
            ),
            "per_page": 100,
            "select": (
                "id,orcid,display_name,display_name_alternatives,works_count,"
                "affiliations,last_known_institutions,updated_date"
            ),
        },
        f"author_batch_{digest}",
    )
    candidates = payload.get("results") or []
    return {
        pi["person_id"]: author
        for pi in pis
        if (author := _select_author_candidate(pi, candidates)) is not None
    }


def _fetch_author_works(
    client: CachedOpenAlex,
    author_id: str,
    start_year: int,
    max_works: int,
) -> list[dict[str, Any]]:
    short_id = author_id.rstrip("/").split("/")[-1]
    payload = client.get(
        "works",
        {
            "filter": f"author.id:{short_id},from_publication_date:{start_year}-01-01",
            "per_page": min(100, max_works),
            "sort": "publication_date:desc",
            "select": (
                "id,doi,title,publication_year,publication_date,abstract_inverted_index,"
                "topics,authorships,type,primary_location,updated_date"
            ),
        },
        f"works_{short_id}_{start_year}_{max_works}",
    )
    works: list[dict[str, Any]] = []
    for raw in payload.get("results") or []:
        title = raw.get("title") or ""
        year = int(raw.get("publication_year") or 0)
        if not title or not year:
            continue
        author_ids = [
            ((authorship.get("author") or {}).get("id") or "").rstrip("/").split("/")[-1]
            for authorship in raw.get("authorships") or []
        ]
        if short_id not in author_ids:
            continue
        works.append(
            {
                "id": raw.get("id"),
                "doi": raw.get("doi"),
                "title": title,
                "year": year,
                "publication_date": raw.get("publication_date"),
                "abstract": reconstruct_abstract(raw.get("abstract_inverted_index")),
                "topics": [topic.get("display_name") for topic in raw.get("topics") or [] if topic.get("display_name")],
                "author_ids": author_ids,
                "updated_date": raw.get("updated_date"),
            }
        )
    return works


def prepare_dataset(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    client = CachedOpenAlex(output_dir / "openalex_cache")
    official_pool_mode = bool(getattr(args, "official_pool_mode", False))
    batch_identity = bool(getattr(args, "batch_identity", False))
    sampled = _load_sample_pis(
        Path(args.db),
        args.max_pis_per_institution,
        args.seed,
        official_pool_mode=official_pool_mode,
    )
    identity_by_pi: dict[str, dict[str, Any]] = {}
    identity_batch_errors: list[dict[str, str]] = []
    if batch_identity:
        grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for pi in sampled:
            grouped[pi["institution_id"]].append(pi)
        for institution_id in sorted(grouped):
            if client.remaining_credits is not None and client.remaining_credits < args.reserve_credits:
                break
            try:
                identity_by_pi.update(_resolve_author_batch(client, grouped[institution_id]))
            except Exception as exc:
                identity_batch_errors.append(
                    {
                        "institution_id": institution_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    else:
        for pi in sampled:
            if client.remaining_credits is not None and client.remaining_credits < args.reserve_credits:
                break
            author = _resolve_author(client, pi)
            if author:
                identity_by_pi[pi["person_id"]] = author

    resolved: list[dict[str, Any]] = []
    identity_audit: dict[str, dict[str, Any]] = {
        pi["person_id"]: {
            "person_id": pi["person_id"],
            "display_name": pi["display_name"],
            "institution_id": pi["institution_id"],
            "institution_name": pi["institution_name"],
            "ror_id": pi.get("ror_id"),
            "official_profile_url": pi.get("profile_url"),
            "official_pool_mode": official_pool_mode,
            "status": "identity_not_resolved",
        }
        for pi in sampled
    }
    resolved_candidates: defaultdict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for pi in sampled:
        author = identity_by_pi.get(pi["person_id"])
        if author:
            resolved_candidates[pi["institution_id"]].append((pi, author))
            identity_audit[pi["person_id"]].update(
                {
                    "status": "identity_resolved",
                    "openalex_author_id": author["id"],
                    "openalex_orcid": author.get("orcid"),
                    "identity_score": author["identity_score"],
                    "name_score": author["name_score"],
                }
            )

    ordered_institutions = sorted(resolved_candidates)
    max_rounds = max((len(items) for items in resolved_candidates.values()), default=0)
    stopped_for_credits = False
    attempted_works = 0
    for round_index in range(max_rounds):
        for institution_id in ordered_institutions:
            items = resolved_candidates[institution_id]
            if round_index >= len(items):
                continue
            if client.remaining_credits is not None and client.remaining_credits < args.reserve_credits:
                stopped_for_credits = True
                break
            pi, author = items[round_index]
            attempted_works += 1
            try:
                works = _fetch_author_works(client, author["id"], args.start_year, args.max_works)
            except Exception as exc:
                identity_audit[pi["person_id"]]["status"] = "works_fetch_failed"
                identity_audit[pi["person_id"]]["error"] = f"{type(exc).__name__}: {exc}"
                continue
            if len(works) < args.min_works:
                identity_audit[pi["person_id"]]["status"] = "insufficient_works"
                identity_audit[pi["person_id"]]["retrieved_work_count"] = len(works)
                continue
            resolved.append(
                {
                    **pi,
                    "openalex_author_id": author["id"],
                    "openalex_orcid": author.get("orcid"),
                    "identity_score": author["identity_score"],
                    "openalex_works_count": author.get("works_count"),
                    "works": works,
                }
            )
            identity_audit[pi["person_id"]]["status"] = "included"
            identity_audit[pi["person_id"]]["retrieved_work_count"] = len(works)
            print(
                f"[{attempted_works}/{len(identity_by_pi)}] "
                f"{pi['institution_name']} :: {pi['display_name']} -> {len(works)} works",
                flush=True,
            )
        if stopped_for_credits:
            break
    identity_path = output_dir / "identity_resolution.jsonl"
    with identity_path.open("w", encoding="utf-8") as handle:
        for pi in sampled:
            handle.write(json.dumps(identity_audit[pi["person_id"]], ensure_ascii=False) + "\n")
    dataset = {
        "generated_at": utc_now_iso(),
        "source_db": str(Path(args.db).resolve()),
        "seed": args.seed,
        "start_year": args.start_year,
        "sampled_pi_count": len(sampled),
        "identity_resolved_pi_count": len(identity_by_pi),
        "identity_batch_errors": identity_batch_errors,
        "resolved_pi_count": len(resolved),
        "official_pool_mode": official_pool_mode,
        "batch_identity": batch_identity,
        "stopped_for_credits": stopped_for_credits,
        "identity_audit": str(identity_path.resolve()),
        "openalex_network_requests": client.network_requests,
        "openalex_cache_hits": client.cache_hits,
        "remaining_openalex_credits": client.remaining_credits,
        "pis": resolved,
    }
    dataset_path = output_dir / "research_profile_dataset.json"
    dataset_path.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    return dataset


def build_cases(
    pis: list[dict[str, Any]],
    holdout_proposals: int,
    holdout_cv_works: int,
    min_profile_works: int,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], set[str]]:
    heldout_ids: set[str] = set()
    case_specs: list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    for pi in pis:
        eligible = [work for work in pi["works"] if len(semantic_tokens(work_text(work))) >= 6]
        eligible.sort(key=lambda work: (int(work.get("year") or 0), work.get("id") or ""), reverse=True)
        needed = holdout_proposals + holdout_cv_works
        if len(eligible) < min_profile_works + needed:
            continue
        proposals = eligible[:holdout_proposals]
        cv_works = eligible[holdout_proposals:needed]
        for work in proposals + cv_works:
            heldout_ids.add(work["id"])
        case_specs.append((pi, proposals, cv_works))

    profiles: dict[str, list[dict[str, Any]]] = {}
    retained_pi_ids: set[str] = set()
    for pi in pis:
        training = [work for work in pi["works"] if work["id"] not in heldout_ids]
        if len(training) >= min_profile_works:
            profiles[pi["person_id"]] = training
            retained_pi_ids.add(pi["person_id"])

    author_to_pi = {
        pi["openalex_author_id"].rstrip("/").split("/")[-1]: pi["person_id"]
        for pi in pis
        if pi["person_id"] in retained_pi_ids
    }
    pi_by_id = {pi["person_id"]: pi for pi in pis}
    cases: list[dict[str, Any]] = []
    for pi, proposal_works, cv_works in case_specs:
        if pi["person_id"] not in retained_pi_ids:
            continue
        cv_text = " ".join(work_text(work) for work in cv_works)
        for proposal_work in proposal_works:
            positives = sorted(
                {
                    author_to_pi[author_id]
                    for author_id in proposal_work.get("author_ids") or []
                    if author_id in author_to_pi
                    and pi_by_id[author_to_pi[author_id]]["institution_id"] == pi["institution_id"]
                }
            )
            if pi["person_id"] not in positives:
                positives.append(pi["person_id"])
            cases.append(
                {
                    "case_id": f"{pi['person_id']}::{proposal_work['id'].split('/')[-1]}",
                    "source_pi_id": pi["person_id"],
                    "institution_id": pi["institution_id"],
                    "institution_name": pi["institution_name"],
                    "proposal_work_id": proposal_work["id"],
                    "proposal_text": work_text(proposal_work),
                    "cv_text": cv_text,
                    "positive_pi_ids": positives,
                }
            )
    return cases, profiles, heldout_ids


def compute_idf(profile_works: dict[str, list[dict[str, Any]]]) -> dict[str, float]:
    document_frequency: Counter[str] = Counter()
    document_count = 0
    for works in profile_works.values():
        for work in works:
            document_count += 1
            document_frequency.update(set(term_counts(work_text(work))))
    return {
        term: math.log((1 + document_count) / (1 + frequency)) + 1.0
        for term, frequency in document_frequency.items()
    }


def encode_experiment_texts(
    profile_works: dict[str, list[dict[str, Any]]],
    cases: list[dict[str, Any]],
    encoder: str,
) -> tuple[dict[str, SparseVector], dict[str, tuple[SparseVector, SparseVector]]]:
    if encoder == "tfidf":
        idf = compute_idf(profile_works)

        def encode(text: str) -> SparseVector:
            return tfidf_vector(text, idf)

    elif encoder == "production_terms":

        def encode(text: str) -> SparseVector:
            return normalize_vector(production_semantic_vector(text, max_features=1024))

    else:
        raise ValueError(f"unsupported encoder: {encoder}")

    vectors_by_work: dict[str, SparseVector] = {}
    for works in profile_works.values():
        for work in works:
            vectors_by_work[work["id"]] = encode(work_text(work))
    query_vectors = {
        case["case_id"]: (encode(case["proposal_text"]), encode(case["cv_text"]))
        for case in cases
    }
    return vectors_by_work, query_vectors


def generate_configs() -> list[ProfileConfig]:
    configs: list[ProfileConfig] = []
    for feature_limit in (64, 128, 256):
        configs.append(ProfileConfig("career", feature_limit, 5, "0", 0, "none"))
        for recent_years in (3, 5, 8):
            configs.append(ProfileConfig("career_recent", feature_limit, recent_years, "0", 0, "none"))
            for cluster_count in ("3", "5", "8", "adaptive"):
                configs.append(
                    ProfileConfig(
                        "career_recent_clusters",
                        feature_limit,
                        recent_years,
                        cluster_count,
                        0,
                        "none",
                    )
                )
    for cluster_count in ("3", "5", "8", "adaptive"):
        configs.append(
            ProfileConfig(
                "career_recent_clusters",
                128,
                5,
                cluster_count,
                0,
                "all",
            )
        )
        for representatives in (1, 3, 5):
            configs.append(
                ProfileConfig(
                    "career_recent_clusters",
                    128,
                    5,
                    cluster_count,
                    representatives,
                    "representative",
                )
            )
    for feature_limit, recent_years, cluster_count in (
        (64, 3, "adaptive"),
        (256, 3, "8"),
    ):
        configs.append(
            ProfileConfig(
                "career_recent_clusters",
                feature_limit,
                recent_years,
                cluster_count,
                0,
                "all",
            )
        )
        for representatives in (1, 3, 5):
            configs.append(
                ProfileConfig(
                    "career_recent_clusters",
                    feature_limit,
                    recent_years,
                    cluster_count,
                    representatives,
                    "representative",
                )
            )
    unique = {config.config_id: config for config in configs}
    return [unique[key] for key in sorted(unique)]


def reciprocal_rank(ranked: list[str], positives: set[str]) -> float:
    for index, pi_id in enumerate(ranked, start=1):
        if pi_id in positives:
            return 1.0 / index
    return 0.0


def ndcg_at_k(ranked: list[str], positives: set[str], k: int) -> float:
    if not positives:
        return 0.0
    dcg = sum(
        1.0 / math.log2(index + 2)
        for index, pi_id in enumerate(ranked[:k])
        if pi_id in positives
    )
    ideal_hits = min(len(positives), k)
    ideal = sum(1.0 / math.log2(index + 2) for index in range(ideal_hits))
    return dcg / ideal if ideal else 0.0


def summarize_case_rows(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {
            "cases": 0,
            "mrr": 0.0,
            "ndcg_at_10": 0.0,
            "recall_at_1": 0.0,
            "recall_at_3": 0.0,
            "recall_at_5": 0.0,
            "recall_at_10": 0.0,
            "mean_rank": 0.0,
            "ndcg_se": 0.0,
            "source_count": 0,
        }
    ndcgs = [row["ndcg_at_10"] for row in rows]
    by_source: defaultdict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_source[row["source_pi_id"]].append(row["ndcg_at_10"])
    source_ndcgs = [statistics.fmean(values) for values in by_source.values()]
    return {
        "cases": len(rows),
        "mrr": statistics.fmean(row["reciprocal_rank"] for row in rows),
        "ndcg_at_10": statistics.fmean(ndcgs),
        "recall_at_1": statistics.fmean(row["rank"] <= 1 for row in rows),
        "recall_at_3": statistics.fmean(row["rank"] <= 3 for row in rows),
        "recall_at_5": statistics.fmean(row["rank"] <= 5 for row in rows),
        "recall_at_10": statistics.fmean(row["rank"] <= 10 for row in rows),
        "mean_rank": statistics.fmean(row["rank"] for row in rows),
        "ndcg_se": (
            statistics.stdev(source_ndcgs) / math.sqrt(len(source_ndcgs))
            if len(source_ndcgs) > 1
            else 0.0
        ),
        "source_count": len(source_ndcgs),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    import csv

    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def select_one_se(results: list[dict[str, Any]]) -> dict[str, Any]:
    best = max(results, key=lambda row: (row["ndcg_at_10"], row["recall_at_5"], -row["storage_bytes_per_pi"]))
    threshold = best["ndcg_at_10"] - best["ndcg_se"]
    recall_floor = best["recall_at_5"] - 0.02
    eligible = [
        row
        for row in results
        if row["ndcg_at_10"] >= threshold and row["recall_at_5"] >= recall_floor
    ]
    return min(
        eligible,
        key=lambda row: (
            row["storage_bytes_per_pi"],
            row["query_ms_per_case"],
            -row["ndcg_at_10"],
        ),
    )


def leave_one_institution_out(
    config_results: list[dict[str, Any]],
    all_case_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    institutions = sorted({row["institution_name"] for row in all_case_rows})
    by_config: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in all_case_rows:
        by_config[row["config_id"]].append(row)
    output: list[dict[str, Any]] = []
    for heldout in institutions:
        training_results: list[dict[str, Any]] = []
        for config in config_results:
            training_rows = [
                row
                for row in by_config[config["config_id"]]
                if row["institution_name"] != heldout
            ]
            training_results.append({**config, **summarize_case_rows(training_rows)})
        selected = select_one_se(training_results)
        heldout_rows = [
            row
            for row in by_config[selected["config_id"]]
            if row["institution_name"] == heldout
        ]
        heldout_summary = summarize_case_rows(heldout_rows)
        output.append(
            {
                "heldout_institution": heldout,
                "selected_config_id": selected["config_id"],
                "selected_mode": selected["mode"],
                "selected_feature_limit": selected["feature_limit"],
                "selected_recent_years": selected["recent_years"],
                "selected_cluster_count": selected["cluster_count"],
                "selected_representatives_per_cluster": selected["representatives_per_cluster"],
                "selected_paper_scope": selected["paper_scope"],
                "train_ndcg_at_10": selected["ndcg_at_10"],
                "train_recall_at_5": selected["recall_at_5"],
                "storage_bytes_per_pi": selected["storage_bytes_per_pi"],
                **{f"heldout_{key}": value for key, value in heldout_summary.items()},
            }
        )
    return output


def paired_bootstrap_delta(
    all_case_rows: list[dict[str, Any]],
    left_config_id: str,
    right_config_id: str,
    samples: int = 4000,
    seed: int = 20260712,
) -> dict[str, float]:
    values: defaultdict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in all_case_rows:
        if row["config_id"] in {left_config_id, right_config_id}:
            values[row["source_pi_id"]][row["config_id"]].append(row["ndcg_at_10"])
    deltas = [
        statistics.fmean(group[left_config_id]) - statistics.fmean(group[right_config_id])
        for group in values.values()
        if group[left_config_id] and group[right_config_id]
    ]
    if not deltas:
        return {"mean_delta": 0.0, "ci_low": 0.0, "ci_high": 0.0, "source_count": 0}
    rng = random.Random(seed)
    bootstrap = sorted(
        statistics.fmean(rng.choice(deltas) for _index in range(len(deltas)))
        for _sample in range(samples)
    )
    low_index = int(0.025 * (len(bootstrap) - 1))
    high_index = int(0.975 * (len(bootstrap) - 1))
    return {
        "mean_delta": statistics.fmean(deltas),
        "ci_low": bootstrap[low_index],
        "ci_high": bootstrap[high_index],
        "source_count": len(deltas),
    }


CONFIG_FIELDS = (
    "mode",
    "feature_limit",
    "recent_years",
    "cluster_count",
    "representatives_per_cluster",
    "paper_scope",
)


def compare_experiment_runs(args: argparse.Namespace) -> dict[str, Any]:
    import csv

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    runs: dict[str, dict[str, dict[str, str]]] = {}
    manifests: dict[str, dict[str, Any]] = {}
    for specification in args.runs:
        if "=" not in specification:
            raise ValueError("each --runs value must use encoder=run_directory")
        encoder, raw_path = specification.split("=", 1)
        run_dir = Path(raw_path)
        with (run_dir / "config_results.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        runs[encoder] = {row["config_id"]: row for row in rows}
        manifests[encoder] = json.loads(
            (run_dir / "experiment_manifest.json").read_text(encoding="utf-8")
        )

    common_ids = set.intersection(*(set(rows) for rows in runs.values()))
    best_by_encoder: dict[str, dict[str, str]] = {}
    thresholds: dict[str, dict[str, float]] = {}
    for encoder, rows in runs.items():
        best = max(
            (rows[config_id] for config_id in common_ids),
            key=lambda row: (
                float(row["ndcg_at_10"]),
                float(row["recall_at_5"]),
                -float(row["storage_bytes_per_pi"]),
            ),
        )
        best_by_encoder[encoder] = best
        thresholds[encoder] = {
            "ndcg_floor": float(best["ndcg_at_10"]) - float(best["ndcg_se"]),
            "recall_at_5_floor": float(best["recall_at_5"]) - 0.02,
        }

    comparison_rows: list[dict[str, Any]] = []
    for config_id in sorted(common_ids):
        encoder_rows = {encoder: rows[config_id] for encoder, rows in runs.items()}
        first = next(iter(encoder_rows.values()))
        ndcgs = [float(row["ndcg_at_10"]) for row in encoder_rows.values()]
        recalls = [float(row["recall_at_5"]) for row in encoder_rows.values()]
        profile_bytes = [float(row["research_profile_bytes_per_pi"]) for row in encoder_rows.values()]
        total_bytes = [float(row["storage_bytes_per_pi"]) for row in encoder_rows.values()]
        row: dict[str, Any] = {
            "config_id": config_id,
            **{field: first[field] for field in CONFIG_FIELDS},
            "mean_ndcg_at_10": statistics.fmean(ndcgs),
            "worst_ndcg_at_10": min(ndcgs),
            "max_ndcg_regret": max(
                float(best_by_encoder[encoder]["ndcg_at_10"])
                - float(encoder_row["ndcg_at_10"])
                for encoder, encoder_row in encoder_rows.items()
            ),
            "min_recall_at_5": min(recalls),
            "mean_research_profile_bytes_per_pi": statistics.fmean(profile_bytes),
            "mean_total_bytes_per_pi": statistics.fmean(total_bytes),
            "robust_one_se_eligible": all(
                float(encoder_row["ndcg_at_10"]) >= thresholds[encoder]["ndcg_floor"]
                and float(encoder_row["recall_at_5"])
                >= thresholds[encoder]["recall_at_5_floor"]
                for encoder, encoder_row in encoder_rows.items()
            ),
        }
        for encoder, encoder_row in encoder_rows.items():
            row[f"{encoder}_ndcg_at_10"] = float(encoder_row["ndcg_at_10"])
            row[f"{encoder}_recall_at_5"] = float(encoder_row["recall_at_5"])
            row[f"{encoder}_mrr"] = float(encoder_row["mrr"])
        comparison_rows.append(row)

    eligible = [row for row in comparison_rows if row["robust_one_se_eligible"]]
    robust_selected = min(
        eligible,
        key=lambda row: (
            row["mean_research_profile_bytes_per_pi"],
            -row["worst_ndcg_at_10"],
        ),
    )
    comparison_rows.sort(
        key=lambda row: (
            not row["robust_one_se_eligible"],
            row["mean_research_profile_bytes_per_pi"],
            -row["mean_ndcg_at_10"],
        )
    )
    _write_csv(output_dir / "cross_encoder_results.csv", comparison_rows)

    ablation_rows: list[dict[str, Any]] = []
    for encoder, rows in runs.items():
        structural = {
            tuple(row[field] for field in CONFIG_FIELDS[:4]): row
            for row in rows.values()
            if row["paper_scope"] == "none"
        }
        for scope in ("representative", "all"):
            comparisons: list[tuple[float, float, float]] = []
            for row in rows.values():
                if row["paper_scope"] != scope:
                    continue
                baseline = structural.get(tuple(row[field] for field in CONFIG_FIELDS[:4]))
                if not baseline:
                    continue
                comparisons.append(
                    (
                        float(row["ndcg_at_10"]) - float(baseline["ndcg_at_10"]),
                        float(row["recall_at_5"]) - float(baseline["recall_at_5"]),
                        float(row["research_profile_bytes_per_pi"])
                        / max(float(baseline["research_profile_bytes_per_pi"]), 1.0),
                    )
                )
            ablation_rows.append(
                {
                    "encoder": encoder,
                    "paper_scope": scope,
                    "comparison_count": len(comparisons),
                    "mean_ndcg_delta": statistics.fmean(value[0] for value in comparisons),
                    "min_ndcg_delta": min(value[0] for value in comparisons),
                    "max_ndcg_delta": max(value[0] for value in comparisons),
                    "mean_recall_at_5_delta": statistics.fmean(value[1] for value in comparisons),
                    "mean_profile_storage_multiplier": statistics.fmean(
                        value[2] for value in comparisons
                    ),
                }
            )
    _write_csv(output_dir / "publication_ablation.csv", ablation_rows)

    manifest = {
        "generated_at": utc_now_iso(),
        "encoders": sorted(runs),
        "common_config_count": len(common_ids),
        "selection_rule": "minimum Research Profile bytes among configurations within each encoder's one-SE nDCG floor and 0.02 Recall@5 floor",
        "thresholds": thresholds,
        "per_encoder_selected": {
            encoder: manifest["selected_config"] for encoder, manifest in manifests.items()
        },
        "robust_selected_config": robust_selected,
        "publication_ablation": ablation_rows,
    }
    (output_dir / "comparison_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    pis = dataset.get("pis") or []
    cases, profile_works, heldout_ids = build_cases(
        pis,
        args.holdout_proposals,
        args.holdout_cv_works,
        args.min_profile_works,
    )
    pi_by_id = {pi["person_id"]: pi for pi in pis if pi["person_id"] in profile_works}
    institution_candidates: defaultdict[str, list[str]] = defaultdict(list)
    for pi_id in profile_works:
        institution_candidates[pi_by_id[pi_id]["institution_id"]].append(pi_id)
    vectors_by_work, query_vectors = encode_experiment_texts(
        profile_works,
        cases,
        args.encoder,
    )

    config_results: list[dict[str, Any]] = []
    all_case_rows: list[dict[str, Any]] = []
    per_institution_rows: list[dict[str, Any]] = []
    profile_cache: dict[tuple[Any, ...], tuple[dict[str, BuiltProfile], float]] = {}
    recent_cache: dict[tuple[str, int, int], SparseVector] = {}
    for config_index, config in enumerate(generate_configs(), start=1):
        structural_key = (
            config.mode,
            config.feature_limit,
            config.cluster_count,
            config.representatives_per_cluster,
        )
        if structural_key not in profile_cache:
            build_started = time.perf_counter()
            build_config = replace(config, recent_years=5, paper_scope="none")
            profiles = {
                pi_id: build_profile(works, vectors_by_work, build_config)
                for pi_id, works in profile_works.items()
            }
            build_ms = (time.perf_counter() - build_started) * 1000.0
            profile_cache[structural_key] = (profiles, build_ms)
        else:
            profiles, build_ms = profile_cache[structural_key]
        if config.mode in {"career_recent", "career_recent_clusters"} and config.recent_years != 5:
            recent_started = time.perf_counter()
            adjusted_profiles: dict[str, BuiltProfile] = {}
            for pi_id, profile in profiles.items():
                recent_key = (pi_id, config.feature_limit, config.recent_years)
                if recent_key not in recent_cache:
                    recent_cache[recent_key] = build_recent_vector(
                        profile_works[pi_id],
                        vectors_by_work,
                        config.recent_years,
                        config.feature_limit,
                    )
                adjusted_profiles[pi_id] = replace(profile, recent=recent_cache[recent_key])
            profiles = adjusted_profiles
            build_ms += (time.perf_counter() - recent_started) * 1000.0
        scoring_started = time.perf_counter()
        case_rows: list[dict[str, Any]] = []
        for case in cases:
            candidates = institution_candidates[case["institution_id"]]
            proposal_vector, cv_vector = query_vectors[case["case_id"]]
            scored = sorted(
                (
                    (
                        pi_id,
                        profile_score(proposal_vector, cv_vector, profiles[pi_id], config),
                    )
                    for pi_id in candidates
                ),
                key=lambda item: (item[1], item[0]),
                reverse=True,
            )
            ranked = [pi_id for pi_id, _score in scored]
            positives = set(case["positive_pi_ids"])
            rank = next((index for index, pi_id in enumerate(ranked, start=1) if pi_id in positives), len(ranked) + 1)
            case_rows.append(
                {
                    "config_id": config.config_id,
                    "case_id": case["case_id"],
                    "institution_id": case["institution_id"],
                    "institution_name": case["institution_name"],
                    "source_pi_id": case["source_pi_id"],
                    "rank": rank,
                    "reciprocal_rank": reciprocal_rank(ranked, positives),
                    "ndcg_at_10": ndcg_at_k(ranked, positives, 10),
                    "candidate_count": len(ranked),
                }
            )
        scoring_ms = (time.perf_counter() - scoring_started) * 1000.0
        summary = summarize_case_rows(case_rows)
        storage_components = [
            profile_storage_components(profile, config) for profile in profiles.values()
        ]
        manifest_values = [manifest for manifest, _research in storage_components]
        research_values = [research for _manifest, research in storage_components]
        storage_values = [manifest + research for manifest, research in storage_components]
        result = {
            "config_id": config.config_id,
            "encoder": args.encoder,
            **asdict(config),
            **summary,
            "pi_count": len(profiles),
            "institution_count": len(institution_candidates),
            "publication_manifest_bytes_per_pi": (
                round(statistics.fmean(manifest_values), 2) if manifest_values else 0.0
            ),
            "research_profile_bytes_per_pi": (
                round(statistics.fmean(research_values), 2) if research_values else 0.0
            ),
            "storage_bytes_per_pi": round(statistics.fmean(storage_values), 2) if storage_values else 0.0,
            "storage_total_bytes": sum(storage_values),
            "build_ms_per_pi": round(build_ms / max(len(profiles), 1), 4),
            "query_ms_per_case": round(scoring_ms / max(len(case_rows), 1), 4),
        }
        config_results.append(result)
        all_case_rows.extend(case_rows)
        grouped_rows: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in case_rows:
            grouped_rows[row["institution_name"]].append(row)
        for institution_name, rows in sorted(grouped_rows.items()):
            per_institution_rows.append(
                {
                    "config_id": config.config_id,
                    "institution_name": institution_name,
                    **summarize_case_rows(rows),
                }
            )
        if config_index % 25 == 0:
            print(f"evaluated {config_index} configs", flush=True)

    selected = select_one_se(config_results)
    best = max(
        config_results,
        key=lambda row: (row["ndcg_at_10"], row["recall_at_5"], -row["storage_bytes_per_pi"]),
    )
    selected_cases = [row for row in all_case_rows if row["config_id"] == selected["config_id"]]
    loso_rows = leave_one_institution_out(config_results, all_case_rows)
    paired_delta = paired_bootstrap_delta(
        all_case_rows,
        selected["config_id"],
        best["config_id"],
    )
    config_results.sort(key=lambda row: (-row["ndcg_at_10"], -row["recall_at_5"], row["storage_bytes_per_pi"]))
    _write_csv(output_dir / "config_results.csv", config_results)
    _write_csv(output_dir / "per_institution_results.csv", per_institution_rows)
    _write_csv(output_dir / "case_results.csv", all_case_rows)
    _write_csv(output_dir / "selected_config_cases.csv", selected_cases)
    _write_csv(output_dir / "leave_one_institution_out.csv", loso_rows)
    loso_selection_counts = Counter(row["selected_config_id"] for row in loso_rows)
    manifest = {
        "generated_at": utc_now_iso(),
        "dataset": str(Path(args.dataset).resolve()),
        "encoder": args.encoder,
        "dataset_pi_count": len(pis),
        "eligible_profile_pi_count": len(profile_works),
        "case_count": len(cases),
        "institution_candidate_counts": {
            pi_by_id[pi_ids[0]]["institution_name"]: len(pi_ids)
            for _institution_id, pi_ids in institution_candidates.items()
            if pi_ids
        },
        "heldout_work_count": len(heldout_ids),
        "config_count": len(config_results),
        "selection_rule": "minimum storage among configs within one source-clustered SE of best nDCG@10 and within 0.02 Recall@5",
        "best_accuracy_config": best,
        "selected_config": selected,
        "selected_minus_best_paired_bootstrap_ndcg": paired_delta,
        "leave_one_institution_out": loso_rows,
        "loso_selection_counts": dict(loso_selection_counts),
        "limitations": [
            "Self-supervised publication holdout measures profile resolution, not final human outreach preference.",
            f"The {args.encoder} encoder is frozen for this run; any future dense encoder must repeat the same ablation.",
            "OpenAlex identity links are restricted to exact/near-exact names plus matching official-institution ROR.",
            "This pilot contains 36 publication-linked PIs from three institutions; candidate pools contain 9 to 14 PIs.",
            "The 54 expected virtual applicant slots are empty locally, so no human-labelled external test was used for tuning.",
        ],
    }
    (output_dir / "experiment_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research Profile resolution benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Resolve official PIs and cache OpenAlex works")
    prepare.add_argument("--db", required=True)
    prepare.add_argument("--output-dir", required=True)
    prepare.add_argument("--max-pis-per-institution", type=int, default=18)
    prepare.add_argument("--min-works", type=int, default=12)
    prepare.add_argument("--max-works", type=int, default=100)
    prepare.add_argument("--start-year", type=int, default=2012)
    prepare.add_argument("--reserve-credits", type=int, default=40)
    prepare.add_argument("--seed", type=int, default=20260712)
    prepare.add_argument(
        "--official-pool-mode",
        action="store_true",
        help="Trust configured official directory membership independently of email availability",
    )
    prepare.add_argument(
        "--batch-identity",
        action="store_true",
        help="Resolve each institution's official names in one OpenAlex OR query",
    )

    run = subparsers.add_parser("run", help="Run profile-resolution ablations")
    run.add_argument("--dataset", required=True)
    run.add_argument("--output-dir", required=True)
    run.add_argument("--holdout-proposals", type=int, default=2)
    run.add_argument("--holdout-cv-works", type=int, default=2)
    run.add_argument("--min-profile-works", type=int, default=8)
    run.add_argument(
        "--encoder",
        choices=("production_terms", "tfidf"),
        default="production_terms",
    )
    compare = subparsers.add_parser("compare", help="Select a profile robust across encoder runs")
    compare.add_argument(
        "--runs",
        nargs="+",
        required=True,
        help="encoder=run_directory pairs",
    )
    compare.add_argument("--output-dir", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "prepare":
        result = prepare_dataset(args)
    elif args.command == "run":
        result = run_experiment(args)
    else:
        result = compare_experiment_runs(args)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
