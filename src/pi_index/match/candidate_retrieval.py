from __future__ import annotations

from ..index.text_index import SimpleTextIndex
from ..models import CanonicalPIRecord


def record_text(record: CanonicalPIRecord) -> str:
    parts = [
        record.display_name,
        record.title or "",
        record.department or "",
        " ".join(record.research_areas),
        " ".join(record.supervision_signals),
        " ".join(str(v) for v in (record.publications_summary or {}).values()),
    ]
    return " ".join(parts)


def retrieve_candidates(query: str, records: list[CanonicalPIRecord]) -> list[tuple[CanonicalPIRecord, float, list[str]]]:
    documents = {record.person_id: record_text(record) for record in records}
    index = SimpleTextIndex(documents)
    scored = []
    for record in records:
        score, overlap = index.score(query, record.person_id)
        scored.append((record, score, overlap))
    return sorted(scored, key=lambda item: item[1], reverse=True)
