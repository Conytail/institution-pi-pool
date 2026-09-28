from __future__ import annotations

from dataclasses import dataclass
import logging
import mimetypes
from pathlib import Path
import re
from threading import Lock, local
import time
from typing import Callable
from urllib.parse import unquote, urlparse

import requests

from ..models import RawSourceRecord, content_hash, utc_now_iso
from .archive import ContentArchive
from .rate_limiter import DomainRateLimiter
from .robots import RobotPolicy


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int | None
    text: str
    content_type: str | None
    content_hash: str
    error: str | None = None
    body: bytes = b""
    fetched_at: str = ""
    encoding: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    archive_key: str | None = None
    uncompressed_bytes: int = 0
    compressed_bytes: int = 0
    network_bytes: int = 0
    not_modified: bool = False


CacheLookup = Callable[[str], RawSourceRecord | None]
FetchObserver = Callable[[FetchResult, str, str], None]


def _is_textual(content_type: str | None, url: str) -> bool:
    value = (content_type or "").lower()
    if any(token in value for token in ("text/", "html", "json", "xml", "javascript")):
        return True
    return Path(urlparse(url).path).suffix.lower() in {".html", ".htm", ".json", ".xml", ".txt"}


def _decode_body(body: bytes, content_type: str | None, encoding: str | None, url: str) -> str:
    if not body or not _is_textual(content_type, url):
        return ""
    return body.decode(encoding or "utf-8", errors="replace")


def _response_encoding(response: requests.Response, content_type: str | None) -> str | None:
    encoding = response.encoding
    if not _is_textual(content_type, response.url):
        return encoding
    if (encoding or "").lower() not in {"iso-8859-1", "latin-1"}:
        return encoding or response.apparent_encoding
    head = (response.content or b"")[:4096]
    match = re.search(br"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)", head, flags=re.I)
    if match:
        return match.group(1).decode("ascii", errors="ignore") or encoding
    return response.apparent_encoding or encoding


