from __future__ import annotations

import os
from typing import Any

import requests


class OpenAlexClient:
    """No-key OpenAlex client. Uses OPENALEX_MAILTO when available."""

    base_url = "https://api.openalex.org"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.mailto = os.getenv("OPENALEX_MAILTO")

    def _params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = dict(params or {})
        if self.mailto:
            merged["mailto"] = self.mailto
        return merged

    def search_authors(self, name: str, ror_id: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"search": name, "per-page": limit}
        if ror_id:
            ror_suffix = ror_id.rstrip("/").split("/")[-1]
            params["filter"] = f"last_known_institutions.ror:{ror_suffix}"
        response = requests.get(f"{self.base_url}/authors", params=self._params(params), timeout=self.timeout)
        if response.status_code >= 400:
            return []
        return response.json().get("results", [])

    def search_institutions(self, name: str, limit: int = 3) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"search": name, "per-page": limit}
        response = requests.get(f"{self.base_url}/institutions", params=self._params(params), timeout=self.timeout)
        if response.status_code >= 400:
            return []
        return response.json().get("results", [])

    def search_works(self, query: str, limit: int = 10, institution_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"search": query, "per-page": limit, "sort": "relevance_score:desc"}
        if institution_id:
            params["filter"] = f"institutions.id:{institution_id}"
        response = requests.get(f"{self.base_url}/works", params=self._params(params), timeout=self.timeout)
        if response.status_code >= 400:
            return []
        return response.json().get("results", [])

    def works_for_author(self, openalex_author_id: str, limit: int = 5) -> list[dict[str, Any]]:
        params = {"filter": f"author.id:{openalex_author_id}", "per-page": limit, "sort": "publication_date:desc"}
        response = requests.get(f"{self.base_url}/works", params=self._params(params), timeout=self.timeout)
        if response.status_code >= 400:
            return []
        return response.json().get("results", [])
