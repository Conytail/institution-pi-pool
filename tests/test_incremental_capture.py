from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from pi_index.crawl.archive import ContentArchive
from pi_index.crawl import discovery
from pi_index.crawl.fetcher import Fetcher, blocked_interstitial_reason
from pi_index.models import RawSourceRecord
from pi_index.storage import PIIndexStorage


def test_latest_raw_source_ignores_newer_200_interstitial_error(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    common = {
        "source_url": "https://example.edu/profile",
        "source_type": "official_profile",
        "institution_id": "inst_example",
        "http_status": 200,
        "content_hash": "hash",
        "archive_key": "blobs/profile.gz",
    }
    storage.insert_raw_source(
        RawSourceRecord(
            **common,
            fetched_at="2026-07-14T00:00:00+00:00",
            error_reason=None,
            run_id="successful-run",
        )
    )
    storage.insert_raw_source(
        RawSourceRecord(
            **common,
            fetched_at="2026-07-14T01:00:00+00:00",
            error_reason="blocked_interstitial:incapsula",
            run_id="blocked-run",
        )
    )

    cached = storage.get_latest_raw_source("inst_example", common["source_url"])

    assert cached is not None
    assert cached.run_id == "successful-run"
    assert cached.error_reason is None
    storage.close()


def test_latest_raw_source_treats_percent_escape_hex_case_as_equivalent(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    stored_url = "https://example.edu/people/uta-sch%d3%a7nberg/"
    storage.insert_raw_source(
        RawSourceRecord(
            source_url=stored_url,
            source_type="official_profile",
            institution_id="inst_example",
            fetched_at="2026-07-14T00:00:00+00:00",
            http_status=200,
            content_hash="hash",
            archive_key="blobs/profile.gz",
            run_id="successful-run",
        )
    )

    cached = storage.get_latest_raw_source(
        "inst_example", "https://example.edu/people/uta-sch%D3%A7nberg/"
    )

    assert cached is not None
    assert cached.source_url == stored_url
    # Path case is potentially significant and must not be collapsed by the
    # case-insensitive SQL candidate lookup.
    assert storage.get_latest_raw_source(
        "inst_example", "https://example.edu/People/uta-sch%D3%A7nberg/"
    ) is None
    storage.close()


class _ConditionalHandler(BaseHTTPRequestHandler):
    body = ("<html><body>Jane Doe Professor</body></html>" * 200).encode()
    request_headers: list[dict[str, str]] = []
    post_bodies: list[str] = []

    def do_GET(self):  # noqa: N802 - stdlib handler API
        type(self).request_headers.append({key: value for key, value in self.headers.items()})
        if self.path == "/utf8-no-charset":
            body = '<html><head><meta charset="utf-8"></head><body>\u6559\u6388</body></html>'.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.headers.get("If-None-Match") == '"profile-v1"':
            self.send_response(304)
            self.send_header("ETag", '"profile-v1"')
            self.send_header("Last-Modified", "Mon, 13 Jul 2026 00:00:00 GMT")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("ETag", '"profile-v1"')
        self.send_header("Last-Modified", "Mon, 13 Jul 2026 00:00:00 GMT")
        self.end_headers()
        self.wfile.write(self.body)

    def do_POST(self):  # noqa: N802 - stdlib handler API
        type(self).request_headers.append({key: value for key, value in self.headers.items()})
        length = int(self.headers.get("Content-Length") or 0)
        type(self).post_bodies.append(self.rfile.read(length).decode("utf-8"))
        response = b'[{"name":"Jane Doe","desc":"Professor"}]'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format, *_args):
        return


def _start_server():
    _ConditionalHandler.request_headers = []
    _ConditionalHandler.post_bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ConditionalHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_fetcher_supports_configured_form_post_seed_requests():
    server, thread = _start_server()
    url = f"http://127.0.0.1:{server.server_port}/ajax/people"
    try:
        result = Fetcher(
            "pi-index-test/1.0",
            default_delay_seconds=0,
            respect_robots=False,
        ).fetch(
            url,
            request_method="POST",
            form_data={"request": "1", "staff_cat": "Faculty"},
        )

        assert result.status_code == 200
        assert result.content_type == "application/json; charset=utf-8"
        assert _ConditionalHandler.post_bodies == ["request=1&staff_cat=Faculty"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_known_200_interstitials_are_not_treated_as_source_pages():
    assert blocked_interstitial_reason(
        '<script src="/_Incapsula_Resource"></script>Request unsuccessful. Incapsula incident ID: 123'
    ) == "blocked_interstitial:incapsula"
    assert blocked_interstitial_reason(
        "The requested page is currently unavailable. Site is processing for example-production."
    ) == "blocked_interstitial:site_processing"
    assert blocked_interstitial_reason("<html><body>Academic staff</body></html>") is None


def test_fetcher_honors_html_meta_charset_when_http_header_omits_it():
    server, thread = _start_server()
    try:
        result = Fetcher(
            "pi-index-test/1.0",
            default_delay_seconds=0,
            respect_robots=False,
        ).fetch(f"http://127.0.0.1:{server.server_port}/utf8-no-charset")

        assert result.encoding.lower() == "utf-8"
        assert "\u6559\u6388" in result.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_content_archive_deduplicates_and_replays_binary_content(tmp_path):
    archive = ContentArchive(tmp_path / "archive")
    pdf = b"%PDF-1.7\n" + bytes(range(256)) * 4

    first = archive.store(pdf)
    second = archive.store(pdf)

    assert first.created is True
    assert second.created is False
    assert first.archive_key == second.archive_key
    assert first.uncompressed_bytes == len(pdf)
    assert archive.read(first.archive_key) == pdf
    assert len(list((tmp_path / "archive").rglob("*.gz"))) == 1


def test_content_archive_is_safe_for_concurrent_identical_writes(tmp_path):
    archive = ContentArchive(tmp_path / "archive")
    body = b"same response" * 1000

    with ThreadPoolExecutor(max_workers=8) as executor:
        entries = list(executor.map(archive.store, [body] * 24))

    assert len({entry.archive_key for entry in entries}) == 1
    assert archive.read(entries[0].archive_key) == body
    assert len(list((tmp_path / "archive").rglob("*.gz"))) == 1
    assert not list((tmp_path / "archive").rglob("*.tmp"))


def test_fetcher_uses_conditional_get_and_offline_archive_replay(tmp_path):
    storage = PIIndexStorage(tmp_path / "pool.db")
    archive = ContentArchive(tmp_path / "archive")
    institution_id = "inst_example"
    run_ids = iter(["run-1", "run-2"])

    def observer(result, source_type, crawl_method):
        storage.insert_raw_source(
            RawSourceRecord(
                source_url=result.url,
                source_type=source_type,
                institution_id=institution_id,
                fetched_at=result.fetched_at,
                http_status=result.status_code,
                content_hash=result.content_hash,
                crawl_method=crawl_method,
                error_reason=result.error,
                run_id=next(run_ids),
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

    server, thread = _start_server()
    url = f"http://127.0.0.1:{server.server_port}/profile"
    try:
        fetcher = Fetcher(
            "pi-index-test/1.0",
            default_delay_seconds=0,
            respect_robots=False,
            archive=archive,
            cache_lookup=lambda value: storage.get_latest_raw_source(institution_id, value),
            on_result=observer,
        )
        first = fetcher.fetch(url, source_type="official_profile")
        second = fetcher.fetch(url, source_type="official_profile")

        assert first.status_code == 200
        assert first.body == _ConditionalHandler.body
        assert first.network_bytes == len(_ConditionalHandler.body)
        assert first.compressed_bytes < first.uncompressed_bytes
        assert second.status_code == 304
        assert second.not_modified is True
        assert second.body == first.body
        assert second.text == first.text
        assert second.archive_key == first.archive_key
        assert second.network_bytes == 0
        conditional_headers = {
            key.lower(): value for key, value in _ConditionalHandler.request_headers[1].items()
        }
        assert conditional_headers["if-none-match"] == '"profile-v1"'
        assert conditional_headers["if-modified-since"] == "Mon, 13 Jul 2026 00:00:00 GMT"
        assert len(list((tmp_path / "archive").rglob("*.gz"))) == 1

        request_count = len(_ConditionalHandler.request_headers)
        offline_fetcher = Fetcher(
            "pi-index-test/1.0",
            default_delay_seconds=0,
            respect_robots=False,
            archive=archive,
            cache_lookup=lambda value: storage.get_latest_raw_source(institution_id, value),
            offline=True,
        )
        replay = offline_fetcher.fetch(url, source_type="official_profile")
        assert replay.not_modified is True
        assert replay.body == first.body
        assert replay.text == first.text
        assert len(_ConditionalHandler.request_headers) == request_count

        rows = storage.conn.execute(
            "SELECT run_id, http_status, not_modified, archive_key FROM raw_sources ORDER BY fetched_at"
        ).fetchall()
        assert [row["run_id"] for row in rows] == ["run-1", "run-2"]
        assert [row["http_status"] for row in rows] == [200, 304]
        assert [row["not_modified"] for row in rows] == [0, 1]
        assert rows[0]["archive_key"] == rows[1]["archive_key"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        storage.close()


def test_offline_cache_miss_does_not_touch_network(tmp_path):
    archive = ContentArchive(tmp_path / "archive")
    fetcher = Fetcher(
        "pi-index-test/1.0",
        archive=archive,
        cache_lookup=lambda _url: None,
        offline=True,
    )

    result = fetcher.fetch("https://does-not-exist.invalid/profile")

    assert result.status_code is None
    assert result.error == "offline_cache_miss"
    assert result.network_bytes == 0
    assert not list(Path(archive.root).rglob("*.gz"))


def test_offline_replay_uses_archived_post_seed_response(tmp_path):
    archive = ContentArchive(tmp_path / "archive")
    url = "https://example.edu/ajax/people"
    body = b'[{"name":"Jane Doe","desc":"Professor"}]'
    entry = archive.store(body)
    cached = RawSourceRecord(
        source_url=url,
        source_type="official_page",
        institution_id="inst_example",
        fetched_at="2026-07-14T00:00:00+00:00",
        http_status=200,
        content_hash=entry.body_sha256,
        content_type="application/json; charset=utf-8",
        encoding="utf-8",
        archive_key=entry.archive_key,
        body_sha256=entry.body_sha256,
        uncompressed_bytes=entry.uncompressed_bytes,
        compressed_bytes=entry.compressed_bytes,
    )
    fetcher = Fetcher(
        "pi-index-test/1.0",
        archive=archive,
        cache_lookup=lambda value: cached if value == url else None,
        offline=True,
    )

    result = fetcher.fetch(
        url,
        request_method="POST",
        form_data={"request": "1", "staff_cat": "Faculty"},
    )

    assert result.status_code == 304
    assert result.not_modified is True
    assert result.body == body
    assert result.error is None


def test_offline_discovery_never_invokes_serp_plugins(tmp_path, monkeypatch):
    class ExplodingProvider:
        name = "must-not-run"

        def discover_official_urls(self, *_args, **_kwargs):
            raise AssertionError("SERP plugin was invoked in offline mode")

    monkeypatch.setattr(discovery, "available_serp_keys", lambda: ["SERPER_API_KEY"])
    monkeypatch.setattr(discovery, "load_search_plugins", lambda _logger: [ExplodingProvider()])
    fetcher = Fetcher(
        "pi-index-test/1.0",
        archive=ContentArchive(tmp_path / "archive"),
        cache_lookup=lambda _url: None,
        offline=True,
    )
    config = {
        "institution": {"name": "Example University", "homepage_url": "https://example.edu"},
        "crawl": {
            "seed_urls": ["https://example.edu/faculty"],
            "max_pages": 5,
            "use_homepage_discovery": False,
            "allow_serp": True,
        },
    }

    assert discovery.discover_institution_urls(config, {}, fetcher) == [
        ("https://example.edu/faculty", "configured_seed")
    ]


def test_corrupt_cache_fails_offline_but_online_fetch_repairs_the_blob(tmp_path):
    archive = ContentArchive(tmp_path / "archive")
    entry = archive.store(_ConditionalHandler.body)
    cached = RawSourceRecord(
        source_url="placeholder",
        source_type="official_profile",
        institution_id="inst_example",
        fetched_at="2026-01-01T00:00:00+00:00",
        http_status=200,
        content_hash=entry.body_sha256,
        content_type="text/html; charset=utf-8",
        encoding="utf-8",
        etag='"profile-v1"',
        archive_key=entry.archive_key,
        body_sha256=entry.body_sha256,
        compressed_bytes=entry.compressed_bytes,
    )
    (archive.root / entry.archive_key).write_bytes(b"not-a-gzip-file")

    offline = Fetcher(
        "pi-index-test/1.0",
        archive=archive,
        cache_lookup=lambda _url: cached,
        offline=True,
    ).fetch("https://example.edu/profile")
    assert offline.error == "offline_cache_error:BadGzipFile"

    server, thread = _start_server()
    url = f"http://127.0.0.1:{server.server_port}/profile"
    cached.source_url = url
    try:
        online = Fetcher(
            "pi-index-test/1.0",
            default_delay_seconds=0,
            respect_robots=False,
            archive=archive,
            cache_lookup=lambda _url: cached,
        ).fetch(url)
        assert online.status_code == 200
        assert online.not_modified is False
        assert online.body == _ConditionalHandler.body
        sent_headers = {
            key.lower(): value for key, value in _ConditionalHandler.request_headers[0].items()
        }
        assert "if-none-match" not in sent_headers
        assert archive.read(entry.archive_key) == _ConditionalHandler.body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
