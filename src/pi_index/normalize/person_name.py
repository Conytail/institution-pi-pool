from __future__ import annotations

import re


def split_name(display_name: str) -> tuple[str | None, str | None, str]:
    name = re.sub(r"\s+", " ", display_name or "").strip()
    if "," in name:
        family, given = [part.strip() for part in name.split(",", 1)]
        normalized = f"{given} {family}".strip()
        return given or None, family or None, normalized
    parts = name.split()
    if not parts:
        return None, None, ""
    if len(parts) == 1:
        return None, parts[0], parts[0]
    return " ".join(parts[:-1]), parts[-1], name
