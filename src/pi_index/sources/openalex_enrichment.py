from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Protocol
import unicodedata


class OpenAlexAuthorClient(Protocol):
    def search_authors(
        self,
        name: str,
        ror_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]: ...

    def works_for_author(
        self,
        openalex_author_id: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ExternalPublicationEnrichment:
    """OpenAlex evidence kept separate from official-site evidence."""

    person_name: str
    institution_ror: str
    openalex_author_id: str
    matched_name: str
    publication_titles: list[str]
    topics: list[str]
    work_ids: list[str]
    fetched_at: str
    source: str = "openalex"
    provenance_class: str = "external_bibliographic"
    identity_rule: str = "exact_normalized_name_tokens_and_exact_ror"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalized_name_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return tuple(re.findall(r"[^\W\d_]+", normalized, flags=re.UNICODE))


def _names_equivalent(left: str, right: str) -> bool:
    left_tokens = _normalized_name_tokens(left)
    right_tokens = _normalized_name_tokens(right)
    if not left_tokens or not right_tokens or len(left_tokens) != len(right_tokens):
        return False
    return left_tokens == right_tokens or sorted(left_tokens) == sorted(right_tokens)


def _ror_key(value: str | None) -> str:
    match = re.search(r"(?:ror\.org/)?(0[a-z0-9]{8})\b", value or "", flags=re.I)
    return match.group(1).casefold() if match else ""


def _author_rors(author: dict[str, Any]) -> set[str]:
    values: list[str] = []
    for institution in author.get("last_known_institutions") or []:
        values.append(institution.get("ror") or "")
    for affiliation in author.get("affiliations") or []:
        institution = affiliation.get("institution") or {}
        values.append(institution.get("ror") or "")
    return {key for value in values if (key := _ror_key(value))}


def _author_names(author: dict[str, Any]) -> list[str]:
    return [
        value
        for value in [
            author.get("display_name") or "",
            *(author.get("display_name_alternatives") or []),
        ]
        if value
    ]


def resolve_strict_openalex_author(
    person_name: str,
    institution_ror: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return one unambiguous exact-name + exact-ROR author, otherwise none."""
    accepted = strict_openalex_author_candidates(
        [person_name],
        institution_ror,
        candidates,
    )
    return accepted[0] if len(accepted) == 1 else None


def strict_openalex_author_candidates(
    person_names: list[str],
    institution_ror: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return every exact-name + exact-ROR candidate, deduplicated by author ID."""

    target_ror = _ror_key(institution_ror)
    if not target_ror:
        return []
    normalized_names = [name for name in person_names if _normalized_name_tokens(name)]
    accepted: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for author in candidates:
        author_id = str(author.get("id") or "").casefold()
        if not author_id or author_id in seen_ids or target_ror not in _author_rors(author):
            continue
        if not any(
            _names_equivalent(person_name, author_name)
            for person_name in normalized_names
            for author_name in _author_names(author)
        ):
            continue
        seen_ids.add(author_id)
        accepted.append(author)
    return accepted


def _work_topics(work: dict[str, Any]) -> list[str]:
    values: list[str] = []
    primary = work.get("primary_topic") or {}
    if primary.get("display_name"):
        values.append(primary["display_name"])
    for topic in work.get("topics") or []:
        if topic.get("display_name"):
            values.append(topic["display_name"])
    for concept in work.get("concepts") or []:
        if concept.get("display_name") and float(concept.get("score") or 0) >= 0.5:
            values.append(concept["display_name"])
    return values


def _unique_text(values: list[str], limit: int) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join((value or "").split())
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        output.append(text)
        if len(output) >= limit:
            break
    return output


def _cache_path(cache_dir: str | Path, person_name: str, institution_ror: str) -> Path:
    digest = hashlib.sha256(
        f"{person_name.casefold()}\n{_ror_key(institution_ror)}".encode("utf-8")
    ).hexdigest()
    return Path(cache_dir) / f"{digest}.json"


def enrich_openalex_publications(
    client: OpenAlexAuthorClient,
    person_name: str,
    institution_ror: str,
    *,
    cache_dir: str | Path | None = None,
    max_works: int = 20,
    force_refresh: bool = False,
) -> ExternalPublicationEnrichment | None:
    """Optionally derive research evidence without mutating the official PI pool.

    A cache entry stores either the external enrichment or an explicit unresolved
    result.  Consumers must retain ``provenance_class`` and must not copy these
    values into official publication fingerprint storage.
    """
    cache_path = (
        _cache_path(cache_dir, person_name, institution_ror)
        if cache_dir is not None
        else None
    )
    if cache_path and cache_path.exists() and not force_refresh:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        result = payload.get("result")
        return ExternalPublicationEnrichment(**result) if result else None

    candidates = client.search_authors(person_name, ror_id=institution_ror, limit=10)
    author = resolve_strict_openalex_author(person_name, institution_ror, candidates)
    enrichment: ExternalPublicationEnrichment | None = None
    if author and author.get("id"):
        works = client.works_for_author(author["id"], limit=max_works)
        names = [name for name in _author_names(author) if _names_equivalent(person_name, name)]
        enrichment = ExternalPublicationEnrichment(
            person_name=person_name,
            institution_ror=f"https://ror.org/{_ror_key(institution_ror)}",
            openalex_author_id=author["id"],
            matched_name=names[0],
            publication_titles=_unique_text(
                [work.get("display_name") or work.get("title") or "" for work in works],
                max_works,
            ),
            topics=_unique_text(
                [topic for work in works for topic in _work_topics(work)],
                max(20, max_works),
            ),
            work_ids=_unique_text([work.get("id") or "" for work in works], max_works),
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": "openalex",
            "provenance_class": "external_bibliographic",
            "result": enrichment.to_dict() if enrichment else None,
        }
        cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return enrichment
