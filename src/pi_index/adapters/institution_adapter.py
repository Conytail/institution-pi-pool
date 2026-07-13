from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..crawl.discovery import discover_institution_urls
from ..crawl.fetcher import Fetcher, FetchResult
from ..models import RawSourceRecord, utc_now_iso
from ..storage import PIIndexStorage
from .template_adapters import ADAPTERS


def _matches_any(value: str, patterns: list[str]) -> bool:
    value = value.lower()
    return any(pattern.lower() in value for pattern in patterns)


def discover_profile_links(
    html_text: str,
    source_url: str,
    official_domains: list[str],
    limit: int = 25,
    link_config: dict | None = None,
) -> list[str]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    source_host = urlparse(source_url).netloc.lower().removeprefix("www.")
    domains = [d.lower().removeprefix("www.") for d in official_domains]
    link_config = link_config or {}
    include_text = link_config.get("profile_link_text_patterns") or []
    include_url = link_config.get("profile_url_patterns") or []
    exclude_url = link_config.get("exclude_url_patterns") or []
    urls: list[str] = []
    for link in soup.find_all("a", href=True):
        text = link.get_text(" ", strip=True).lower()
        href = link["href"]
        if href.startswith("mailto:") or href.startswith("#"):
            continue
        absolute = urljoin(source_url, href)
        parsed = urlparse(absolute)
        host = parsed.netloc.lower().removeprefix("www.")
        if host and host != source_host and not any(host == d or host.endswith("." + d) for d in domains):
            continue
        haystack = f"{absolute} {text}".lower()
        if exclude_url and _matches_any(absolute, exclude_url):
            continue
        if include_text or include_url:
            matched = (include_text and _matches_any(text, include_text)) or (include_url and _matches_any(absolute, include_url))
        else:
            matched = any(token in haystack for token in ["/people/", "/person/", "/profile", "faculty", "personal page"])
        if matched:
            if absolute not in urls:
                urls.append(absolute)
        if len(urls) >= limit:
            break
    return urls


class ConfigDrivenInstitutionAdapter:
    def __init__(
        self,
        config: dict,
        crawl_policy: dict,
        fetcher: Fetcher,
        storage: PIIndexStorage,
        institution_id: str,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self.crawl_policy = crawl_policy
        self.fetcher = fetcher
        self.storage = storage
        self.institution_id = institution_id
        self.logger = logger or logging.getLogger(__name__)

    def crawl_and_parse(self) -> list[tuple[FetchResult, list]]:
        inst = self.config.get("institution", {})
        crawl = self.config.get("crawl", {})
        official_domains = inst.get("official_domains") or []
        max_depth = int(crawl.get("max_depth") or 0)
        max_pages = int(crawl.get("max_pages") or 100)
        delay = crawl.get("crawl_delay_seconds")
        for seed in crawl.get("seed_urls") or []:
            self.fetcher.set_domain_delay(seed, delay)

        queue = [(url, method, 0) for url, method in discover_institution_urls(self.config, self.crawl_policy, self.fetcher, self.logger)]
        seen: set[str] = set()
        parsed_results: list[tuple[FetchResult, list]] = []
        pages_fetched = 0

        while queue and pages_fetched < max_pages:
            url, method, depth = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            result = self.fetcher.fetch(url)
            pages_fetched += 1
            self.storage.insert_raw_source(
                RawSourceRecord(
                    source_url=url,
                    source_type="official_page",
                    institution_id=self.institution_id,
                    fetched_at=utc_now_iso(),
                    http_status=result.status_code,
                    content_hash=result.content_hash,
                    parser_used=None,
                    crawl_method=method,
                    error_reason=result.error,
                )
            )
            if result.error:
                self.storage.record_crawl_error(self.institution_id, url, "fetch", result.error)
                continue
            if not result.text or "html" not in (result.content_type or "text/html").lower():
                continue

            people = self._parse_html(result.text, result.final_url or url)
            parsed_results.append((result, people))

            if depth < max_depth and pages_fetched < max_pages:
                profile_limit = int(crawl.get("profile_link_limit") or 25)
                for link in discover_profile_links(result.text, result.final_url or url, official_domains, profile_limit, crawl):
                    if link not in seen:
                        queue.append((link, "profile_link", depth + 1))
        return parsed_results

    def _parse_html(self, html_text: str, source_url: str) -> list:
        preferred = self.config.get("parsing", {}).get("preferred_adapters") or list(ADAPTERS)
        all_people = []
        for adapter_name in preferred:
            adapter = ADAPTERS.get(adapter_name)
            if not adapter:
                self.logger.warning("Unknown adapter %s", adapter_name)
                continue
            try:
                people = adapter.parse(html_text, source_url, self.config)
            except Exception as exc:
                self.storage.record_crawl_error(self.institution_id, source_url, f"parse:{adapter_name}", f"{type(exc).__name__}: {exc}")
                continue
            stats_fn = getattr(adapter, "stats", None)
            if stats_fn:
                stats = stats_fn(html_text, people)
                self.storage.record_parse_metric(
                    self.institution_id,
                    source_url,
                    adapter_name,
                    stats.get("candidate_blocks", 0),
                    stats.get("people_extracted", len(people)),
                    stats.get("filtered_blocks", 0),
                )
            all_people.extend(people)
        deduped = {}
        for person in all_people:
            key = (person.name.lower(), person.profile_url or person.source_url, ",".join(person.emails))
            if key not in deduped or person.confidence > deduped[key].confidence:
                deduped[key] = person
        return list(deduped.values())
