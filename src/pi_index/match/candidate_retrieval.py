from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..index.text_index import SimpleTextIndex
from ..models import CanonicalPIRecord


def _publication_text(values: Iterable[str] | str | None) -> str:
    """Build searchable text from explicit publication evidence.

    Publication counts and years are intentionally not inferred from
    ``publications_summary`` here.  Matching needs titles/topics, not a proxy for
    productivity.  Callers should pass text loaded from the official publication
    fingerprint table (or another source whose provenance they track separately).
    """
    if not values:
        return ""
    items = [values] if isinstance(values, str) else values
    deduped: list[str] = []
    seen: set[str] = set()
    for value in items:
        text = " ".join(str(value or "").split())
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        deduped.append(text)
    return " ".join(deduped)


def record_text(
    record: CanonicalPIRecord,
    official_publication_text: Iterable[str] | str | None = None,
) -> str:
    parts = [
        record.display_name,
        record.department or "",
        " ".join(record.research_areas),
        _publication_text(official_publication_text),
    ]
    return " ".join(parts)


def has_research_evidence(
    record: CanonicalPIRecord,
    official_publication_text: Iterable[str] | str | None = None,
) -> bool:
    """Admit recommendations only from explicit research evidence.

    Appointment title, department and contact availability describe a person but
    do not establish research fit.  Official research areas or a meaningful
    publication fingerprint do.
    """

    return bool(record.research_areas or _publication_text(official_publication_text))


def retrieve_candidates(
    query: str,
    records: list[CanonicalPIRecord],
    official_publication_text_by_person: Mapping[str, Iterable[str] | str] | None = None,
) -> list[tuple[CanonicalPIRecord, float, list[str]]]:
    publication_text = official_publication_text_by_person or {}
    documents = {
        record.person_id: record_text(record, publication_text.get(record.person_id))
        for record in records
    }
    index = SimpleTextIndex(documents)
    scored = []
    for record in records:
        score, overlap = index.score(query, record.person_id)
        scored.append((record, score, overlap))
    return sorted(scored, key=lambda item: item[1], reverse=True)
