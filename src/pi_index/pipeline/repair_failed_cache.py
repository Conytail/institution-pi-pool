from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import load_institution_config
from ..crawl.archive import ContentArchive
from ..crawl.fetcher import Fetcher
from ..models import RawSourceRecord
from ..normalize.institution import institution_from_config
from ..storage import PIIndexStorage
from .ingest_institution import load_crawl_policy


def repair_failed_cache(
    config_path: str | Path,
    storage: PIIndexStorage,
    failed_run_id: str,
    crawl_policy_path: str | Path = "configs/crawl_policy.yaml",
    archive_root: str | Path | None = None,
) -> dict[str, Any]:
    """Refetch failed URLs so a subsequent offline ingestion can replay a full run."""

    config = load_institution_config(config_path)
    institution = institution_from_config(config)
    run = storage.conn.execute(
        "SELECT institution_id, status FROM ingestion_runs WHERE run_id=?",
        (failed_run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"Unknown ingestion run: {failed_run_id}")
    if run["institution_id"] != institution.institution_id:
        raise ValueError("The failed run does not belong to the configured institution")
    if run["status"] == "running":
        raise ValueError("Cannot repair a run that is still running")

    urls = [
        row["source_url"]
        for row in storage.conn.execute(
            """
            SELECT DISTINCT source_url
            FROM crawl_errors
            WHERE run_id=? AND stage='fetch' AND source_url IS NOT NULL
            ORDER BY source_url
            """,
            (failed_run_id,),
        ).fetchall()
    ]
    repair_run_id = f"cache-repair-{failed_run_id}-{uuid4().hex[:8]}"
    archive_path = Path(archive_root) if archive_root is not None else storage.db_path.parent / "raw_sources"
    archive = ContentArchive(archive_path)

    def record_fetch(result, source_type: str, crawl_method: str) -> None:
        storage.insert_raw_source(
            RawSourceRecord(
                source_url=result.url,
                source_type=source_type,
                institution_id=institution.institution_id,
                fetched_at=result.fetched_at,
                http_status=result.status_code,
                content_hash=result.content_hash,
                parser_used=None,
                crawl_method=crawl_method,
                error_reason=result.error,
                run_id=repair_run_id,
                final_url=result.final_url,
                content_type=result.content_type,
                encoding=result.encoding,
                etag=result.etag,
                last_modified=result.last_modified,
                archive_key=result.archive_key,
                body_sha256=result.content_hash if result.body else None,
                uncompressed_bytes=result.uncompressed_bytes,
                compressed_bytes=result.compressed_bytes,
                network_bytes=result.network_bytes,
                not_modified=result.not_modified,
            )
        )

    policy = load_crawl_policy(crawl_policy_path)
    crawl = config.get("crawl") or {}
    fetcher = Fetcher(
        user_agent=policy.get("user_agent", "pi-index-mvp/0.1"),
        timeout_seconds=int(crawl.get("timeout_seconds") or policy.get("timeout_seconds") or 20),
        max_retries=int(crawl.get("max_retries") or policy.get("max_retries") or 2),
        backoff_seconds=float(crawl.get("backoff_seconds") or policy.get("backoff_seconds") or 1.5),
        default_delay_seconds=float(policy.get("default_crawl_delay_seconds") or 1.0),
        respect_robots=bool(crawl.get("respect_robots_txt", policy.get("respect_robots_txt", True))),
        archive=archive,
        cache_lookup=lambda url: storage.get_latest_raw_source(institution.institution_id, url),
        on_result=record_fetch,
    )

    succeeded = 0
    failed: list[dict[str, str]] = []
    for url in urls:
        fetcher.set_domain_delay(url, crawl.get("crawl_delay_seconds"))
        result = fetcher.fetch(
            url,
            source_type="official_page",
            crawl_method="failed_cache_repair",
        )
        if result.error:
            storage.record_crawl_error(
                institution.institution_id,
                url,
                "fetch_repair",
                result.error,
                repair_run_id,
            )
            failed.append({"url": url, "reason": result.error})
        else:
            succeeded += 1

    return {
        "failed_run_id": failed_run_id,
        "repair_run_id": repair_run_id,
        "institution_id": institution.institution_id,
        "attempted": len(urls),
        "succeeded": succeeded,
        "failed": len(failed),
        "failures": failed,
        "archive_root": str(archive.root),
        "offline_replay_ready": not failed,
    }
