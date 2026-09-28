from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from ..crawl.discovery import discover_institution_urls
from ..crawl.fetcher import Fetcher, FetchResult
from ..parsers.publications import extract_publication_snapshot
from ..storage import PIIndexStorage
from .template_adapters import ADAPTERS


PROFILE_CRAWL_METHODS = {"configured_profile", "profile_link", "sitemap_profile"}
PROFILE_PUBLICATION_PARSER_VERSION = "official-publication-snapshot-v3"


@dataclass(frozen=True)
class ProfilePublicationParse:
    fingerprints: list[dict[str, Any]]
    parser_names: tuple[str, ...]
    parser_errors: tuple[str, ...]
    person_match_count: int
    parsed_person_count: int
    authoritative: bool
    truncated: bool
    reason: str | None


def parse_profile_publications(
    html_text: str,
    source_url: str,
    config: dict,
    expected_names: list[str],
) -> ProfilePublicationParse:
    """Parse one PI profile without mutating crawl/evidence state.

    Identity and inventory completeness are returned separately so refresh code
    can accept additions from weak pages while refusing destructive diffs.
    """

    parsing = config.get("parsing", {})
    preferred = parsing.get("profile_adapters") or parsing.get("preferred_adapters") or list(ADAPTERS)
    parser_names: list[str] = []
    parser_errors: list[str] = []
    parsed_people: list[Any] = []
    for adapter_name in preferred:
        adapter = ADAPTERS.get(adapter_name)
        if adapter is None:
            parser_errors.append(f"unknown_adapter:{adapter_name}")
            continue
        try:
            people = adapter.parse(html_text, source_url, config)
        except Exception as exc:  # parser failures must quarantine removals
            parser_errors.append(f"{adapter_name}:{type(exc).__name__}:{exc}")
            continue
        parser_names.append(adapter_name)
        parsed_people.extend(people)

    expected_keys = {
        _normalized_person_name(value)
        for value in expected_names
        if _normalized_person_name(value)
    }
    parsed_name_keys = {
        _normalized_person_name(str(getattr(person, "name", "")))
        for person in parsed_people
        if _normalized_person_name(str(getattr(person, "name", "")))
    }
    matched_keys = parsed_name_keys.intersection(expected_keys)
    identity_unique = len(matched_keys) == 1 and parsed_name_keys == matched_keys

    snapshot = extract_publication_snapshot(html_text, source_url)
    authoritative = (
        identity_unique
        and snapshot.authoritative
        and not snapshot.truncated
        and not parser_errors
    )
    reason: str | None = None
    if not matched_keys:
        reason = "target_identity_not_found"
    elif len(matched_keys) != 1 or parsed_name_keys != matched_keys:
        reason = "profile_identity_ambiguous"
    elif parser_errors:
        reason = "parser_error"
    elif snapshot.truncated:
        reason = "publication_inventory_truncated"
    elif not snapshot.authoritative:
        reason = "publication_inventory_not_authoritative"

    return ProfilePublicationParse(
        fingerprints=snapshot.fingerprints,
        parser_names=tuple(parser_names),
        parser_errors=tuple(parser_errors),
        person_match_count=len(matched_keys),
        parsed_person_count=len(parsed_name_keys),
        authoritative=authoritative,
        truncated=snapshot.truncated,
        reason=reason,
    )


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


def _normalized_person_name(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).casefold().split())


def _official_profile_url(
    value: str | None,
    source_url: str,
    official_domains: list[str],
) -> str | None:
    if not value:
        return None
    absolute = urljoin(source_url, value)
    parsed = urlparse(absolute)
    host = parsed.netloc.lower().removeprefix("www.")
    domains = [domain.lower().removeprefix("www.") for domain in official_domains]
    if not host or not any(host == domain or host.endswith("." + domain) for domain in domains):
        return None
    if parsed.path.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".svg", ".pdf")):
        return None
    canonical = urlunparse(parsed._replace(fragment=""))
    source_canonical = urlunparse(urlparse(source_url)._replace(fragment=""))
    return canonical if canonical != source_canonical else None


def discover_profile_links_from_people(
    people: list,
    source_url: str,
    official_domains: list[str],
    limit: int = 25,
) -> list[str]:
    """Use parser-established person/profile relationships before raw links."""

    urls: list[str] = []
    for person in people:
        profile_url = _official_profile_url(
            getattr(person, "profile_url", None),
            source_url,
            official_domains,
        )
        if profile_url and profile_url not in urls:
            urls.append(profile_url)
        if len(urls) >= limit:
            break
    return urls


