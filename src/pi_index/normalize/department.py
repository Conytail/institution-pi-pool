from __future__ import annotations

import re


def normalize_department(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip(" -|")
    return value[:200] if value else None
