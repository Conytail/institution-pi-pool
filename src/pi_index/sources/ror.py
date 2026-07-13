from __future__ import annotations

import logging
from typing import Any

import requests


class RORClient:
    """Small no-key ROR client used for institution normalization."""

    def __init__(self, timeout: int = 15, logger: logging.Logger | None = None):
        self.timeout = timeout
        self.logger = logger or logging.getLogger(__name__)

    def search(self, name: str) -> dict[str, Any] | None:
        if not name:
            return None
        urls = [
            "https://api.ror.org/v2/organizations",
            "https://api.ror.org/organizations",
        ]
        for url in urls:
            try:
                response = requests.get(url, params={"query": name}, timeout=self.timeout)
                if response.status_code >= 400:
                    continue
                data = response.json()
                items = data.get("items") or data.get("results") or []
                if not items:
                    continue
                first = items[0]
                return first.get("organization") or first
            except Exception as exc:  # pragma: no cover - network-dependent
                self.logger.debug("ROR lookup failed for %s via %s: %s", name, url, exc)
        return None

    def normalize(self, name: str) -> dict[str, Any] | None:
        item = self.search(name)
        if not item:
            return None
        names = item.get("names") or []
        display_name = item.get("name")
        aliases: list[str] = []
        if names:
            for name_obj in names:
                value = name_obj.get("value")
                if not value:
                    continue
                types = name_obj.get("types") or []
                if "ror_display" in types or not display_name:
                    display_name = value
                elif value != display_name:
                    aliases.append(value)
        links = item.get("links") or []
        homepage = None
        if links and isinstance(links[0], dict):
            homepage = links[0].get("value")
        elif links and isinstance(links[0], str):
            homepage = links[0]
        return {
            "ror_id": item.get("id"),
            "name": display_name,
            "aliases": aliases,
            "homepage_url": homepage,
            "country": (item.get("country") or {}).get("country_name"),
        }
