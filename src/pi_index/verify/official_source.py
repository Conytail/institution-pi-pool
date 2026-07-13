from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse


def is_official_url(url: str | None, official_domains: list[str]) -> bool:
    if not url:
        return False
    if Path(url).exists():
        return True
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return True
    host = parsed.netloc.lower().removeprefix("www.")
    domains = [domain.lower().removeprefix("www.") for domain in official_domains]
    return any(host == domain or host.endswith("." + domain) for domain in domains)
