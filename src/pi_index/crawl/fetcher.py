from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import time
from urllib.parse import unquote, urlparse

import requests

from ..models import content_hash
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
    ):
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.logger = logger or logging.getLogger(__name__)
        self.robots = RobotPolicy(user_agent, respect_robots, self.logger)
        self.rate_limiter = DomainRateLimiter(default_delay_seconds)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})

    def set_domain_delay(self, url: str, delay_seconds: float | None) -> None:
        domain = urlparse(url).netloc.lower()
        if domain:
            self.rate_limiter.set_delay(domain, delay_seconds)

    def fetch(self, url: str) -> FetchResult:
        if Path(url).exists():
            text = Path(url).read_text(encoding="utf-8")
            return FetchResult(url, url, 200, text, "text/html", content_hash(text))

        parsed = urlparse(url)
        if parsed.scheme == "file":
            path = Path(unquote(parsed.path))
            text = path.read_text(encoding="utf-8")
            return FetchResult(url, url, 200, text, "text/html", content_hash(text))

        if not self.robots.can_fetch(url):
            return FetchResult(url, url, 0, "", None, content_hash(""), "blocked_by_robots")

        crawl_delay = self.robots.crawl_delay(url)
        if crawl_delay is not None:
            self.set_domain_delay(url, crawl_delay)

        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self.rate_limiter.wait(url)
                response = self.session.get(url, timeout=self.timeout_seconds, allow_redirects=True)
                text = response.text if response.text is not None else ""
                if response.status_code >= 500 and attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (attempt + 1))
                    continue
                return FetchResult(
                    url=url,
                    final_url=response.url,
                    status_code=response.status_code,
                    text=text,
                    content_type=response.headers.get("content-type"),
                    content_hash=content_hash(text),
                    error=None if response.status_code < 400 else f"http_{response.status_code}",
                )
            except Exception as exc:  # pragma: no cover - network-dependent
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (attempt + 1))
        return FetchResult(url, url, None, "", None, content_hash(""), last_error or "fetch_failed")
