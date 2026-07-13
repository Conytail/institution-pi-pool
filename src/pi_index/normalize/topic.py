from __future__ import annotations

import re


def normalize_topic(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .;,:").lower()


def normalize_topics(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        topic = normalize_topic(value)
        if topic and topic not in deduped:
            deduped.append(topic)
    return deduped
