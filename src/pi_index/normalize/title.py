from __future__ import annotations

import re


def normalize_title(title: str | None) -> str | None:
    if not title:
        return None
    return re.sub(r"\s+", " ", title).strip(" -|")
