from __future__ import annotations

from collections import Counter
import math
import re


STOPWORDS = {
    "about",
    "above",
    "across",
    "after",
    "again",
    "against",
    "also",
    "among",
    "and",
    "are",
    "around",
    "based",
    "because",
    "been",
    "between",
    "both",
    "can",
    "did",
    "does",
    "done",
    "during",
    "each",
    "for",
    "from",
    "have",
    "having",
    "into",
    "its",
    "may",
    "more",
    "most",
    "over",
    "per",
    "such",
    "than",
    "that",
    "the",
    "their",
    "then",
    "there",
    "these",
    "this",
    "those",
    "through",
    "toward",
    "under",
    "using",
    "via",
    "was",
    "were",
    "with",
    "within",
    "without",
}


LOW_SIGNAL_TERMS = {
    "algorithm",
    "algorithms",
    "analysis",
    "application",
    "applications",
    "approach",
    "approaches",
    "artificial",
    "computational",
    "computer",
    "computing",
    "data",
    "deep",
    "development",
    "framework",
    "intelligence",
    "learning",
    "machine",
    "method",
    "methods",
    "model",
    "modeling",
    "models",
    "network",
    "networks",
    "optimization",
    "paper",
    "project",
    "proposal",
    "research",
    "science",
    "scientific",
    "study",
    "system",
    "systems",
}


def semantic_tokens(text: str) -> list[str]:
    raw = re.findall(r"[a-z][a-z0-9+-]{2,}", (text or "").lower())
    return [token for token in raw if token not in STOPWORDS]


def _term_weight(term: str) -> float:
    if term in LOW_SIGNAL_TERMS:
        return 0.25
    if len(term) >= 12:
        return 1.25
    if len(term) >= 8:
        return 1.1
    return 1.0


def _phrase_weight(parts: tuple[str, ...]) -> float:
    if all(part in LOW_SIGNAL_TERMS for part in parts):
        return 0.0
    informative = sum(1 for part in parts if part not in LOW_SIGNAL_TERMS)
    if informative == 0:
        return 0.0
    return 1.0 + 0.35 * (len(parts) - 1) + 0.15 * informative


def semantic_vector(text: str, max_features: int = 160) -> dict[str, float]:
    tokens = semantic_tokens(text)
    counts: Counter[str] = Counter()
    for token in tokens:
        counts[token] += _term_weight(token)
    for size in (2, 3):
        for index in range(0, max(0, len(tokens) - size + 1)):
            parts = tuple(tokens[index : index + size])
            weight = _phrase_weight(parts)
            if weight:
                counts["_".join(parts)] += weight

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:max_features]
    return {term: weight for term, weight in ranked}


def cosine_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    overlap = set(left).intersection(right)
    if not overlap:
        return 0.0
    dot = sum(left[term] * right[term] for term in overlap)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return round(min(1.0, dot / (left_norm * right_norm)), 4)


def semantic_similarity(left_text: str, right_text: str) -> float:
    return cosine_similarity(semantic_vector(left_text), semantic_vector(right_text))


def paper_relevance_score(proposal_text: str, paper_text: str) -> float:
    return semantic_similarity(proposal_text, paper_text)


def shared_terms(left_text: str, right_text: str, limit: int = 8) -> list[str]:
    left = semantic_vector(left_text)
    right = semantic_vector(right_text)
    ranked = sorted(
        ((term, left[term] + right[term]) for term in set(left).intersection(right)),
        key=lambda item: (-item[1], item[0]),
    )
    return [term.replace("_", " ") for term, _weight in ranked[:limit]]


def research_intent_text(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines()]
    keep_headings = {
        "objective",
        "profile",
        "project",
        "proposal",
        "research",
        "research experience",
        "research interests",
        "research statement",
        "short bio",
        "summary",
    }
    skip_headings = {
        "awards",
        "coursework",
        "education",
        "employment",
        "experience",
        "publications",
        "references",
        "service",
        "skills",
        "technical skills",
        "teaching",
    }
    kept: list[str] = []
    found_headings = False
    keep_section = False

    for line in lines:
        if not line:
            continue
        heading = re.sub(r"^\d+(\.\d+)*\s+", "", line).strip().lower()
        heading = re.sub(r"[^a-z ]+", " ", heading)
        heading = " ".join(heading.split())
        is_heading = len(heading.split()) <= 5 and (
            line.isupper()
            or re.match(r"^\d+(\.\d+)*\s+[A-Z]", line)
            or heading in keep_headings
            or heading in skip_headings
        )
        if is_heading:
            found_headings = True
            keep_section = heading in keep_headings or any(heading.startswith(f"{prefix} ") for prefix in keep_headings)
            if keep_section:
                kept.append(line)
            elif heading in skip_headings or any(heading.startswith(f"{prefix} ") for prefix in skip_headings):
                keep_section = False
            continue
        if keep_section:
            kept.append(line)

    if kept:
        return "\n".join(kept)
    if found_headings:
        return "\n".join(line for line in lines if line)
    return text