def discover_pagination_links(
    html_text: str,
    source_url: str,
    official_domains: list[str],
    link_config: dict | None = None,
) -> list[str]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    source_parsed = urlparse(source_url)
    source_host = source_parsed.netloc.lower().removeprefix("www.")
    source_path = source_parsed.path.rstrip("/") or "/"
    pagination_root = re.sub(r"/page/?\d+$", "", source_path, flags=re.I) or "/"
    link_config = link_config or {}
    text_patterns = link_config.get("pagination_link_text_patterns") or [
        "next",
        "next page",
        "older",
        "more results",
        "下一页",
    ]
    url_patterns = link_config.get("pagination_url_patterns") or []
    urls: list[str] = []
    for link in soup.find_all("a", href=True):
        href = link.get("href") or ""
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        absolute = urljoin(source_url, href)
        parsed = urlparse(absolute)
        absolute = urlunparse(parsed._replace(fragment=""))
        parsed = urlparse(absolute)
        host = parsed.netloc.lower().removeprefix("www.")
        if host and host != source_host:
            continue
        candidate_path = parsed.path.rstrip("/") or "/"
        same_pagination_family = candidate_path == pagination_root or bool(
            re.fullmatch(re.escape(pagination_root.rstrip("/")) + r"/page/?\d+", candidate_path, flags=re.I)
        )
        if not same_pagination_family:
            continue
        text = " ".join(
            filter(
                None,
                [
                    link.get_text(" ", strip=True),
                    link.get("aria-label"),
                    link.get("title"),
                ],
            )
        ).lower()
        rel = [str(value).lower() for value in (link.get("rel") or [])]
        matched = "next" in rel
        matched = matched or any(pattern.lower() == text or pattern.lower() in text for pattern in text_patterns)
        matched = matched or any(pattern.lower() in absolute.lower() for pattern in url_patterns)
        if matched and absolute not in urls:
            urls.append(absolute)
    return urls


@dataclass
class CrawlOutcome:
    parsed_results: list[tuple[FetchResult, list]]
    metrics: dict