def blocked_interstitial_reason(text: str) -> str | None:
    lower = (text or "").lower()
    if "_incapsula_resource" in lower and "request unsuccessful" in lower:
        return "blocked_interstitial:incapsula"
    if "_incapsula_resource" in lower and re.search(r"<body[^>]*>\s*</body>", lower):
        return "blocked_interstitial:incapsula"
    if "requested page is currently unavailable" in lower and "site is processing for" in lower:
        return "blocked_interstitial:site_processing"
    return None


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        timeout_seconds: int = 20,
        max_retries: int = 2,
        backoff_seconds: float = 1.5,
        default_delay_seconds: float = 1.0,
        respect_robots: bool = True,
        logger: logging.Logger | None = None,
        archive: ContentArchive | None = None,
        cache_lookup: CacheLookup | None = None,
        on_result: FetchObserver | None = None,
        offline: bool = False,
    ):
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.logger = logger or logging.getLogger(__name__)
        self.robots = RobotPolicy(user_agent, respect_robots, self.logger)
        self.rate_limiter = DomainRateLimiter(default_delay_seconds)
        self.archive = archive
        self.cache_lookup = cache_lookup
        self.on_result = on_result
        self.offline = offline
        self._force_archive_urls: set[str] = set()
        self._archive_state_lock = Lock()
        self._session_local = local()
        self._session_headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
        }
        # Keep the public attribute for existing single-threaded callers while
        # giving each worker its own requests.Session during concurrent refresh.
        self.session = self._new_session()
        self._session_local.session = self.session

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(self._session_headers)
        return session

    def _request_session(self) -> requests.Session:
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = self._new_session()
            self._session_local.session = session
        return session

    def set_domain_delay(self, url: str, delay_seconds: float | None) -> None:
        domain = urlparse(url).netloc.lower()
        if domain:
            self.rate_limiter.set_delay(domain, delay_seconds)

    def _cached(self, url: str) -> RawSourceRecord | None:
        return self.cache_lookup(url) if self.cache_lookup else None

    def _archive_result(self, result: FetchResult) -> FetchResult:
        if not result.body or self.archive is None:
            result.uncompressed_bytes = len(result.body)
            return result
        with self._archive_state_lock:
            force = result.url in self._force_archive_urls
        entry = self.archive.store(result.body, force=force)
        if result.status_code is not None and 200 <= result.status_code < 300:
            with self._archive_state_lock:
                self._force_archive_urls.discard(result.url)
        result.archive_key = entry.archive_key
        result.content_hash = entry.body_sha256
        result.uncompressed_bytes = entry.uncompressed_bytes
        result.compressed_bytes = entry.compressed_bytes
        return result

    def _finish(self, result: FetchResult, source_type: str, crawl_method: str) -> FetchResult:
        if not result.fetched_at:
            result.fetched_at = utc_now_iso()
        result = self._archive_result(result)
        if self.on_result:
            self.on_result(result, source_type, crawl_method)
        return result

    def _from_cache(self, url: str, cached: RawSourceRecord, *, offline: bool) -> FetchResult | None:
        if self.archive is None or not self.archive.exists(cached.archive_key):
            return None
        body = self.archive.read(str(cached.archive_key))
        content_type = cached.content_type
        encoding = cached.encoding
        return FetchResult(
            url=url,
            final_url=cached.final_url or url,
            status_code=304,
            text=_decode_body(body, content_type, encoding, cached.final_url or url),
            content_type=content_type,
            content_hash=cached.body_sha256 or cached.content_hash,
            body=body,
            fetched_at=utc_now_iso(),
            encoding=encoding,
            etag=cached.etag,
            last_modified=cached.last_modified,
            archive_key=cached.archive_key,
            uncompressed_bytes=len(body),
            compressed_bytes=cached.compressed_bytes,
            network_bytes=0,
            not_modified=True,
            error=None if not offline else None,
        )

    def fetch(
        self,
        url: str,
        *,
        source_type: str = "official_page",
        crawl_method: str = "direct_fetch",
        request_method: str = "GET",
        form_data: dict[str, str] | None = None,
    ) -> FetchResult:
        request_method = request_method.upper()
        parsed = urlparse(url)
        cached = self._cached(url) if request_method == "GET" or self.offline else None
        if self.offline:
            try:
                replay = self._from_cache(url, cached, offline=True) if cached else None
            except Exception as exc:
                return self._finish(
                    FetchResult(
                        url=url,
                        final_url=url,
                        status_code=None,
                        text="",
                        content_type=cached.content_type if cached else None,
                        content_hash=content_hash(b""),
                        error=f"offline_cache_error:{type(exc).__name__}",
                    ),
                    source_type,
                    f"offline_replay:{crawl_method}",
                )
            if replay:
                return self._finish(replay, source_type, f"offline_replay:{crawl_method}")
            return self._finish(
                FetchResult(
                    url=url,
                    final_url=url,
                    status_code=None,
                    text="",
                    content_type=None,
                    content_hash=content_hash(b""),
                    error="offline_cache_miss",
                ),
                source_type,
                f"offline_replay:{crawl_method}",
            )

        local_path: Path | None = None
        try:
            candidate = Path(url)
            if candidate.exists():
                local_path = candidate
        except OSError:
            local_path = None
        if local_path is None and parsed.scheme == "file":
            local_path = Path(unquote(parsed.path))
        elif local_path is None and not parsed.scheme:
            candidate = Path(url)
            if candidate.exists():
                local_path = candidate
        if local_path is not None:
            body = local_path.read_bytes()
            content_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
            result = FetchResult(
                url=url,
                final_url=url,
                status_code=200,
                text=_decode_body(body, content_type, "utf-8", url),
                content_type=content_type,
                content_hash=content_hash(body),
                body=body,
                fetched_at=utc_now_iso(),
                encoding="utf-8" if _is_textual(content_type, url) else None,
                network_bytes=0,
            )
            return self._finish(result, source_type, crawl_method)

        if not self.robots.can_fetch(url):
            return self._finish(
                FetchResult(url, url, 0, "", None, content_hash(b""), "blocked_by_robots"),
                source_type,
                crawl_method,
            )

        crawl_delay = self.robots.crawl_delay(url)
        if crawl_delay is not None:
            self.set_domain_delay(url, crawl_delay)

        conditional_headers: dict[str, str] = {}
        try:
            cached_replay = self._from_cache(url, cached, offline=False) if cached else None
        except Exception as exc:
            self.logger.warning("Cached response could not be replayed for %s: %s", url, exc)
            with self._archive_state_lock:
                self._force_archive_urls.add(url)
            cached_replay = None
        if cached_replay:
            if cached and cached.etag:
                conditional_headers["If-None-Match"] = cached.etag
            if cached and cached.last_modified:
                conditional_headers["If-Modified-Since"] = cached.last_modified

        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self.rate_limiter.wait(url)
                response = self._request_session().request(
                    request_method,
                    url,
                    timeout=self.timeout_seconds,
                    allow_redirects=True,
                    headers=conditional_headers,
                    data=form_data if request_method == "POST" else None,
                )
                if response.status_code == 304 and cached_replay:
                    cached_replay.fetched_at = utc_now_iso()
                    cached_replay.etag = response.headers.get("etag") or cached_replay.etag
                    cached_replay.last_modified = response.headers.get("last-modified") or cached_replay.last_modified
                    return self._finish(cached_replay, source_type, crawl_method)
                if response.status_code >= 500 and attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (attempt + 1))
                    continue

                body = response.content or b""
                content_type = response.headers.get("content-type")
                encoding = _response_encoding(response, content_type)
                text = _decode_body(body, content_type, encoding, response.url)
                interstitial_error = blocked_interstitial_reason(text) if response.status_code < 400 else None
                if interstitial_error and attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (attempt + 1))
                    continue
                result = FetchResult(
                    url=url,
                    final_url=response.url,
                    status_code=response.status_code,
                    text=text,
                    content_type=content_type,
                    content_hash=content_hash(body),
                    error=interstitial_error or (None if response.status_code < 400 else f"http_{response.status_code}"),
                    body=body,
                    fetched_at=utc_now_iso(),
                    encoding=encoding,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    network_bytes=len(body),
                )
                return self._finish(result, source_type, crawl_method)
            except Exception as exc:  # pragma: no cover - network-dependent
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (attempt + 1))
        return self._finish(
            FetchResult(url, url, None, "", None, content_hash(b""), last_error or "fetch_failed"),
            source_type,
            crawl_method,
        )
