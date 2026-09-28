from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

from .fetcher import Fetcher


DEFAULT_PROFILE_PATTERNS = [
    r"/people",
    r"/faculty",
    r"/staff",
    r"/directory",
    r"/profile",
    r"/research",
    r"/academics",
    r"/departments",
]


def _same_domain(url: str, homepage_url: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(homepage_url).netloc.lower()


def _extract_locs(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    locs: list[str] = []
    for loc in root.iter():
        if loc.tag.endswith("loc") and loc.text:
            locs.append(loc.text.strip())
    return locs


def discover_sitemap_urls(
    homepage_url: str,
    fetcher: Fetcher,
    limit: int = 100,
    patterns: list[str] | None = None,
    sitemap_urls: list[str] | None = None,
    max_sitemaps: int = 10,
    logger: logging.Logger | None = None,
) -> list[str]:
    logger = logger or logging.getLogger(__name__)
    patterns = patterns or DEFAULT_PROFILE_PATTERNS
    initial_sitemaps = list(sitemap_urls or [])
    if not initial_sitemaps:
        initial_sitemaps.append(urljoin(homepage_url.rstrip("/") + "/", "sitemap.xml"))
        robots_url = urljoin(homepage_url.rstrip("/") + "/", "robots.txt")
        robots = fetcher.fetch(
            robots_url,
            source_type="robots_txt",
            crawl_method="sitemap_discovery:robots",
        )
        if robots.not_modified or (robots.status_code is not None and 200 <= robots.status_code < 300):
            for line in robots.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    initial_sitemaps.append(line.split(":", 1)[1].strip())

    discovered: list[str] = []
    seen_sitemaps: set[str] = set()
    queue = initial_sitemaps[:]
    while queue and len(discovered) < limit:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)
        result = fetcher.fetch(
            sitemap_url,
            source_type="official_sitemap",
            crawl_method="sitemap_discovery:sitemap",
        )
        if result.error or not result.text:
            logger.debug("Sitemap fetch skipped %s: %s", sitemap_url, result.error)
            continue
        locs = _extract_locs(result.text)
        for loc in locs:
            if ".xml" in urlparse(loc).path.lower() and len(seen_sitemaps) < max_sitemaps:
                queue.append(loc)
                continue
            if not _same_domain(loc, homepage_url):
                continue
            if any(re.search(pattern, loc, flags=re.I) for pattern in patterns):
                discovered.append(loc)
                if len(discovered) >= limit:
                    break
    return discovered