class ConfigDrivenInstitutionAdapter:
    def __init__(
        self,
        config: dict,
        crawl_policy: dict,
        fetcher: Fetcher,
        storage: PIIndexStorage,
        institution_id: str,
        run_id: str,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self.crawl_policy = crawl_policy
        self.fetcher = fetcher
        self.storage = storage
        self.institution_id = institution_id
        self.run_id = run_id
        self.logger = logger or logging.getLogger(__name__)
        self._candidate_blocks = 0
        self._filtered_blocks = 0
        self._last_page_had_candidate = False

    def crawl_and_parse(self) -> CrawlOutcome:
        inst = self.config.get("institution", {})
        crawl = self.config.get("crawl", {})
        official_domains = inst.get("official_domains") or []
        max_depth = int(crawl.get("max_depth") or 0)
        max_pages = int(crawl.get("max_pages") or 100)
        seed_requests = {
            str(item.get("url")): item
            for item in crawl.get("seed_requests") or []
            if isinstance(item, dict) and item.get("url")
        }
        delay = crawl.get("crawl_delay_seconds")
        for seed in crawl.get("seed_urls") or []:
            self.fetcher.set_domain_delay(seed, delay)

        discovered_urls = discover_institution_urls(self.config, self.crawl_policy, self.fetcher, self.logger)
        queue: list[tuple[str, str, int]] = []
        queued: set[str] = set()
        queued_methods: dict[str, str] = {}
        all_discovered_urls: set[str] = set()
        seed_urls = set(crawl.get("seed_urls") or [])
        configured_units = (self.config.get("pool_scope") or {}).get("units") or [
            {
                "name": (self.config.get("pool_scope") or {}).get("name") or "configured_scope",
                "seed_urls": sorted(seed_urls),
            }
        ]
        seen: set[str] = set()
        parsed_results: list[tuple[FetchResult, list]] = []
        pages_fetched = 0
        pages_succeeded = 0
        pages_failed = 0
        pages_not_modified = 0
        network_bytes = 0
        seed_attempted = 0
        seed_succeeded = 0
        successful_seed_urls: set[str] = set()
        profile_links: set[str] = set()
        profile_attempted = 0
        profile_succeeded = 0
        profile_parsed = 0
        pagination_links: set[str] = set()
        pagination_attempted = 0
        pagination_succeeded = 0
        pagination_failed = 0

        exclude_url_patterns = crawl.get("exclude_url_patterns") or []

        def enqueue_discovered_url(url: str, method: str, depth: int) -> bool:
            # Configured seeds are authoritative inputs. Every URL discovered
            # from them (raw links, parsed people/overrides, pagination, or a
            # sitemap) passes through this one exclusion check before enqueue.
            if method != "configured_seed" and exclude_url_patterns and _matches_any(url, exclude_url_patterns):
                return False
            all_discovered_urls.add(url)
            if url in seen:
                return False
            if url in queued:
                if queued_methods.get(url) == method:
                    if method in PROFILE_CRAWL_METHODS:
                        profile_links.add(url)
                    elif method == "pagination":
                        pagination_links.add(url)
                return False
            queue.append((url, method, depth))
            queued.add(url)
            queued_methods[url] = method
            if method in PROFILE_CRAWL_METHODS:
                profile_links.add(url)
            elif method == "pagination":
                pagination_links.add(url)
            return True

        for url, method in discovered_urls:
            enqueue_discovered_url(url, method, 0)

        while queue and pages_fetched < max_pages:
            url, method, depth = queue.pop(0)
            queued.discard(url)
            queued_methods.pop(url, None)
            if url in seen:
                continue
            seen.add(url)
            request_spec = seed_requests.get(url) or {}
            fetch_kwargs = {
                "source_type": "official_page",
                "crawl_method": method,
            }
            if request_spec:
                fetch_kwargs["request_method"] = str(request_spec.get("method") or "GET")
                if isinstance(request_spec.get("form_data"), dict):
                    fetch_kwargs["form_data"] = request_spec["form_data"]
            result = self.fetcher.fetch(url, **fetch_kwargs)
            pages_fetched += 1
            network_bytes += int(result.network_bytes or 0)
            if result.not_modified:
                pages_not_modified += 1
            if method == "configured_seed":
                seed_attempted += 1
            elif method in PROFILE_CRAWL_METHODS:
                profile_attempted += 1
            elif method == "pagination":
                pagination_attempted += 1
            if result.error:
                pages_failed += 1
                if method == "pagination":
                    pagination_failed += 1
                self.storage.record_crawl_error(
                    self.institution_id,
                    url,
                    "fetch",
                    result.error,
                    self.run_id,
                )
                continue
            pages_succeeded += 1
            if method == "configured_seed":
                seed_succeeded += 1
                successful_seed_urls.add(url)
            elif method in PROFILE_CRAWL_METHODS:
                profile_succeeded += 1
            elif method == "pagination":
                pagination_succeeded += 1
            content_type = (result.content_type or "text/html").lower()
            if not result.text or not any(value in content_type for value in ("html", "json", "markdown")):
                continue

            people = self._parse_html(result.text, result.final_url or url, method)
            if method in PROFILE_CRAWL_METHODS and (people or self._last_page_had_candidate):
                profile_parsed += 1

            for link in discover_pagination_links(
                result.text,
                result.final_url or url,
                official_domains,
                crawl,
            ):
                enqueue_discovered_url(link, "pagination", depth)
            if depth < max_depth:
                profile_limit = int(crawl.get("profile_link_limit") or 25)
                parsed_links = discover_profile_links_from_people(
                    people,
                    result.final_url or url,
                    official_domains,
                    profile_limit,
                )
                raw_links = []
                if not crawl.get("profile_links_from_parsed_people_only", False):
                    raw_links = discover_profile_links(
                        result.text,
                        result.final_url or url,
                        official_domains,
                        profile_limit,
                        crawl,
                    )
                for link in list(dict.fromkeys([*parsed_links, *raw_links]))[:profile_limit]:
                    enqueue_discovered_url(link, "profile_link", depth + 1)
            parsed_results.append((result, people))
            result.text = ""
            result.body = b""
        max_pages_reached = bool(queue) and pages_fetched >= max_pages
        seed_coverage = seed_succeeded / len(seed_urls) if seed_urls else 1.0
        profile_fetch_coverage = profile_succeeded / len(profile_links) if profile_links else 1.0
        profile_parse_coverage = profile_parsed / profile_succeeded if profile_succeeded else 1.0
        succeeded_units = [
            str(unit.get("name") or "configured_scope")
            for unit in configured_units
            if successful_seed_urls.intersection(unit.get("seed_urls") or [])
        ]
        missing_units = [
            str(unit.get("name") or "configured_scope")
            for unit in configured_units
            if not successful_seed_urls.intersection(unit.get("seed_urls") or [])
        ]
        unit_coverage = len(succeeded_units) / len(configured_units) if configured_units else 1.0
        capture_row = self.storage.conn.execute(
            """
            SELECT COUNT(*) AS requests_total,
                   COALESCE(SUM(network_bytes), 0) AS network_bytes_total,
                   COALESCE(SUM(uncompressed_bytes), 0) AS response_bytes_total,
                   COALESCE(SUM(CASE WHEN not_modified=1 THEN 1 ELSE 0 END), 0) AS not_modified_total,
                   COALESCE(SUM(CASE WHEN archive_key IS NOT NULL THEN 1 ELSE 0 END), 0) AS archived_responses
            FROM raw_sources
            WHERE run_id=?
            """,
            (self.run_id,),
        ).fetchone()
        captured_requests = int(capture_row["requests_total"] or 0)
        if captured_requests:
            network_bytes = int(capture_row["network_bytes_total"] or 0)
            response_bytes_total = int(capture_row["response_bytes_total"] or 0)
            not_modified_total = int(capture_row["not_modified_total"] or 0)
            archived_responses = int(capture_row["archived_responses"] or 0)
        else:
            captured_requests = pages_fetched
            response_bytes_total = network_bytes
            not_modified_total = pages_not_modified
            archived_responses = 0
        metrics = {
            "urls_discovered": len(all_discovered_urls),
            "pages_attempted": pages_fetched,
            "pages_succeeded": pages_succeeded,
            "pages_failed": pages_failed,
            "pages_not_modified": pages_not_modified,
            "network_bytes": network_bytes,
            "http_requests_total": captured_requests,
            "discovery_requests_attempted": max(0, captured_requests - pages_fetched),
            "response_bytes_total": response_bytes_total,
            "responses_not_modified_total": not_modified_total,
            "archived_responses": archived_responses,
            "seed_urls_expected": len(seed_urls),
            "seed_urls_attempted": seed_attempted,
            "seed_urls_succeeded": seed_succeeded,
            "seed_url_coverage": seed_coverage,
            "units_expected": len(configured_units),
            "units_succeeded": len(succeeded_units),
            "units_missing": missing_units,
            "unit_coverage": unit_coverage,
            "profile_links_discovered": len(profile_links),
            "profile_pages_attempted": profile_attempted,
            "profile_pages_succeeded": profile_succeeded,
            "profile_pages_parsed": profile_parsed,
            "profile_fetch_coverage": profile_fetch_coverage,
            "profile_parse_coverage": profile_parse_coverage,
            "pagination_links_discovered": len(pagination_links),
            "pagination_pages_attempted": pagination_attempted,
            "pagination_pages_succeeded": pagination_succeeded,
            "pagination_pages_failed": pagination_failed,
            "pagination_complete": not max_pages_reached and pagination_failed == 0,
            "queue_exhausted": not queue,
            "max_pages_reached": max_pages_reached,
            "candidate_blocks": self._candidate_blocks,
            "filtered_blocks": self._filtered_blocks,
        }
        return CrawlOutcome(parsed_results=parsed_results, metrics=metrics)

    def _parse_html(self, html_text: str, source_url: str, crawl_method: str | None = None) -> list:
        self._last_page_had_candidate = False
        parsing = self.config.get("parsing", {})
        if crawl_method in PROFILE_CRAWL_METHODS:
            preferred = parsing.get("profile_adapters") or parsing.get("preferred_adapters") or list(ADAPTERS)
        else:
            preferred = parsing.get("preferred_adapters") or list(ADAPTERS)
        all_people = []
        for adapter_name in preferred:
            adapter = ADAPTERS.get(adapter_name)
            if not adapter:
                self.logger.warning("Unknown adapter %s", adapter_name)
                continue
            try:
                people = adapter.parse(html_text, source_url, self.config)
            except Exception as exc:
                self.storage.record_crawl_error(
                    self.institution_id,
                    source_url,
                    f"parse:{adapter_name}",
                    f"{type(exc).__name__}: {exc}",
                    self.run_id,
                )
                continue
            stats_fn = getattr(adapter, "stats", None)
            if stats_fn:
                stats = stats_fn(html_text, people)
                if int(stats.get("candidate_blocks", 0)) > 0:
                    self._last_page_had_candidate = True
                self._candidate_blocks += int(stats.get("candidate_blocks", 0))
                self._filtered_blocks += int(stats.get("filtered_blocks", 0))
                self.storage.record_parse_metric(
                    self.institution_id,
                    source_url,
                    adapter_name,
                    stats.get("candidate_blocks", 0),
                    stats.get("people_extracted", len(people)),
                    stats.get("filtered_blocks", 0),
                    self.run_id,
                )
            all_people.extend(people)
        deduped = {}
        for person in all_people:
            key = (person.name.lower(), person.profile_url or person.source_url, ",".join(person.emails))
            if key not in deduped or person.confidence > deduped[key].confidence:
                deduped[key] = person
        people = list(deduped.values())
        overrides = {
            _normalized_person_name(str(name)): str(profile_url)
            for name, profile_url in (parsing.get("profile_overrides") or {}).items()
            if name and profile_url
        }
        if overrides:
            official_domains = (self.config.get("institution") or {}).get("official_domains") or []
            for person in people:
                override = overrides.get(_normalized_person_name(person.name))
                official_url = _official_profile_url(override, source_url, official_domains)
                if official_url:
                    person.profile_url = official_url
        return people
