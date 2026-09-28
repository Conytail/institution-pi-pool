from __future__ import annotations

import time
from threading import Lock
from urllib.parse import urlparse


class DomainRateLimiter:
    def __init__(self, default_delay_seconds: float = 1.0):
        self.default_delay_seconds = default_delay_seconds
        self.domain_delays: dict[str, float] = {}
        self.last_seen: dict[str, float] = {}
        self._lock = Lock()

    def set_delay(self, domain: str, delay_seconds: float | None) -> None:
        if delay_seconds is not None:
            with self._lock:
                self.domain_delays[domain.lower()] = max(0.0, float(delay_seconds))

    def wait(self, url: str) -> None:
        parsed = urlparse(url)
        domain = (parsed.netloc or parsed.path).lower()
        # Hold the lock through the short scheduling wait so concurrent workers
        # share one per-host request-start cadence.  Responses may overlap, but
        # a worker pool cannot accidentally multiply the configured rate.
        with self._lock:
            delay = self.domain_delays.get(domain, self.default_delay_seconds)
            now = time.monotonic()
            last = self.last_seen.get(domain)
            if last is not None:
                remaining = delay - (now - last)
                if remaining > 0:
                    time.sleep(remaining)
            self.last_seen[domain] = time.monotonic()
