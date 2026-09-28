from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pi_index.sources.openalex_client import (
    OpenAlexConfigurationError,
    OpenAlexHTTPClient,
    OpenAlexHTTPError,
    OpenAlexProtocolError,
)


class FakeResponse:
    def __init__(self, status_code, payload=None, *, headers=None, text=""):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client(responses, *, sleeps=None, **kwargs):
    session = FakeSession(responses)
    recorded_sleeps = [] if sleeps is None else sleeps
    client = OpenAlexHTTPClient(
        session=session,
        sleep=recorded_sleeps.append,
        api_key="test-key",
        **kwargs,
    )
    return client, session, recorded_sleeps


def test_search_authors_returns_raw_results_and_builds_filters():
    author = {"id": "https://openalex.org/A1", "display_name": "Yuanwei Yao"}
    client, session, _sleeps = _client(
        [FakeResponse(200, {"results": [author], "meta": {"count": 1}})]
    )

    result = client.search_authors(
        "  Yuanwei   Yao ",
        ror_id="https://ror.org/02zhqgq86",
        limit=25,
        filter={"has_orcid": "true"},
    )

    assert result == [author]
    assert result[0] is author
    url, request = session.calls[0]
    assert url == "https://api.openalex.org/authors"
    assert request["timeout"] == 30.0
    assert request["params"] == {
        "search": "Yuanwei Yao",
        "per_page": 25,
        "filter": (
            "affiliations.institution.ror:https://ror.org/02zhqgq86,has_orcid:true"
        ),
        "api_key": "test-key",
    }


def test_search_authors_rejects_invalid_ror_before_network():
    client, session, _sleeps = _client([])

    with pytest.raises(ValueError, match="invalid ROR ID"):
        client.search_authors("Yuanwei Yao", ror_id="not-a-ror")

    assert session.calls == []


def test_institution_and_work_search_use_authenticated_snake_case_pages():
    institution = {"id": "https://openalex.org/I1", "display_name": "HKU"}
    work = {"id": "https://openalex.org/W1", "title": "A paper"}
    client, session, _sleeps = _client(
        [
            FakeResponse(200, {"results": [institution], "meta": {}}),
            FakeResponse(200, {"results": [work], "meta": {}}),
        ]
    )

    assert client.search_institutions("University of Hong Kong", limit=3) == [institution]
    assert client.search_works(
        "causal learning",
        limit=10,
        institution_id="https://openalex.org/i123",
        exact=True,
    ) == [work]
    assert session.calls[0][1]["params"] == {
        "search": "University of Hong Kong",
        "per_page": 3,
        "api_key": "test-key",
    }
    assert session.calls[1][1]["params"] == {
        "per_page": 10,
        "search.exact": "causal learning",
        "filter": "institutions.id:I123",
        "api_key": "test-key",
    }


def test_get_author_by_orcid_uses_singleton_and_treats_404_as_missing():
    author = {"id": "https://openalex.org/A1", "display_name": "Yang Liu"}
    client, session, _sleeps = _client(
        [
            FakeResponse(200, author),
            FakeResponse(404, text="not found"),
        ]
    )

    assert client.get_author_by_orcid("https://orcid.org/0000-0001-7187-9196") == author
    assert client.get_author_by_orcid("0000-0001-7187-9196") is None
    assert [call[0] for call in session.calls] == [
        "https://api.openalex.org/authors/orcid:0000-0001-7187-9196",
        "https://api.openalex.org/authors/orcid:0000-0001-7187-9196",
    ]


def test_get_author_by_orcid_rejects_invalid_value_before_network():
    client, session, _sleeps = _client([])

    with pytest.raises(ValueError, match="invalid ORCID"):
        client.get_author_by_orcid("not-an-orcid")

    assert session.calls == []


