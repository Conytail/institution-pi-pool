import json

from pi_index.sources.openalex_enrichment import (
    enrich_openalex_publications,
    resolve_strict_openalex_author,
    strict_openalex_author_candidates,
)


ROR = "https://ror.org/02zhqgq86"


def _author(author_id: str, name: str, ror: str = ROR):
    return {
        "id": f"https://openalex.org/{author_id}",
        "display_name": name,
        "display_name_alternatives": [],
        "last_known_institutions": [{"ror": ror}],
    }


class FakeClient:
    def __init__(self, authors, works):
        self.authors = authors
        self.works = works
        self.author_calls = 0
        self.work_calls = 0

    def search_authors(self, name, ror_id=None, limit=5):
        self.author_calls += 1
        return self.authors

    def works_for_author(self, author_id, limit=5):
        self.work_calls += 1
        return self.works[:limit]


def test_strict_resolution_requires_both_exact_name_tokens_and_exact_ror():
    candidates = [
        _author("A1", "Yuanwei Yao", "https://ror.org/000000000"),
        _author("A2", "Yuanwei M. Yao"),
        _author("A3", "Yao Yuanwei"),
    ]

    match = resolve_strict_openalex_author("Yuanwei Yao", ROR, candidates)

    assert match["id"] == "https://openalex.org/A3"


def test_strict_resolution_rejects_ambiguous_exact_matches():
    candidates = [_author("A1", "Yuanwei Yao"), _author("A2", "Yao Yuanwei")]

    assert resolve_strict_openalex_author("Yuanwei Yao", ROR, candidates) is None


def test_strict_candidates_accept_any_canonical_alias_and_dedupe_author_ids():
    candidate = _author("A1", "Matthias Fahn")
    duplicate = dict(candidate)

    matches = strict_openalex_author_candidates(
        ["Matthias Nikolaus", "Matthias FAHN"],
        ROR,
        [candidate, duplicate],
    )

    assert matches == [candidate]


def test_enrichment_is_cached_and_explicitly_external(tmp_path):
    client = FakeClient(
        [_author("A1", "Yuanwei Yao")],
        [
            {
                "id": "https://openalex.org/W1",
                "display_name": "Causal Representation Learning for Biology",
                "primary_topic": {"display_name": "Causal Inference"},
                "topics": [{"display_name": "Single-cell biology"}],
                "concepts": [{"display_name": "Machine learning", "score": 0.8}],
            }
        ],
    )

    first = enrich_openalex_publications(
        client,
        "Yuanwei Yao",
        ROR,
        cache_dir=tmp_path,
    )
    second = enrich_openalex_publications(
        client,
        "Yuanwei Yao",
        ROR,
        cache_dir=tmp_path,
    )

    assert first == second
    assert first.provenance_class == "external_bibliographic"
    assert first.source == "openalex"
    assert first.publication_titles == ["Causal Representation Learning for Biology"]
    assert first.topics == ["Causal Inference", "Single-cell biology", "Machine learning"]
    assert client.author_calls == 1
    assert client.work_calls == 1
    payload = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["provenance_class"] == "external_bibliographic"


def test_unresolved_identity_is_negative_cached_without_fetching_works(tmp_path):
    client = FakeClient([_author("A1", "Different Person")], [])

    assert enrich_openalex_publications(client, "Yuanwei Yao", ROR, cache_dir=tmp_path) is None
    assert enrich_openalex_publications(client, "Yuanwei Yao", ROR, cache_dir=tmp_path) is None
    assert client.author_calls == 1
    assert client.work_calls == 0
