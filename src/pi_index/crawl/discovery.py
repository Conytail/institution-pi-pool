from __future__ import annotations

import importlib
import logging
import os
from typing import Protocol
from urllib.parse import urljoin

from .fetcher import Fetcher
from .sitemap import discover_sitemap_urls


SERP_ENV_KEYS = ["SERPER_API_KEY", "BRAVE_SEARCH_API_KEY", "BING_SEARCH_API_KEY"]


class SearchProviderPlugin(Protocol):
    name: str

    def discover_official_urls(self, institution_name: str, homepage_url: str, limit: int = 10) -> list[str]:
        ...


def available_serp_keys() -> list[str]:
    return [key for key in SERP_ENV_KEYS if os.getenv(key)]


def load_search_plugins(logger: logging.Logger | None = None) -> list[SearchProviderPlugin]:
    logger = logger or logging.getLogger(__name__)
    module_names = [m.strip() for m in os.getenv("PI_INDEX_SEARCH_PLUGIN_MODULES", "").split(",") if m.strip()]
    providers: list[SearchProviderPlugin] = []
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
            provider = module.build_provider()
            providers.append(provider)
        except Exception as exc:
            logger.warning("Search plugin %s could not be loaded: %s", module_name, exc)
    return providers


def common_official_urls(homepage_url: str, paths: list[str]) -> list[str]:
    base = homepage_url.rstrip("/") + "/"
    urls = []
    for path in paths:
        urls.append(urljoin(base, path.lstrip("/")))
    return urls


def discover_institution_urls(
    institution_config: dict,
    crawl_policy: dict,
    fetcher: Fetcher,
    logger: logging.Logger | None = None,
) -> list[tuple[str, str]]:
    """Return (url, crawl_method) pairs using no-key official discovery first."""

    logger = logger or logging.getLogger(__name__)
    inst = institution_config.get("institution", {})
    crawl = institution_config.get("crawl", {})
    homepage = inst.get("homepage_url")
    max_pages = int(crawl.get("max_pages") or 100)
    urls: list[tuple[str, str]] = []

    for seed in crawl.get("seed_urls") or []:
        urls.append((seed, "configured_seed"))

    if homepage and crawl.get("use_homepage_discovery", True):
        for url in common_official_urls(homepage, crawl_policy.get("common_official_paths") or []):
            urls.append((url, "common_official_path"))
        sitemap_limit = max(0, max_pages - len(urls))
        if sitemap_limit:
            for url in discover_sitemap_urls(homepage, fetcher, limit=sitemap_limit, logger=logger):
                urls.append((url, "sitemap"))

    if len(urls) < max_pages and crawl.get("allow_serp", True):
        keys = available_serp_keys()
        providers = load_search_plugins(logger)
        if keys and providers:
            for provider in providers:
                for url in provider.discover_official_urls(inst.get("name", ""), homepage or "", limit=max_pages - len(urls)):
                    urls.append((url, f"serp_plugin:{provider.name}"))
                    if len(urls) >= max_pages:
                        break
        elif keys and not providers:
            logger.info("SERP API key present, but no PI_INDEX_SEARCH_PLUGIN_MODULES provider is configured; skipping SERP fallback.")
        else:
            logger.info("No SERP API key available; using configured seeds, official paths, and sitemap discovery only.")
    elif len(urls) < max_pages:
        logger.info("SERP fallback disabled by institution config; using configured official discovery only.")

    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url, method in urls:
        if url not in seen:
            deduped.append((url, method))
            seen.add(url)
        if len(deduped) >= max_pages:
            break
    return deduped
