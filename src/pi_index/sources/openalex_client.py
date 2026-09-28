from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import os
import re
import time
from typing import Any, Protocol, TypeAlias

import requests


FilterInput: TypeAlias = str | Mapping[str, object] | Iterable[str] | None


class HTTPSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


class OpenAlexError(RuntimeError):
    """Base error for the OpenAlex HTTP client."""


class OpenAlexConfigurationError(OpenAlexError):
    """A required OpenAlex API capability or credential is unavailable."""


class OpenAlexHTTPError(OpenAlexError):
    """An HTTP response whose real status code is preserved for callers."""

    def __init__(self, status_code: int, url: str, body: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.body = body
        detail = f": {body[:300]}" if body else ""
        super().__init__(f"OpenAlex request failed with HTTP {status_code} for {url}{detail}")


class OpenAlexTransportError(OpenAlexError):
    """A network failure after retry attempts are exhausted."""


class OpenAlexProtocolError(OpenAlexError):
    """A successful HTTP response with an invalid OpenAlex JSON shape."""


@dataclass(frozen=True)
class OpenAlexWorksResult:
    """Auditable cursor result used to prove full-snapshot completeness."""

    works: list[dict[str, Any]]
    meta_count: int | None
    pages_fetched: int
    raw_results_count: int
    terminal_cursor: bool
    stopped_at_cutoff: bool
    cursors: tuple[str, ...]


def _filter_parts(value: FilterInput) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip().strip(",")
        return [text] if text else []
    if isinstance(value, Mapping):
        return [
            f"{key}:{item}"
            for key, item in value.items()
            if item is not None and str(key).strip() and str(item).strip()
        ]
    return [text for item in value if (text := str(item).strip().strip(","))]


def _short_author_id(value: str) -> str:
    match = re.search(r"(?:openalex\.org/)?(A\d+)\b", value or "", flags=re.I)
    if not match:
        raise ValueError(f"invalid OpenAlex Author ID: {value!r}")
    return match.group(1).upper()


def _short_work_id(value: str) -> str:
    match = re.search(r"(?:openalex\.org/)?(W\d+)\b", value or "", flags=re.I)
    if not match:
        raise ValueError(f"invalid OpenAlex Work ID: {value!r}")
    return match.group(1).upper()


def _short_institution_id(value: str) -> str:
    match = re.search(r"(?:openalex\.org/)?(I\d+)\b", value or "", flags=re.I)
    if not match:
        raise ValueError(f"invalid OpenAlex Institution ID: {value!r}")
    return match.group(1).upper()


def _canonical_ror_url(value: str) -> str:
    """Return the full ROR URL required by current OpenAlex ROR filters."""

    match = re.search(r"(?:ror\.org/)?(0[a-z0-9]{8})\b", value or "", flags=re.I)
    if not match:
        raise ValueError(f"invalid ROR ID: {value!r}")
    return f"https://ror.org/{match.group(1).casefold()}"


def _short_orcid(value: str) -> str:
    match = re.search(r"(\d{4}-\d{4}-\d{4}-[\dX]{4})\b", value or "", flags=re.I)
    if not match:
        raise ValueError(f"invalid ORCID: {value!r}")
    return match.group(1).upper()


def _short_doi(value: str) -> str:
    text = re.sub(
        r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)",
        "",
        str(value or "").strip(),
        flags=re.I,
    ).strip()
    if not text or "/" not in text or any(character.isspace() for character in text):
        raise ValueError(f"invalid DOI: {value!r}")
    return text.casefold()


