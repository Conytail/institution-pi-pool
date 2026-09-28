from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
from urllib.parse import urlparse
import urllib.robotparser

import requests


BLOCK_MARKERS = (
    "_incapsula_resource",
    "request unsuccessful",
    "requested page is currently unavailable",
)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _sample_urls(
    db_path: Path,
    institution_id: str,
    host: str,
    sample_size: int,
) -> list[dict[str, str | None]]:
    connection = _readonly_connection(db_path)
    try:
        rows = connection.execute(
            """
            SELECT p.person_id,
                   p.display_name,
                   p.profile_url,
                   (
                       SELECT r.last_modified
                       FROM raw_sources AS r
                       WHERE r.institution_id=p.institution_id
                         AND r.source_url=p.profile_url
                         AND r.last_modified IS NOT NULL
                       ORDER BY r.fetched_at DESC
                       LIMIT 1
                   ) AS last_modified
            FROM canonical_pi_records AS p
            WHERE p.institution_id=?
              AND p.membership_status='active'
              AND p.profile_url LIKE ?
              AND EXISTS (
                  SELECT 1
                  FROM raw_sources AS validator
                  WHERE validator.institution_id=p.institution_id
                    AND validator.source_url=p.profile_url
                    AND validator.last_modified IS NOT NULL
              )
            ORDER BY p.person_id
            LIMIT ?
            """,
            (institution_id, f"https://{host}/%", sample_size),
        ).fetchall()
    finally:
        connection.close()
    result = [dict(row) for row in rows]
    if len(result) != sample_size:
        raise RuntimeError(
            f"Requested {sample_size} active profile URLs on {host}, found {len(result)}"
        )
    for row in result:
        if urlparse(str(row["profile_url"])).hostname != host:
            raise RuntimeError(f"Unexpected host in sampled URL: {row['profile_url']}")
        if not row.get("last_modified"):
            raise RuntimeError(f"Sampled URL has no Last-Modified validator: {row['profile_url']}")
    return result


def _robots_policy(base_url: str, user_agent: str, timeout: float) -> dict[str, Any]:
    robots_url = f"{base_url.rstrip('/')}/robots.txt"
    response = requests.get(
        robots_url,
        headers={"User-Agent": user_agent},
        timeout=timeout,
        allow_redirects=True,
    )
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    if 200 <= response.status_code < 300:
        parser.parse(response.text.splitlines())
        allowed = True
    elif response.status_code in {401, 403} or response.status_code >= 500:
        allowed = False
    else:
        parser.parse([])
        allowed = True
    crawl_delay = parser.crawl_delay(user_agent) if allowed else None
    request_rate = parser.request_rate(user_agent) if allowed else None
    return {
        "url": robots_url,
        "status": response.status_code,
        "allowed": allowed,
        "crawl_delay": float(crawl_delay) if crawl_delay is not None else None,
        "request_rate": (
            {"requests": request_rate.requests, "seconds": request_rate.seconds}
            if request_rate is not None
            else None
        ),
        "parser": parser,
    }


_thread_local = threading.local()


def _session(user_agent: str) -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            }
        )
        _thread_local.session = session
    return session


