from __future__ import annotations

import logging
from threading import RLock
from urllib.parse import urlparse, urlunparse
import urllib.robotparser

import requests


class RobotPolicy:
    def __init__(self, user_agent: str, respect_robots: bool = True, logger: logging.Logger | None = None):
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self.logger = logger or logging.getLogger(__name__)
        self.cache: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._lock = RLock()

    def _robots_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))

    def _parser(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parsed = urlparse(url)
        if parsed.scheme in {"", "file"}:
            return None
        base = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            if base in self.cache:
                return self.cache[base]
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(self._robots_url(url))
            try:
                response = requests.get(
                    parser.url,
                    headers={"User-Agent": self.user_agent},
                    timeout=10,
                    allow_redirects=True,
                )
                if 200 <= response.status_code < 300:
                    parser.parse(response.text.splitlines())
                elif response.status_code in {401, 403}:
                    parser.disallow_all = True
                elif 400 <= response.status_code < 500:
                    parser.allow_all = True
                else:
                    parser.disallow_all = True
            except requests.RequestException as exc:  # pragma: no cover - network-dependent
                self.logger.debug("robots.txt read failed for %s: %s", base, exc)
                parser.disallow_all = True
            self.cache[base] = parser
            return parser

    def can_fetch(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = self._parser(url)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def crawl_delay(self, url: str) -> float | None:
        if not self.respect_robots:
            return None
        parser = self._parser(url)
        if parser is None:
            return None
        try:
            delay = parser.crawl_delay(self.user_agent)
            return float(delay) if delay is not None else None
        except Exception:
            return None