class OpenAlexHTTPClient:
    """Small, injectable OpenAlex client for identity lookup and work synchronization.

    ``works_for_author`` always uses cursor paging and returns the unmodified work
    dictionaries supplied by OpenAlex.  HTTP 429 and 5xx responses are retried;
    other responses, including a genuine 403, are raised immediately with their
    actual status code.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://api.openalex.org",
        api_key: str | None = None,
        timeout: float = 30.0,
        session: HTTPSession | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if backoff_base < 0 or max_backoff < 0:
            raise ValueError("backoff values cannot be negative")
        self.base_url = base_url.rstrip("/")
        configured_api_key = os.getenv("OPENALEX_API_KEY") if api_key is None else api_key
        self.api_key = str(configured_api_key or "").strip() or None
        if not self.api_key:
            raise OpenAlexConfigurationError(
                "OpenAlex API key is required; pass api_key or set OPENALEX_API_KEY"
            )
        self.timeout = timeout
        self.session = session or requests.Session()
        self.sleep = sleep
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff

    def search_authors(
        self,
        name: str,
        ror_id: str | None = None,
        limit: int = 10,
        *,
        filter: FilterInput = None,
    ) -> list[dict[str, Any]]:
        """Search authors and return the raw author dictionaries from one page."""
        query = " ".join((name or "").split())
        if not query:
            raise ValueError("author name cannot be empty")
        self._validate_per_page(limit)
        filters = _filter_parts(filter)
        if ror_id:
            filters.insert(
                0,
                f"affiliations.institution.ror:{_canonical_ror_url(ror_id)}",
            )
        params: dict[str, Any] = {"search": query, "per_page": limit}
        if filters:
            params["filter"] = ",".join(filters)
        payload = self._get_json("authors", params)
        return self._raw_results(payload, endpoint="authors")

    def get_author_by_orcid(self, orcid: str) -> dict[str, Any] | None:
        """Resolve one canonical ORCID through OpenAlex's singleton endpoint."""

        normalized = _short_orcid(orcid)
        try:
            return self._get_json(f"authors/orcid:{normalized}", {})
        except OpenAlexHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise

    def get_author(self, openalex_author_id: str) -> dict[str, Any] | None:
        """Get one Author by canonical or URL-form OpenAlex identifier."""

        normalized = _short_author_id(openalex_author_id)
        try:
            return self._get_json(f"authors/{normalized}", {})
        except OpenAlexHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise

    def search_institutions(
        self,
        name: str,
        limit: int = 3,
        *,
        filter: FilterInput = None,
    ) -> list[dict[str, Any]]:
        """Search institutions and return one raw result page."""
        query = " ".join((name or "").split())
        if not query:
            raise ValueError("institution name cannot be empty")
        self._validate_per_page(limit)
        params: dict[str, Any] = {"search": query, "per_page": limit}
        filters = _filter_parts(filter)
        if filters:
            params["filter"] = ",".join(filters)
        payload = self._get_json("institutions", params)
        return self._raw_results(payload, endpoint="institutions")

    def search_works(
        self,
        query: str,
        limit: int = 10,
        institution_id: str | None = None,
        *,
        filter: FilterInput = None,
        sort: str | None = None,
        exact: bool = False,
    ) -> list[dict[str, Any]]:
        """Return one raw Works page for text search and/or exact filters."""
        self._validate_per_page(limit)
        normalized_query = " ".join((query or "").split())
        filters = _filter_parts(filter)
        if institution_id:
            filters.insert(0, f"institutions.id:{_short_institution_id(institution_id)}")
        if not normalized_query and not filters:
            raise ValueError("work search requires a query or filter")
        params: dict[str, Any] = {"per_page": limit}
        if normalized_query:
            params["search.exact" if exact else "search"] = normalized_query
        if filters:
            params["filter"] = ",".join(filters)
        if sort:
            params["sort"] = sort
        payload = self._get_json("works", params)
        return self._raw_results(payload, endpoint="works")

    def get_work_by_doi(self, doi: str) -> dict[str, Any] | None:
        """Resolve an exact DOI with OpenAlex's unmetered singleton endpoint."""

        normalized = _short_doi(doi)
        try:
            return self._get_json(f"works/doi:{normalized}", {})
        except OpenAlexHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise

    def get_work(self, openalex_work_id: str) -> dict[str, Any] | None:
        """Resolve an exact OpenAlex Work ID with the singleton endpoint."""

        normalized = _short_work_id(openalex_work_id)
        try:
            return self._get_json(f"works/{normalized}", {})
        except OpenAlexHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise

    def works_for_author(
        self,
        openalex_author_id: str,
        *,
        per_page: int = 100,
        since_updated_date: str | datetime | None = None,
        filter: FilterInput = None,
        premium_updated_filter: bool = False,
        sort: str | None = None,
        stop_before_updated_date: str | datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return every matching raw work using OpenAlex cursor pagination.

        ``since_updated_date`` becomes the OpenAlex ``from_updated_date`` filter,
        which requires an OpenAlex Premium plan and must be explicitly enabled.
        ``filter`` accepts a complete filter string, an iterable of filter strings,
        or a mapping of field names to values.
        """
        return self.fetch_works_for_author(
            openalex_author_id,
            per_page=per_page,
            since_updated_date=since_updated_date,
            filter=filter,
            premium_updated_filter=premium_updated_filter,
            sort=sort,
            stop_before_updated_date=stop_before_updated_date,
        ).works

    def fetch_works_for_author(
        self,
        openalex_author_id: str,
        *,
        per_page: int = 100,
        since_updated_date: str | datetime | None = None,
        filter: FilterInput = None,
        premium_updated_filter: bool = False,
        sort: str | None = None,
        stop_before_updated_date: str | datetime | None = None,
    ) -> OpenAlexWorksResult:
        """Cursor-page works and retain completeness/cutoff audit metadata."""
        self._validate_per_page(per_page)
        if since_updated_date is not None and stop_before_updated_date is not None:
            raise ValueError(
                "since_updated_date and stop_before_updated_date are alternative delta strategies"
            )
        author_id = _short_author_id(openalex_author_id)
        filters = [f"author.id:{author_id}"]
        if since_updated_date is not None:
            if not premium_updated_filter:
                raise OpenAlexConfigurationError(
                    "since_updated_date uses OpenAlex's Premium from_updated_date filter; "
                    "set premium_updated_filter=True only for a Premium account"
                )
            since = self._updated_date_value(since_updated_date)
            if since:
                filters.append(f"from_updated_date:{since}")
        filters.extend(_filter_parts(filter))

        cutoff = (
            self._parse_updated_datetime(stop_before_updated_date)
            if stop_before_updated_date is not None
            else None
        )
        if cutoff is not None and sort not in {"updated_date:desc", "-updated_date"}:
            raise ValueError(
                "stop_before_updated_date requires descending updated_date sort"
            )

        works: list[dict[str, Any]] = []
        cursor = "*"
        seen_cursors: set[str] = set()
        cursors: list[str] = []
        pages_fetched = 0
        raw_results_count = 0
        meta_count: int | None = None
        terminal_cursor = False
        stopped_at_cutoff = False
        while cursor:
            if cursor in seen_cursors:
                raise OpenAlexProtocolError(
                    f"OpenAlex repeated cursor {cursor!r} while paging author {author_id}"
                )
            seen_cursors.add(cursor)
            cursors.append(cursor)
            params: dict[str, Any] = {
                "filter": ",".join(filters),
                "per_page": per_page,
                "cursor": cursor,
            }
            if sort:
                params["sort"] = sort
            payload = self._get_json(
                "works",
                params,
            )
            pages_fetched += 1
            page_works = self._raw_results(payload, endpoint="works")
            raw_results_count += len(page_works)
            meta = payload.get("meta")
            if meta is not None and not isinstance(meta, Mapping):
                raise OpenAlexProtocolError("OpenAlex works response has a non-object meta field")
            page_count = (meta or {}).get("count")
            if page_count is not None:
                try:
                    page_count = int(page_count)
                except (TypeError, ValueError) as exc:
                    raise OpenAlexProtocolError("OpenAlex works meta.count is not an integer") from exc
                if meta_count is None:
                    meta_count = page_count
                elif page_count != meta_count:
                    raise OpenAlexProtocolError(
                        "OpenAlex works meta.count changed during cursor pagination"
                    )
            for work in page_works:
                if cutoff is not None:
                    updated = self._parse_updated_datetime(work.get("updated_date"), required=False)
                    if updated is not None and updated < cutoff:
                        stopped_at_cutoff = True
                        break
                works.append(work)
            if stopped_at_cutoff:
                break
            next_cursor = (meta or {}).get("next_cursor")
            if not next_cursor:
                terminal_cursor = True
                break
            cursor = str(next_cursor)
        return OpenAlexWorksResult(
            works=works,
            meta_count=meta_count,
            pages_fetched=pages_fetched,
            raw_results_count=raw_results_count,
            terminal_cursor=terminal_cursor,
            stopped_at_cutoff=stopped_at_cutoff,
            cursors=tuple(cursors),
        )

    @staticmethod
    def _validate_per_page(value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
            raise ValueError("per_page must be an integer between 1 and 100")

    @staticmethod
    def _updated_date_value(value: str | datetime) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat().replace("+00:00", "Z")
        return str(value).strip()

    @staticmethod
    def _parse_updated_datetime(
        value: str | datetime | Any,
        *,
        required: bool = True,
    ) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                if required:
                    raise ValueError("updated_date cutoff cannot be empty")
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                if required:
                    raise ValueError(f"invalid updated_date cutoff: {value!r}") from None
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _params(self, params: Mapping[str, Any]) -> dict[str, Any]:
        merged = dict(params)
        merged["api_key"] = self.api_key
        return merged

    def _get_json(self, endpoint: str, params: Mapping[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        request_params = self._params(params)
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, params=request_params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise OpenAlexTransportError(
                        f"OpenAlex transport failed after {attempt + 1} attempts for {url}: {exc}"
                    ) from exc
                self.sleep(self._backoff_seconds(attempt))
                continue

            status = int(response.status_code)
            if 200 <= status < 300:
                try:
                    payload = response.json()
                except (TypeError, ValueError) as exc:
                    raise OpenAlexProtocolError(
                        f"OpenAlex returned invalid JSON for {url}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise OpenAlexProtocolError(
                        f"OpenAlex returned a non-object JSON response for {url}"
                    )
                return payload

            retryable = status == 429 or 500 <= status <= 599
            if retryable and attempt < self.max_retries:
                self.sleep(self._retry_delay(response, attempt))
                continue
            raise OpenAlexHTTPError(status, url, str(getattr(response, "text", "") or ""))

        raise AssertionError("unreachable OpenAlex retry state")

    @staticmethod
    def _raw_results(payload: Mapping[str, Any], *, endpoint: str) -> list[dict[str, Any]]:
        results = payload.get("results")
        if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
            raise OpenAlexProtocolError(
                f"OpenAlex {endpoint} response has a non-list or non-object results field"
            )
        return results

    def _backoff_seconds(self, attempt: int) -> float:
        return min(self.max_backoff, self.backoff_base * (2**attempt))

    def _retry_delay(self, response: Any, attempt: int) -> float:
        backoff = self._backoff_seconds(attempt)
        retry_after = self._retry_after_seconds(getattr(response, "headers", {}) or {})
        return max(backoff, retry_after) if retry_after is not None else backoff

    @staticmethod
    def _retry_after_seconds(headers: Mapping[str, Any]) -> float | None:
        raw: Any = None
        for key, value in headers.items():
            if str(key).casefold() == "retry-after":
                raw = value
                break
        if raw is None:
            return None
        try:
            return max(0.0, float(str(raw).strip()))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(str(raw))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None


# A concise alias for callers that do not need the transport distinction in the name.
OpenAlexClient = OpenAlexHTTPClient


__all__ = [
    "OpenAlexClient",
    "OpenAlexConfigurationError",
    "OpenAlexError",
    "OpenAlexHTTPClient",
    "OpenAlexHTTPError",
    "OpenAlexProtocolError",
    "OpenAlexTransportError",
    "OpenAlexWorksResult",
]