def _fetch_one(
    row: dict[str, str | None],
    *,
    user_agent: str,
    timeout: float,
) -> dict[str, Any]:
    url = str(row["profile_url"])
    headers: dict[str, str] = {}
    if row.get("last_modified"):
        headers["If-Modified-Since"] = str(row["last_modified"])
    started = time.perf_counter()
    try:
        response = _session(user_agent).get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )
        elapsed = time.perf_counter() - started
        body = response.content or b""
        lower = body[:200_000].decode(response.encoding or "utf-8", errors="ignore").lower()
        blocked = any(marker in lower for marker in BLOCK_MARKERS)
        return {
            "person_id": row["person_id"],
            "name": row["display_name"],
            "url": url,
            "final_url": response.url,
            "status": response.status_code,
            "elapsed_seconds": round(elapsed, 4),
            "response_bytes": len(body),
            "retry_after": response.headers.get("Retry-After"),
            "blocked_interstitial": blocked,
            "error": None,
        }
    except requests.RequestException as error:
        return {
            "person_id": row["person_id"],
            "name": row["display_name"],
            "url": url,
            "status": None,
            "elapsed_seconds": round(time.perf_counter() - started, 4),
            "response_bytes": 0,
            "retry_after": None,
            "blocked_interstitial": False,
            "error": f"{type(error).__name__}: {error}",
        }


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _summarize(results: list[dict[str, Any]], elapsed: float, workers: int) -> dict[str, Any]:
    latencies = [float(row["elapsed_seconds"]) for row in results]
    status_counts: dict[str, int] = {}
    for row in results:
        key = str(row["status"] if row["status"] is not None else "error")
        status_counts[key] = status_counts.get(key, 0) + 1
    return {
        "workers": workers,
        "requests": len(results),
        "wall_seconds": round(elapsed, 4),
        "requests_per_second": round(len(results) / elapsed, 4) if elapsed else None,
        "status_counts": status_counts,
        "errors": sum(1 for row in results if row["error"]),
        "blocked_interstitials": sum(1 for row in results if row["blocked_interstitial"]),
        "retry_after_responses": sum(1 for row in results if row["retry_after"]),
        "response_bytes": sum(int(row["response_bytes"]) for row in results),
        "median_latency_seconds": round(sorted(latencies)[len(latencies) // 2], 4),
        "p95_latency_seconds": round(_p95(latencies), 4),
    }


def _hard_stop(
    summary: dict[str, Any],
    results: list[dict[str, Any]],
    baseline_p95: float | None,
    *,
    allow_full_responses: bool,
) -> list[str]:
    reasons: list[str] = []
    if any(row["status"] in {403, 429} for row in results):
        reasons.append("http_403_or_429")
    if summary["retry_after_responses"]:
        reasons.append("retry_after")
    if summary["blocked_interstitials"]:
        reasons.append("blocked_interstitial")
    severe = sum(
        1
        for row in results
        if row["error"] or (row["status"] is not None and int(row["status"]) >= 500)
    )
    if severe >= 2 or (results and severe / len(results) > 0.05):
        reasons.append("transport_or_5xx_error_rate")
    if (
        not allow_full_responses
        and sum(1 for row in results if row["status"] == 200) > len(results) * 0.25
    ):
        reasons.append("conditional_cache_miss_rate")
    if summary["response_bytes"] > 25 * 1024 * 1024:
        reasons.append("network_bytes_over_25mb")
    if baseline_p95 is not None and summary["p95_latency_seconds"] > max(5.0, baseline_p95 * 2):
        reasons.append("p95_latency_regression")
    return reasons


def _run_phase(
    rows: list[dict[str, str | None]],
    *,
    workers: int,
    wave_interval: float,
    user_agent: str,
    timeout: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    all_results: list[dict[str, Any]] = []
    phase_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for offset in range(0, len(rows), workers):
            wave_started = time.perf_counter()
            futures = [
                executor.submit(
                    _fetch_one,
                    row,
                    user_agent=user_agent,
                    timeout=timeout,
                )
                for row in rows[offset : offset + workers]
            ]
            wave_results = [future.result() for future in futures]
            all_results.extend(wave_results)
            if any(
                row["status"] in {403, 429}
                or row["retry_after"]
                or row["blocked_interstitial"]
                for row in wave_results
            ):
                break
            remaining = wave_interval - (time.perf_counter() - wave_started)
            if remaining > 0 and offset + workers < len(rows):
                time.sleep(remaining)
    elapsed = time.perf_counter() - phase_started
    return _summarize(all_results, elapsed, workers), all_results


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only conditional-GET concurrency probe")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--institution-id", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--sample-size", type=int, default=24)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 3, 4])
    parser.add_argument("--wave-interval", type=float, default=1.0)
    parser.add_argument("--cooldown", type=float, default=30.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--allow-full-responses", action="store_true")
    parser.add_argument(
        "--user-agent",
        default="pi-index-mvp/0.1 (+public academic indexing; operator contact configured separately)",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    rows = _sample_urls(args.db, args.institution_id, args.host, args.sample_size)
    robots = _robots_policy(f"https://{args.host}", args.user_agent, args.timeout)
    robot_parser = robots.pop("parser")
    disallowed = [row["profile_url"] for row in rows if not robot_parser.can_fetch(args.user_agent, str(row["profile_url"]))]
    if not robots["allowed"] or disallowed:
        raise RuntimeError(f"robots policy does not allow the probe: {disallowed[:3]}")
    if robots["crawl_delay"] is not None and any(worker > 1 for worker in args.workers):
        raise RuntimeError(f"robots Crawl-delay={robots['crawl_delay']} forbids the requested concurrent probe")
    if robots["request_rate"] is not None:
        allowed_rps = robots["request_rate"]["requests"] / robots["request_rate"]["seconds"]
        requested_rps = max(args.workers) / args.wave_interval
        if requested_rps > allowed_rps:
            raise RuntimeError(
                f"robots Request-rate allows {allowed_rps:.3f} rps, probe requested {requested_rps:.3f} rps"
            )

    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": str(args.db.resolve()),
        "institution_id": args.institution_id,
        "host": args.host,
        "conditional_get": True,
        "sample_size": len(rows),
        "wave_interval_seconds": args.wave_interval,
        "cooldown_seconds": args.cooldown,
        "robots": robots,
        "sample": rows,
        "phases": [],
        "pass": True,
    }
    baseline_p95: float | None = None
    for index, workers in enumerate(args.workers):
        summary, results = _run_phase(
            rows,
            workers=workers,
            wave_interval=args.wave_interval,
            user_agent=args.user_agent,
            timeout=args.timeout,
        )
        stop_reasons = _hard_stop(
            summary,
            results,
            baseline_p95,
            allow_full_responses=args.allow_full_responses,
        )
        report["phases"].append(
            {"summary": summary, "stop_reasons": stop_reasons, "results": results}
        )
        if baseline_p95 is None:
            baseline_p95 = float(summary["p95_latency_seconds"])
        if stop_reasons:
            report["pass"] = False
            break
        if index + 1 < len(args.workers) and args.cooldown > 0:
            time.sleep(args.cooldown)

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
