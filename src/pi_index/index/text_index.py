from __future__ import annotations

import math
import re
from collections import Counter


STOPWORDS = {
    "and",
    "the",
    "for",
    "with",
    "from",
    "this",
    "that",
    "into",
    "interested",
    "target",
    "degree",
    "phd",
}


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-zA-Z][a-zA-Z0-9+-]{2,}", text.lower()) if t not in STOPWORDS]


class SimpleTextIndex:
    def __init__(self, documents: dict[str, str]):
        self.documents = documents
        self.doc_terms = {doc_id: Counter(tokenize(text)) for doc_id, text in documents.items()}
        self.doc_freq: Counter[str] = Counter()
        for terms in self.doc_terms.values():
            self.doc_freq.update(set(terms))
        self.n_docs = max(1, len(documents))

    def score(self, query: str, doc_id: str) -> tuple[float, list[str]]:
        query_terms = Counter(tokenize(query))
        doc_terms = self.doc_terms.get(doc_id, Counter())
        if not query_terms or not doc_terms:
            return 0.0, []
        score = 0.0
        overlap: list[str] = []
        for term, q_count in query_terms.items():
            tf = doc_terms.get(term, 0)
            if not tf:
                continue
            idf = math.log((self.n_docs + 1) / (1 + self.doc_freq[term])) + 1
            score += (1 + math.log(tf)) * idf * q_count
            overlap.append(term)
        norm = math.sqrt(sum(v * v for v in doc_terms.values())) or 1.0
        return score / norm, sorted(set(overlap))
