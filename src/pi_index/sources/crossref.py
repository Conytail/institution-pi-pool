from __future__ import annotations

import os
from typing import Any

import requests


class CrossrefClient:
    """No-key Crossref client. Uses CROSSREF_MAILTO when available."""

    base_url = "https://api.crossref.org"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.mailto = os.getenv("CROSSREF_MAILTO")

    def works(self, query: str, rows: int = 5) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query, "rows": rows}
        if self.mailto:
            params["mailto"] = self.mailto
        response = requests.get(f"{self.base_url}/works", params=params, timeout=self.timeout)
        if response.status_code >= 400:
            return []
        return response.json().get("message", {}).get("items", [])
