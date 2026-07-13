from __future__ import annotations

from ..models import CanonicalPIRecord


def explain_match(record: CanonicalPIRecord, overlap: list[str], verdict: str) -> str:
    topics = ", ".join(overlap[:8]) if overlap else "no strong keyword overlap"
    title = record.title or "title unavailable"
    areas = ", ".join(record.research_areas[:5]) if record.research_areas else "research areas unavailable"
    return f"{record.display_name} ({title}) matched on {topics}. Areas: {areas}. Contact verdict: {verdict}."
