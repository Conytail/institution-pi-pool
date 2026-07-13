from __future__ import annotations

from typing import Any

import requests


class ORCIDClient:
    """ORCID Public API client for public identity lookup."""

    base_url = "https://pub.orcid.org/v3.0"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    def search(self, given_name: str | None, family_name: str | None, institution_name: str | None = None) -> list[dict[str, Any]]:
        terms = []
        if given_name:
            terms.append(f'given-names:"{given_name}"')
        if family_name:
            terms.append(f'family-name:"{family_name}"')
        if institution_name:
            terms.append(f'affiliation-org-name:"{institution_name}"')
        if not terms:
            return []
        response = requests.get(
            f"{self.base_url}/search/",
            params={"q": " AND ".join(terms)},
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            return []
        return response.json().get("result", [])