def test_singleton_author_and_doi_helpers_are_exact_and_404_safe():
    author = {"id": "https://openalex.org/A123", "display_name": "Researcher"}
    work = {"id": "https://openalex.org/W123", "doi": "https://doi.org/10.1/example"}
    client, session, _sleeps = _client(
        [
            FakeResponse(200, author),
            FakeResponse(404, text="missing author"),
            FakeResponse(200, work),
            FakeResponse(404, text="missing work"),
            FakeResponse(200, work),
            FakeResponse(404, text="missing work id"),
        ]
    )

    assert client.get_author("https://openalex.org/a123") == author
    assert client.get_author("A999") is None
    assert client.get_work_by_doi("https://doi.org/10.1/EXAMPLE") == work
    assert client.get_work_by_doi("doi:10.1/missing") is None
    assert client.get_work("https://openalex.org/w123") == work
    assert client.get_work("W999") is None
    assert [call[0] for call in session.calls] == [
        "https://api.openalex.org/authors/A123",
        "https://api.openalex.org/authors/A999",
        "https://api.openalex.org/works/doi:10.1/example",
        "https://api.openalex.org/works/doi:10.1/missing",
        "https://api.openalex.org/works/W123",
        "https://api.openalex.org/works/W999",
    ]


def test_singleton_doi_rejects_invalid_value_before_network():
    client, session, _sleeps = _client([])

    with pytest.raises(ValueError, match="invalid DOI"):
        client.get_work_by_doi("not-a-doi")

    assert session.calls == []


def test_legacy_openalex_module_delegates_without_silent_http_fallback(monkeypatch):
    import pi_index.sources.openalex as legacy

    calls = []

    class Delegate:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def search_authors(self, *args, **kwargs):
            calls.append(("authors", args, kwargs))
            return [{"id": "A1"}]

        def search_institutions(self, *args, **kwargs):
            calls.append(("institutions", args, kwargs))
            return [{"id": "I1"}]

        def search_works(self, *args, **kwargs):
            calls.append(("works", args, kwargs))
            return [{"id": "W1"}]

    monkeypatch.setattr(legacy, "OpenAlexHTTPClient", Delegate)
    client = legacy.OpenAlexClient(timeout=17)
    ror = "https://ror.org/02zhqgq86"

    assert client.search_authors("A Name", ror, 4)
    assert client.search_institutions("HKU", 2)
    assert client.search_works("query", 8, "I123")
    assert client.works_for_author("https://openalex.org/A123", 6)
    assert calls == [
        ("init", {"timeout": 17.0}),
        ("authors", ("A Name",), {"ror_id": ror, "limit": 4}),
        ("institutions", ("HKU",), {"limit": 2}),
        ("works", ("query",), {"limit": 8, "institution_id": "I123"}),
        (
            "works",
            ("",),
            {
                "limit": 6,
                "filter": {"author.id": "A123"},
                "sort": "publication_date:desc",
            },
        ),
    ]


def test_works_for_author_follows_every_cursor_and_returns_raw_works():
    first = {"id": "https://openalex.org/W1", "updated_date": "2026-07-01"}
    second = {"id": "https://openalex.org/W2", "updated_date": "2026-07-02"}
    client, session, _sleeps = _client(
        [
            FakeResponse(200, {"results": [first], "meta": {"next_cursor": "cursor-2"}}),
            FakeResponse(200, {"results": [second], "meta": {"next_cursor": None}}),
        ]
    )

    works = client.works_for_author(
        "https://openalex.org/a123456789",
        per_page=100,
        since_updated_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
        filter=["type:article|book", "is_retracted:false"],
        premium_updated_filter=True,
    )

    assert works == [first, second]
    assert works[0] is first
    params = [call[1]["params"] for call in session.calls]
    assert [item["cursor"] for item in params] == ["*", "cursor-2"]
    assert all(item["per_page"] == 100 for item in params)
    assert all(
        item["filter"]
        == (
            "author.id:A123456789,from_updated_date:2026-07-01T00:00:00Z,"
            "type:article|book,is_retracted:false"
        )
        for item in params
    )


def test_audited_full_cursor_result_proves_count_and_terminal_cursor():
    client, _session, _sleeps = _client(
        [
            FakeResponse(
                200,
                {
                    "results": [{"id": "https://openalex.org/W1"}],
                    "meta": {"count": 2, "next_cursor": "next"},
                },
            ),
            FakeResponse(
                200,
                {
                    "results": [{"id": "https://openalex.org/W2"}],
                    "meta": {"count": 2, "next_cursor": None},
                },
            ),
        ]
    )

    result = client.fetch_works_for_author("A123")

    assert [work["id"] for work in result.works] == [
        "https://openalex.org/W1",
        "https://openalex.org/W2",
    ]
    assert result.meta_count == 2
    assert result.pages_fetched == 2
    assert result.raw_results_count == 2
    assert result.terminal_cursor is True
    assert result.stopped_at_cutoff is False


