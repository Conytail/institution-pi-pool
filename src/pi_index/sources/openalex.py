from __future__ import annotations

from typing import Any

from .openalex_client import OpenAlexHTTPClient


class OpenAlexClient:
    """Compatibility wrapper for existing paper-backtrace callers.

    The historical module silently returned empty lists on HTTP errors and used
    obsolete no-key/mailto request semantics.  This wrapper preserves its public
    method signatures while delegating to the authenticated 2026 HTTP client,
    whose exceptions retain the actual response status.
    """

    def __init__(self, timeout: int = 20):
        self._client = OpenAlexHTTPClient(timeout=float(timeout))

    def search_authors(
        self,
        name: str,
        ror_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        return self._client.search_authors(name, ror_id=ror_id, limit=limit)

    def search_institutions(self, name: str, limit: int = 3) -> list[dict[str, Any]]:
        return self._client.search_institutions(name, limit=limit)

    def search_works(
        self,
        query: str,
        limit: int = 10,
        institution_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._client.search_works(
            query,
            limit=limit,
            institution_id=institution_id,
        )

    def works_for_author(
        self,
        openalex_author_id: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        short_id = openalex_author_id.rstrip("/").rsplit("/", 1)[-1]
        return self._client.search_works(
            "",
            limit=limit,
            filter={"author.id": short_id},
            sort="publication_date:desc",
        )


__all__ = ["OpenAlexClient"]