def test_free_updated_date_delta_stops_before_requesting_an_older_cursor_page():
    client, session, _sleeps = _client(
        [
            FakeResponse(
                200,
                {
                    "results": [
                        {"id": "https://openalex.org/W1", "updated_date": "2026-07-14T00:00:00Z"},
                        {"id": "https://openalex.org/W0", "updated_date": "2026-07-09T23:59:59Z"},
                    ],
                    "meta": {"count": 1000, "next_cursor": "must-not-be-requested"},
                },
            )
        ]
    )

    result = client.fetch_works_for_author(
        "A123",
        sort="updated_date:desc",
        stop_before_updated_date="2026-07-10T00:00:00Z",
    )

    assert [work["id"] for work in result.works] == ["https://openalex.org/W1"]
    assert result.stopped_at_cutoff is True
    assert result.terminal_cursor is False
    assert len(session.calls) == 1
    assert session.calls[0][1]["params"]["sort"] == "updated_date:desc"


@pytest.mark.parametrize("value", [0, 101, True, 2.5])
def test_per_page_must_be_an_integer_not_greater_than_100(value):
    client, session, _sleeps = _client([])

    with pytest.raises(ValueError, match="between 1 and 100"):
        client.works_for_author("A123", per_page=value)

    assert session.calls == []


def test_429_honors_retry_after_then_returns_success():
    client, session, sleeps = _client(
        [
            FakeResponse(429, {"error": "rate limited"}, headers={"Retry-After": "3"}),
            FakeResponse(200, {"results": [], "meta": {"next_cursor": None}}),
        ],
        backoff_base=1.0,
    )

    assert client.works_for_author("A123") == []
    assert len(session.calls) == 2
    assert sleeps == [3.0]


def test_5xx_uses_exponential_backoff_when_retry_after_is_absent():
    client, session, sleeps = _client(
        [
            FakeResponse(503, text="temporarily unavailable"),
            FakeResponse(500, text="still unavailable"),
            FakeResponse(200, {"results": [], "meta": {"next_cursor": None}}),
        ],
        backoff_base=0.5,
    )

    assert client.works_for_author("A123") == []
    assert len(session.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_non_retryable_status_is_not_misreported_as_403():
    client, session, sleeps = _client([FakeResponse(404, text="not found")])

    with pytest.raises(OpenAlexHTTPError) as captured:
        client.search_authors("Missing Person")

    assert captured.value.status_code == 404
    assert "HTTP 404" in str(captured.value)
    assert "403" not in str(captured.value)
    assert len(session.calls) == 1
    assert sleeps == []


def test_genuine_403_is_raised_immediately_without_retry():
    client, session, sleeps = _client([FakeResponse(403, text="forbidden")])

    with pytest.raises(OpenAlexHTTPError) as captured:
        client.search_authors("Blocked Person")

    assert captured.value.status_code == 403
    assert len(session.calls) == 1
    assert sleeps == []


def test_repeated_cursor_is_rejected_instead_of_looping_forever():
    client, session, _sleeps = _client(
        [FakeResponse(200, {"results": [], "meta": {"next_cursor": "*"}})]
    )

    with pytest.raises(OpenAlexProtocolError, match="repeated cursor"):
        client.works_for_author("A123")

    assert len(session.calls) == 1


def test_api_key_is_required_and_mailto_is_not_a_substitute(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.setenv("OPENALEX_MAILTO", "legacy@example.org")

    with pytest.raises(OpenAlexConfigurationError, match="API key is required"):
        OpenAlexHTTPClient(session=FakeSession([]), sleep=lambda _delay: None)


def test_updated_date_filter_requires_explicit_premium_capability():
    client, session, _sleeps = _client([])

    with pytest.raises(OpenAlexConfigurationError, match="Premium"):
        client.works_for_author("A123", since_updated_date="2026-07-01T00:00:00Z")

    assert session.calls == []
