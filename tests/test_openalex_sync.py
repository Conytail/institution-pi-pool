from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from pi_index.pipeline.sync_openalex_publications import sync_openalex_publications
from pi_index.sources.openalex_client import OpenAlexHTTPError, OpenAlexWorksResult


ROR = "https://ror.org/02zhqgq86"
NOW = datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc)


@dataclass
class Person:
    person_id: str
    display_name: str
    institution_id: str = "inst_hku"
    ror_id: str | None = ROR
    department: str | None = "Faculty of Engineering"
    departments: list[str] = field(default_factory=list)
    external_ids: dict = field(
        default_factory=lambda: {"orcid": "https://orcid.org/0000-0001-2345-678X"}
    )
    publications_summary: dict = field(
        default_factory=lambda: {"titles": ["OFFICIAL TITLE MUST NOT BECOME A WORK"]}
    )


def _author(
    author_id="A1",
    name="Yuanwei Yao",
    ror=ROR,
    orcid="https://orcid.org/0000-0001-2345-678X",
    works_count=10,
):
    return {
        "id": f"https://openalex.org/{author_id}",
        "display_name": name,
        "display_name_alternatives": [],
        "last_known_institutions": [{"ror": ror}],
        "orcid": orcid,
        "works_count": works_count,
    }


def _work(
    work_id,
    title,
    updated_date="2026-07-14T00:00:00Z",
    *,
    doi=None,
    author_ids=None,
):
    work = {
        "id": f"https://openalex.org/{work_id}",
        "title": title,
        "updated_date": updated_date,
        "abstract_inverted_index": {"research": [0]},
        "topics": [],
    }
    if doi:
        work["doi"] = doi
    if author_ids:
        work["authorships"] = [
            {
                "author": {
                    "id": f"https://openalex.org/{author_id}",
                    "display_name": None,
                },
                "raw_author_name": None,
            }
            for author_id in author_ids
        ]
    return work


def _audited(works, *, meta_count=None, terminal_cursor=True, stopped=False):
    works = list(works)
    return OpenAlexWorksResult(
        works=works,
        meta_count=len(works) if meta_count is None else meta_count,
        pages_fetched=1,
        raw_results_count=len(works),
        terminal_cursor=terminal_cursor,
        stopped_at_cutoff=stopped,
        cursors=("*",),
    )


class FakeClient:
    def __init__(self, authors=None, work_batches=None):
        self.authors = authors or {}
        self.work_batches = {key: list(value) for key, value in (work_batches or {}).items()}
        self.author_calls = []
        self.author_get_calls = []
        self.work_search_calls = []
        self.doi_calls = []
        self.work_get_calls = []
        self.work_calls = []

    def get_author_by_orcid(self, orcid):
        matches = []
        for authors in self.authors.values():
            for author in authors:
                value = author.get("orcid") or (author.get("ids") or {}).get("orcid")
                if value and value.rstrip("/").rsplit("/", 1)[-1].upper() == orcid.upper():
                    matches.append(dict(author))
        return matches[0] if len(matches) == 1 else None

    def get_author(self, author_id):
        short_id = author_id.rstrip("/").rsplit("/", 1)[-1].upper()
        self.author_get_calls.append(short_id)
        matches = [
            dict(author)
            for authors in self.authors.values()
            for author in authors
            if author["id"].rstrip("/").rsplit("/", 1)[-1].upper() == short_id
        ]
        return matches[0] if len(matches) == 1 else None

    def search_authors(self, name, ror_id=None, limit=10, *, filter=None):
        self.author_calls.append((name, ror_id, limit))
        return list(self.authors.get(name, []))

    def _searchable_works(self):
        merged = {}
        for author_id, batches in self.work_batches.items():
            for batch in batches:
                if isinstance(batch, Exception):
                    continue
                works = batch.works if isinstance(batch, OpenAlexWorksResult) else batch
                for raw_work in works:
                    work_id = raw_work["id"].rsplit("/", 1)[-1].upper()
                    work = merged.setdefault(work_id, dict(raw_work))
                    authorships = [dict(item) for item in work.get("authorships") or []]
                    known = {
                        item.get("author", {}).get("id", "").rsplit("/", 1)[-1].upper()
                        for item in authorships
                    }
                    if author_id.upper() not in known:
                        candidate = next(
                            (
                                author
                                for authors in self.authors.values()
                                for author in authors
                                if author["id"].rsplit("/", 1)[-1].upper()
                                == author_id.upper()
                            ),
                            {},
                        )
                        authorships.append(
                            {
                                "author": {
                                    "id": f"https://openalex.org/{author_id}",
                                    "display_name": candidate.get("display_name"),
                                },
                                "raw_author_name": candidate.get("display_name"),
                            }
                        )
                    work["authorships"] = authorships
        return list(merged.values())

    def search_works(
        self,
        query,
        limit=10,
        institution_id=None,
        *,
        filter=None,
        sort=None,
        exact=False,
    ):
        self.work_search_calls.append((query, limit, exact))
        title = query.strip().strip('"').replace('\\"', '"').casefold()
        return [
            work
            for work in self._searchable_works()
            if str(work.get("title") or "").casefold() == title
        ][:limit]

    def get_work_by_doi(self, doi):
        normalized = doi.casefold().removeprefix("https://doi.org/").removeprefix("doi:")
        self.doi_calls.append(normalized)
        matches = [
            work
            for work in self._searchable_works()
            if str(work.get("doi") or "")
            .casefold()
            .removeprefix("https://doi.org/")
            == normalized
        ]
        return matches[0] if len(matches) == 1 else None

    def get_work(self, openalex_work_id):
        short_id = openalex_work_id.rstrip("/").rsplit("/", 1)[-1].upper()
        self.work_get_calls.append(short_id)
        matches = [
            work
            for work in self._searchable_works()
            if work["id"].rsplit("/", 1)[-1].upper() == short_id
        ]
        return matches[0] if len(matches) == 1 else None

    def fetch_works_for_author(
        self,
        author_id,
        *,
        per_page=100,
        since_updated_date=None,
        filter=None,
        premium_updated_filter=False,
        sort=None,
        stop_before_updated_date=None,
    ):
        short_id = author_id.rstrip("/").rsplit("/", 1)[-1]
        self.work_calls.append(
            {
                "author_id": short_id,
                "per_page": per_page,
                "since_updated_date": since_updated_date,
                "premium_updated_filter": premium_updated_filter,
                "sort": sort,
                "stop_before_updated_date": stop_before_updated_date,
            }
        )
        batches = self.work_batches.setdefault(short_id, [])
        batch = batches.pop(0) if batches else []
        if isinstance(batch, Exception):
            raise batch
        if isinstance(batch, OpenAlexWorksResult):
            return batch
        works = list(batch)
        return OpenAlexWorksResult(
            works=works,
            meta_count=len(works),
            pages_fetched=1,
            raw_results_count=len(works),
            terminal_cursor=True,
            stopped_at_cutoff=False,
            cursors=("*",),
        )


class FakeStorage:
    def __init__(self, people):
        self.people = list(people)
        self.author_links = {}
        self.works = {}
        self.person_works = {}
        self.person_work_author_ids = {}
        self.upserted_raw_works = []
        self.started = []
        self.finished = []
        self.next_queue_id = 1
        self.official_evidence = {}
        self.pending_openalex_sync_jobs = set()
        self.completed_openalex_sync_jobs = []
        self.identity_probe_cache = {}

    def iter_pi_records(self, include_inactive=False):
        return iter(self.people)

    def start_openalex_sync_run(self, run_id, institution_id=None, **kwargs):
        self.started.append((run_id, institution_id, kwargs))
        return {"run_id": run_id}

    def finish_openalex_sync_run(self, run_id, status, metrics=None, **kwargs):
        self.finished.append((run_id, status, metrics, kwargs))
        return {"run_id": run_id, "status": status}

    def get_openalex_author_link(self, person_id):
        link = self.author_links.get(person_id)
        return dict(link) if link else None

    def get_official_publication_identity_evidence(self, person_id):
        return list(self.official_evidence.get(person_id, []))

    def get_openalex_identity_probe_cache(self, probe_key):
        cached = self.identity_probe_cache.get(probe_key)
        return dict(cached) if cached else None

    def upsert_openalex_identity_probe_cache(
        self,
        probe_key,
        probe_version,
        evidence_kind,
        evidence_value,
        works,
        *,
        fetched_at,
        expires_at,
        run_id=None,
    ):
        raw_works = [dict(work) for work in works]
        works_json = json.dumps(raw_works, ensure_ascii=False, sort_keys=True)
        cached = {
            "probe_key": probe_key,
            "probe_version": probe_version,
            "evidence_kind": evidence_kind,
            "evidence_value": evidence_value,
            "result_status": "hit" if raw_works else "miss",
            "works": raw_works,
            "works_sha256": hashlib.sha256(works_json.encode()).hexdigest(),
            "fetched_at": fetched_at,
            "expires_at": expires_at,
            "last_run_id": run_id,
        }
        self.identity_probe_cache[probe_key] = cached
        return dict(cached)

    def complete_openalex_sync_jobs(self, person_id, run_id, *, completed_at=None):
        if person_id not in self.pending_openalex_sync_jobs:
            return 0
        self.pending_openalex_sync_jobs.remove(person_id)
        self.completed_openalex_sync_jobs.append((person_id, run_id, completed_at))
        return 1

    def iter_current_openalex_works(
        self, *, person_id=None, institution_id=None, openalex_author_id=None
    ):
        for work_id, relationship in self.person_works.get(person_id, {}).items():
            if relationship["status"] in {"active", "missing"}:
                yield {"openalex_work_id": work_id}

    def upsert_openalex_author_link(
        self, person_id, institution_id, openalex_author_id, **kwargs
    ):
        previous = self.author_links.get(person_id, {})
        link = {
            **previous,
            "person_id": person_id,
            "institution_id": institution_id,
            "openalex_author_id": openalex_author_id,
            **kwargs,
        }
        self.author_links[person_id] = link
        return dict(link)

    @staticmethod
    def _hash(work):
        return hashlib.sha256(
            json.dumps(work, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _job(self, entity_kind, entity_id, *, created=True):
        job = {
            "queue_id": self.next_queue_id,
            "created": created,
            "entity_kind": entity_kind,
            "entity_id": entity_id,
        }
        self.next_queue_id += 1
        return job

    def upsert_openalex_work(
        self,
        work,
        run_id,
        *,
        observed_at=None,
        dry_run=False,
        enqueue_vectors=True,
    ):
        work_id = work["id"].rsplit("/", 1)[-1].upper()
        digest = self._hash(work)
        previous = self.works.get(work_id)
        created = previous is None
        metadata_changed = previous is not None and previous["hash"] != digest
        vector_text_changed = created or metadata_changed
        jobs = [self._job("openalex_work", work_id)] if enqueue_vectors and vector_text_changed else []
        if not dry_run:
            self.works[work_id] = {"hash": digest, "work": dict(work)}
            self.upserted_raw_works.append(dict(work))
        return {
            "work_id": work_id,
            "created": created,
            "vector_text_changed": vector_text_changed,
            "metadata_changed": metadata_changed,
            "dry_run": dry_run,
            "vector_jobs": jobs,
        }

    def reconcile_openalex_person_works(
        self,
        person_id,
        institution_id,
        openalex_author_id,
        run_id,
        observed_work_ids,
        *,
        full_snapshot,
        missing_runs_before_tombstone=2,
        dry_run=False,
        observed_at=None,
        enqueue_vectors=True,
        relationship_evidence=None,
        observed_work_author_ids=None,
        archive_existing_author_link=None,
    ):
        archive_audit = None
        if archive_existing_author_link is not None:
            existing_author_link = self.author_links.get(person_id)
            expected_author_id = archive_existing_author_link.get(
                "expected_openalex_author_id"
            )
            if (
                existing_author_link is None
                or existing_author_link.get("openalex_author_id") != expected_author_id
            ):
                raise ValueError("automatic OpenAlex author link changed")
            reviewed = (relationship_evidence or {}).get("reviewed_identity") or {}
            archive_audit = {
                "action": "planned" if dry_run else "archived",
                "person_id": person_id,
                "openalex_author_id": expected_author_id,
                "match_method": existing_author_link.get("match_method"),
                "archive_reason": archive_existing_author_link.get("reason"),
                "replacement_manifest_sha256": reviewed.get("manifest_sha256"),
                "original_link_sha256": "fake-original-link-sha256",
            }
            if not dry_run:
                del self.author_links[person_id]
        current = {
            key: dict(value)
            for key, value in self.person_works.get(person_id, {}).items()
        }
        observed = set(observed_work_ids)
        categories = {key: [] for key in ("added", "recovered", "reactivated", "missing", "tombstoned", "unchanged")}
        collection_changed = False
        for work_id in observed:
            link = current.get(work_id)
            if link is None:
                current[work_id] = {
                    "status": "active",
                    "missing_streak": 0,
                    "relationship_evidence": relationship_evidence,
                }
                categories["added"].append(work_id)
                collection_changed = True
            elif link["status"] == "tombstoned":
                link.update(status="active", missing_streak=0)
                categories["reactivated"].append(work_id)
                collection_changed = True
            elif link["missing_streak"]:
                link.update(status="active", missing_streak=0)
                categories["recovered"].append(work_id)
            else:
                categories["unchanged"].append(work_id)
        if full_snapshot:
            for work_id, link in current.items():
                if work_id in observed or link["status"] == "tombstoned":
                    continue
                link["missing_streak"] += 1
                if link["missing_streak"] >= missing_runs_before_tombstone:
                    link["status"] = "tombstoned"
                    categories["tombstoned"].append(work_id)
                    collection_changed = True
                else:
                    link["status"] = "missing"
                    categories["missing"].append(work_id)
        jobs = [self._job("pi_career", person_id)] if enqueue_vectors and collection_changed else []
        if not dry_run:
            self.person_works[person_id] = current
            provenance = self.person_work_author_ids.setdefault(person_id, {})
            for work_id in observed:
                provenance[work_id] = (
                    (observed_work_author_ids or {}).get(work_id)
                    or openalex_author_id
                )
            if openalex_author_id:
                link = self.author_links[person_id]
                link["last_successful_sync_at"] = observed_at
                if full_snapshot:
                    link["last_full_sync_at"] = observed_at
        return {
            **categories,
            "author_link_archive": archive_audit,
            "counts": {
                **{key: len(value) for key, value in categories.items()},
                "collection_changes": int(collection_changed),
                "vector_jobs": len(jobs),
            },
            "vector_jobs": jobs,
        }


def _run(storage, client, **kwargs):
    kwargs.setdefault("institution_id", "inst_hku")
    kwargs.setdefault("person_ids", ["pi_1"])
    people_by_id = {person.person_id: person for person in storage.people}
    profile_names = {}
    for person_id, link in storage.author_links.items():
        author_id = str(link.get("openalex_author_id") or "").rsplit("/", 1)[-1]
        if author_id and person_id in people_by_id:
            profile_names[author_id] = people_by_id[person_id].display_name
    for authors in client.authors.values():
        for author in authors:
            author_id = str(author.get("id") or "").rsplit("/", 1)[-1]
            if author_id and author.get("display_name"):
                profile_names[author_id] = author["display_name"]
    fallback_name = storage.people[0].display_name if storage.people else "Test Scholar"
    for profile_id, batches in client.work_batches.items():
        profile_name = profile_names.get(profile_id, fallback_name)
        for batch in batches:
            if isinstance(batch, Exception):
                continue
            works = batch.works if isinstance(batch, OpenAlexWorksResult) else batch
            for work in works:
                authorships = work.setdefault("authorships", [])
                matching = [
                    item
                    for item in authorships
                    if str((item.get("author") or {}).get("id") or "")
                    .rsplit("/", 1)[-1]
                    .upper()
                    == profile_id.upper()
                ]
                if not matching:
                    authorships.append(
                        {
                            "author": {
                                "id": f"https://openalex.org/{profile_id}",
                                "display_name": profile_name,
                            },
                            "raw_author_name": profile_name,
                        }
                    )
                else:
                    for authorship in matching:
                        author = authorship.setdefault("author", {})
                        if not author.get("display_name") and not authorship.get(
                            "raw_author_name"
                        ):
                            author["display_name"] = profile_name
                            authorship["raw_author_name"] = profile_name
    return sync_openalex_publications(
        storage,
        client=client,
        run_id=kwargs.pop("run_id", "sync_test"),
        now=lambda: NOW,
        **kwargs,
    )


def _write_reviewed_identity_manifest(path, *links, reviewed_by="unit-test"):
    payload = {
        "schema_version": 1,
        "audit_type": "reviewed_openalex_identity_manifest",
        "reviewed_at": "2026-07-15T03:00:00Z",
        "reviewed_by": reviewed_by,
        "links": list(links),
    }
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return payload


def _reviewed_link(
    *,
    person_id="pi_1",
    institution_id="inst_hku",
    display_name="Reviewed Scholar",
    primary="A1",
    author_ids=None,
    sync_mode="full_profile",
    official_works=None,
    work_policy=None,
    authorship_name_aliases=None,
    reason="Official profile and publication authorship were manually reviewed",
):
    payload = {
        "person_id": person_id,
        "institution_id": institution_id,
        "expected_display_name": display_name,
        "primary_openalex_author_id": primary,
        "confirmed_openalex_author_ids": list(
            author_ids if author_ids is not None else ([primary] if primary else [])
        ),
        "sync_mode": sync_mode,
        "reason": reason,
    }
    if official_works is not None:
        payload["official_works"] = list(official_works)
    if work_policy is not None:
        payload["work_policy"] = dict(work_policy)
    if authorship_name_aliases is not None:
        payload["authorship_name_aliases"] = list(authorship_name_aliases)
    return payload


def test_reviewed_identity_manifest_persists_audited_split_profiles_and_reuses_all(
    tmp_path,
):
    person = Person("pi_1", "Reviewed Scholar", external_ids={})
    storage = FakeStorage([person])
    manifest = tmp_path / "reviewed-openalex-identities.json"
    _write_reviewed_identity_manifest(
        manifest,
        _reviewed_link(primary="A2", author_ids=["A1", "A2"]),
    )
    first_client = FakeClient(
        work_batches={
            "A1": [[_work("W1", "First profile paper")]],
            "A2": [[_work("W2", "Primary profile paper")]],
        }
    )

    result = _run(
        storage,
        first_client,
        reviewed_identity_manifest=manifest,
        full=True,
    )

    assert result["people_resolved"] == 1
    assert result["people"][0]["resolution"] == "reviewed_openalex_identity_manifest_v1"
    assert [call["author_id"] for call in first_client.work_calls] == ["A2", "A1"]
    assert first_client.author_calls == []
    link = storage.author_links["pi_1"]
    assert link["openalex_author_id"] == "A2"
    assert link["match_method"] == "reviewed_openalex_identity_manifest_v1"
    assert link["evidence"]["confirmed_openalex_author_ids"] == ["A2", "A1"]
    assert link["evidence"]["split_profile"] is True
    reviewed = link["evidence"]["reviewed_identity"]
    assert reviewed["reviewed"] is True
    assert reviewed["reason"].startswith("Official profile")
    assert reviewed["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()

    second_client = FakeClient(
        work_batches={
            "A2": [[_work("W2", "Primary profile paper")]],
            "A1": [[_work("W1", "First profile paper")]],
        }
    )
    second = _run(storage, second_client, run_id="sync_again")

    assert second["people_resolved"] == 1
    assert [call["author_id"] for call in second_client.work_calls] == ["A2", "A1"]


def test_reviewed_identity_manifest_dry_run_does_not_persist_link_or_works(tmp_path):
    person = Person("pi_1", "Reviewed Scholar", external_ids={})
    storage = FakeStorage([person])
    manifest = tmp_path / "reviewed-openalex-identities.json"
    _write_reviewed_identity_manifest(manifest, _reviewed_link())
    client = FakeClient(work_batches={"A1": [[_work("W1", "Reviewed paper")]]})

    result = _run(
        storage,
        client,
        reviewed_identity_manifest=manifest,
        dry_run=True,
        full=True,
    )

    assert result["people_resolved"] == 1
    assert storage.author_links == {}
    assert storage.works == {}
    assert storage.person_works == {}
    assert storage.started == []


def test_reviewed_official_evidence_only_links_work_without_fake_author_id(tmp_path):
    title = "A reviewed 2026 international business paper"
    doi = "10.1057/s41267-026-00839-w"
    person = Person("pi_1", "Yan PAN", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": title, "doi": doi}]
    storage.pending_openalex_sync_jobs.add("pi_1")
    manifest = tmp_path / "reviewed-work-only.json"
    _write_reviewed_identity_manifest(
        manifest,
        _reviewed_link(
            display_name="Yan PAN",
            primary=None,
            author_ids=[],
            sync_mode="official_evidence_only",
            official_works=[{"doi": doi, "expected_title": title}],
        ),
    )
    work = _work("W77", title, doi=f"https://doi.org/{doi}")
    client = FakeClient(work_batches={"A_SOURCE": [[work]]})

    result = _run(storage, client, reviewed_identity_manifest=manifest)

    assert result["people_resolved"] == 1
    assert result["people_official_evidence_only"] == 1
    assert result["people_identity_pending"] == 1
    assert result["people"][0]["identity_status"] == "pending"
    assert storage.author_links == {}
    assert set(storage.person_works["pi_1"]) == {"W77"}
    relationship = storage.person_works["pi_1"]["W77"]
    assert relationship["relationship_evidence"]["identity_status"] == "pending"
    assert result["vector_jobs"] == 2
    assert "pi_1" in storage.pending_openalex_sync_jobs
    assert result["source_sync_jobs_completed"] == 0


def test_reviewed_official_evidence_only_archives_old_automatic_author_link(
    tmp_path,
):
    title = "Reviewed business evidence"
    person = Person("pi_1", "Yan PAN", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": title, "doi": None}]
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A9",
        "link_status": "confirmed",
        "match_method": "automatic_old_rule",
        "evidence": {"confirmed_openalex_author_ids": ["A9"]},
    }
    storage.person_works["pi_1"] = {
        "W900": {"status": "active", "missing_streak": 0},
    }
    manifest = tmp_path / "reviewed-work-only-replaces-auto.json"
    _write_reviewed_identity_manifest(
        manifest,
        _reviewed_link(
            display_name="Yan PAN",
            primary=None,
            author_ids=[],
            sync_mode="official_evidence_only",
            official_works=[{"openalex_work_id": "W77", "expected_title": title}],
        ),
    )

    result = _run(
        storage,
        FakeClient(work_batches={"A_SOURCE": [[_work("W77", title)]]}),
        reviewed_identity_manifest=manifest,
        missing_runs_before_tombstone=1,
    )

    assert result["status"] == "success"
    assert result["automatic_author_links_archived_for_identity_pending"] == 1
    assert result["automatic_author_links_archive_planned_for_identity_pending"] == 0
    assert storage.author_links == {}
    assert storage.person_works["pi_1"]["W900"]["status"] == "tombstoned"
    archive = result["people"][0]["snapshot_audit"][
        "superseded_automatic_author_link"
    ]
    assert archive["action"] == "archived"
    assert archive["openalex_author_id"] == "A9"
    assert archive["replacement_manifest_sha256"]


def test_reviewed_official_evidence_only_dry_run_plans_without_archiving_auto_link(
    tmp_path,
):
    title = "Reviewed business evidence"
    person = Person("pi_1", "Yan PAN", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": title, "doi": None}]
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A9",
        "link_status": "confirmed",
        "match_method": "automatic_old_rule",
        "evidence": {"confirmed_openalex_author_ids": ["A9"]},
    }
    manifest = tmp_path / "reviewed-work-only-dry-run.json"
    _write_reviewed_identity_manifest(
        manifest,
        _reviewed_link(
            display_name="Yan PAN",
            primary=None,
            author_ids=[],
            sync_mode="official_evidence_only",
            official_works=[{"openalex_work_id": "W77", "expected_title": title}],
        ),
    )

    result = _run(
        storage,
        FakeClient(work_batches={"A_SOURCE": [[_work("W77", title)]]}),
        reviewed_identity_manifest=manifest,
        dry_run=True,
        missing_runs_before_tombstone=1,
    )

    assert result["automatic_author_links_archived_for_identity_pending"] == 0
    assert result["automatic_author_links_archive_planned_for_identity_pending"] == 1
    assert storage.author_links["pi_1"]["openalex_author_id"] == "A9"
    archive = result["people"][0]["snapshot_audit"][
        "superseded_automatic_author_link"
    ]
    assert archive["action"] == "planned"


def test_reviewed_official_work_policy_tombstones_legacy_profile_pollution(
    tmp_path,
):
    title = "Reviewed business evidence"
    person = Person("pi_1", "Yan PAN", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": title, "doi": None}]
    storage.person_works["pi_1"] = {
        "W900": {"status": "active", "missing_streak": 0},
    }
    manifest = tmp_path / "reviewed-work-only.json"
    _write_reviewed_identity_manifest(
        manifest,
        _reviewed_link(
            display_name="Yan PAN",
            primary=None,
            author_ids=[],
            sync_mode="official_evidence_only",
            official_works=[
                {"openalex_work_id": "W77", "expected_title": title}
            ],
        ),
    )
    client = FakeClient(work_batches={"A_SOURCE": [[_work("W77", title)]]})

    result = _run(
        storage,
        client,
        reviewed_identity_manifest=manifest,
        missing_runs_before_tombstone=1,
    )

    assert result["people"][0]["links_tombstoned"] == 1
    assert storage.person_works["pi_1"]["W900"]["status"] == "tombstoned"
    assert storage.person_works["pi_1"]["W77"]["status"] == "active"


def test_reviewed_official_work_supports_audited_openalex_title_variant(tmp_path):
    official_title = "Volatility, Intermediaries, and Exchange Rate"
    openalex_title = "Volatility, intermediaries, and exchange rates"
    person = Person("pi_1", "Yang LIU", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [
        {"title": official_title, "doi": None}
    ]
    manifest = tmp_path / "reviewed-title-variant.json"
    _write_reviewed_identity_manifest(
        manifest,
        _reviewed_link(
            display_name="Yang LIU",
            primary=None,
            author_ids=[],
            sync_mode="official_evidence_only",
            official_works=[
                {
                    "openalex_work_id": "W77",
                    "expected_title": official_title,
                    "expected_openalex_title": openalex_title,
                }
            ],
        ),
    )
    client = FakeClient(
        work_batches={"A_SOURCE": [[_work("W77", openalex_title)]]}
    )

    result = _run(storage, client, reviewed_identity_manifest=manifest)

    assert result["status"] == "success"
    assert set(storage.works) == {"W77"}


def test_reviewed_field_allowlist_audits_raw_profile_then_selects_policy_subset(
    tmp_path,
):
    person = Person("pi_1", "Reviewed Scholar", external_ids={})
    storage = FakeStorage([person])
    manifest = tmp_path / "reviewed-field-policy.json"
    policy = {
        "mode": "field_allowlist",
        "fields": ["Economics"],
        "always_include_work_ids": ["W3"],
    }
    _write_reviewed_identity_manifest(
        manifest,
        _reviewed_link(work_policy=policy),
    )
    economics = _work("W1", "Economics paper")
    economics["primary_topic"] = {"field": {"display_name": "Economics"}}
    excluded = _work("W2", "Computer science contamination")
    excluded["primary_topic"] = {"field": {"display_name": "Computer Science"}}
    forced = _work("W3", "Reviewed exception")
    forced["primary_topic"] = {"field": {"display_name": "Computer Science"}}
    client = FakeClient(work_batches={"A1": [[economics, excluded, forced]]})

    result = _run(
        storage,
        client,
        reviewed_identity_manifest=manifest,
        full=True,
    )

    person_result = result["people"][0]
    assert person_result["works_fetched"] == 3
    assert person_result["works_selected"] == 2
    assert set(storage.works) == {"W1", "W3"}
    assert result["works_policy_excluded"] == 1
    audit = person_result["snapshot_audit"]
    assert audit["unique_work_count"] == 3
    assert audit["selected_union_work_count"] == 2
    assert person_result["work_policy"]["policy_sha256"] == hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    # The persisted coverage limit cannot silently fall back to a future full
    # profile sync when the reviewed manifest is omitted.
    retry_client = FakeClient(work_batches={"A1": [[economics, excluded, forced]]})
    retry = _run(storage, retry_client, run_id="policy_without_manifest")
    assert retry["unresolved_reasons"] == {
        "reviewed_identity_manifest_required_for_limited_coverage": 1
    }
    assert retry_client.work_calls == []


def test_reviewed_exact_work_allowlist_selects_only_ids_and_audits_snapshot(tmp_path):
    person = Person("pi_1", "Reviewed Scholar", external_ids={})
    storage = FakeStorage([person])
    manifest = tmp_path / "reviewed-exact-work-policy.json"
    policy = {
        "mode": "exact_work_allowlist",
        "work_ids": ["W1", "https://openalex.org/W3"],
    }
    _write_reviewed_identity_manifest(
        manifest,
        _reviewed_link(work_policy=policy),
    )
    client = FakeClient(
        work_batches={
            "A1": [[
                _work("W1", "Reviewed first paper"),
                _work("W2", "Profile contamination"),
                _work("W3", "Reviewed second paper"),
            ]]
        }
    )

    result = _run(
        storage,
        client,
        reviewed_identity_manifest=manifest,
        full=True,
    )

    assert result["status"] == "success"
    assert result["people_resolved"] == 1
    assert set(storage.works) == {"W1", "W3"}
    audit = result["people"][0]["snapshot_audit"]["reviewed_work_policy"]
    assert audit == {
        "mode": "exact_work_allowlist",
        "policy_sha256": hashlib.sha256(
            json.dumps(
                {"mode": "exact_work_allowlist", "work_ids": ["W1", "W3"]},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "work_ids": ["W1", "W3"],
        "missing_work_ids": [],
        "raw_work_count": 3,
        "selected_work_count": 2,
        "excluded_work_count": 1,
    }


def test_reviewed_exact_work_allowlist_missing_id_fails_closed_and_is_not_resolved(
    tmp_path,
):
    person = Person("pi_1", "Reviewed Scholar", external_ids={})
    storage = FakeStorage([person])
    manifest = tmp_path / "reviewed-exact-work-policy-missing.json"
    _write_reviewed_identity_manifest(
        manifest,
        _reviewed_link(
            work_policy={
                "mode": "exact_work_allowlist",
                "work_ids": ["W1", "W404"],
            }
        ),
    )

    result = _run(
        storage,
        FakeClient(work_batches={"A1": [[_work("W1", "Only returned paper")]]}),
        reviewed_identity_manifest=manifest,
        full=True,
    )

    assert result["status"] == "failed"
    assert result["people_resolved"] == 0
    assert result["people_failed"] == 1
    assert "W404=missing_from_complete_profile" in result["people"][0]["error"]
    assert storage.works == {}


def test_reviewed_authorship_alias_is_local_audited_and_accepts_profile_work(tmp_path):
    person = Person("pi_1", "Jeffrey Tat Chee Ng", external_ids={})
    storage = FakeStorage([person])
    manifest = tmp_path / "reviewed-authorship-alias.json"
    _write_reviewed_identity_manifest(
        manifest,
        _reviewed_link(
            display_name="Jeffrey Tat Chee Ng",
            authorship_name_aliases=["Jeffrey Ng"],
        ),
    )
    work = _work("W1", "Reviewed alias paper", author_ids=["A1"])
    work["authorships"][0]["author"]["display_name"] = "Jeffrey Ng"
    work["authorships"][0]["raw_author_name"] = "Jeffrey Ng"

    result = _run(
        storage,
        FakeClient(work_batches={"A1": [[work]]}),
        reviewed_identity_manifest=manifest,
        full=True,
    )

    assert result["status"] == "success"
    assert result["people_resolved"] == 1
    assert set(storage.works) == {"W1"}
    assert result["people"][0]["snapshot_audit"][
        "reviewed_authorship_name_aliases"
    ] == ["Jeffrey Ng"]
    assert result["people"][0]["authorship_validation"]["A1"][
        "compatible_work_count"
    ] == 1
    assert storage.author_links["pi_1"]["evidence"]["reviewed_identity"][
        "authorship_name_aliases"
    ] == ["Jeffrey Ng"]
    assert not hasattr(person, "aliases")


def test_exact_work_allowlist_uses_reviewed_alias_for_official_slot_override(tmp_path):
    person = Person("pi_1", "Jeffrey Tat Chee Ng", external_ids={})
    storage = FakeStorage([person])
    title = "Audited shifted authorship paper"
    storage.official_evidence["pi_1"] = [{"title": title, "doi": None}]
    manifest = tmp_path / "reviewed-exact-override.json"
    _write_reviewed_identity_manifest(
        manifest,
        _reviewed_link(
            display_name="Jeffrey Tat Chee Ng",
            authorship_name_aliases=["Jeffrey Ng"],
            work_policy={"mode": "exact_work_allowlist", "work_ids": ["W7"]},
        ),
    )
    shifted = _work("W7", title, author_ids=["A1", "A2"])
    shifted["authorships"][0]["author"]["display_name"] = "Different Person"
    shifted["authorships"][0]["raw_author_name"] = "Different Person"
    shifted["authorships"][1]["author"]["display_name"] = "Jeffrey Ng"
    shifted["authorships"][1]["raw_author_name"] = "Jeffrey Ng"
    ordinary = _work("W8", "Compatible but not allowlisted", author_ids=["A1"])
    ordinary["authorships"][0]["author"]["display_name"] = "Jeffrey Ng"
    ordinary["authorships"][0]["raw_author_name"] = "Jeffrey Ng"

    result = _run(
        storage,
        FakeClient(work_batches={"A1": [[shifted, ordinary]]}),
        reviewed_identity_manifest=manifest,
        full=True,
    )

    assert result["status"] == "success"
    assert result["people_resolved"] == 1
    assert set(storage.works) == {"W7"}
    override = result["people"][0]["snapshot_audit"][
        "reviewed_official_authorship_overrides"
    ]
    assert override["accepted_work_ids"] == ["W7"]
    assert result["people"][0]["work_policy"]["work_ids"] == ["W7"]


@pytest.mark.parametrize(
    ("aliases", "sync_mode", "match"),
    [
        (["Jeffrey"], "full_profile", "at least two name tokens"),
        (["Jeffrey Wong"], "full_profile", "surname must match"),
        (["Tatiana Ng"], "full_profile", "substantive given-name token"),
        (["Jeffrey Ng"], "official_evidence_only", "only valid for full_profile"),
    ],
)
def test_reviewed_authorship_alias_manifest_validation_is_strict(
    tmp_path,
    aliases,
    sync_mode,
    match,
):
    person = Person("pi_1", "Jeffrey Tat Chee Ng", external_ids={})
    storage = FakeStorage([person])
    manifest = tmp_path / f"invalid-alias-{sync_mode}.json"
    kwargs = {
        "display_name": "Jeffrey Tat Chee Ng",
        "authorship_name_aliases": aliases,
        "sync_mode": sync_mode,
    }
    if sync_mode == "official_evidence_only":
        kwargs.update(
            primary=None,
            author_ids=[],
            official_works=[
                {"openalex_work_id": "W1", "expected_title": "Official work"}
            ],
        )
        storage.official_evidence["pi_1"] = [
            {"title": "Official work", "doi": None}
        ]
    _write_reviewed_identity_manifest(manifest, _reviewed_link(**kwargs))

    with pytest.raises(ValueError, match=match):
        _run(
            storage,
            FakeClient(work_batches={"A1": [[_work("W1", "Official work")]]}),
            reviewed_identity_manifest=manifest,
            full=True,
        )


def test_reviewed_official_work_can_override_shifted_openalex_author_slot(
    tmp_path,
):
    person = Person("pi_1", "Mengzhou Zhuang", external_ids={})
    storage = FakeStorage([person])
    title = "Tales of Two Channels"
    storage.official_evidence["pi_1"] = [{"title": title, "doi": None}]
    manifest = tmp_path / "reviewed-shifted-slot.json"
    _write_reviewed_identity_manifest(
        manifest,
        _reviewed_link(
            display_name="Mengzhou Zhuang",
            work_policy={
                "mode": "field_allowlist",
                "fields": ["Business, Management and Accounting"],
                "always_include_work_ids": ["W2"],
            },
        ),
    )
    ordinary = _work("W1", "Ordinary profile work", author_ids=["A1"])
    ordinary["primary_topic"] = {
        "field": {"display_name": "Business, Management and Accounting"}
    }
    shifted = _work("W2", title, author_ids=["A1", "A2"])
    shifted["authorships"][0]["author"]["display_name"] = "Beibei Dong"
    shifted["authorships"][0]["raw_author_name"] = "Beibei Dong"
    shifted["authorships"][1]["author"]["display_name"] = "Eric Fang"
    shifted["authorships"][1]["raw_author_name"] = "Mengzhou Zhuang"
    shifted["primary_topic"] = {"field": {"display_name": "Social Sciences"}}
    client = FakeClient(work_batches={"A1": [[ordinary, shifted]]})

    result = _run(
        storage,
        client,
        reviewed_identity_manifest=manifest,
        full=True,
    )

    assert result["status"] == "success"
    assert set(storage.works) == {"W1", "W2"}
    assert result["works_reviewed_authorship_overrides"] == 1
    override = result["people"][0]["snapshot_audit"][
        "reviewed_official_authorship_overrides"
    ]
    assert override["accepted_work_ids"] == ["W2"]
    assert override["rejected_work_ids"] == {}


def test_full_profile_filters_wrong_authorship_name_and_reports_fraction():
    person = Person("pi_1", "Lionel Zhepeng LI", external_ids={})
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A1",
        "link_status": "confirmed",
    }
    good = _work("W1", "Correct paper", author_ids=["A1"])
    good["authorships"][0]["author"]["display_name"] = "L. Z. Li"
    good["authorships"][0]["raw_author_name"] = "L. Z. Li"
    wrong = _work("W2", "Merged contamination", author_ids=["A1"])
    wrong["authorships"][0]["author"]["display_name"] = "Zixuan Li"
    wrong["authorships"][0]["raw_author_name"] = "Zixuan Li"
    client = FakeClient(work_batches={"A1": [[good, wrong]]})

    result = _run(storage, client, full=True)

    assert result["people_resolved"] == 1
    assert set(storage.works) == {"W1"}
    assert result["works_authorship_rejected"] == 1
    audit = result["people"][0]["authorship_validation"]["A1"]
    assert audit["compatible_fraction"] == 0.5
    assert audit["rejected_reasons"] == {
        "incompatible_profile_authorship_name": 1
    }


def test_full_profile_with_zero_compatible_authorship_works_fails_closed():
    person = Person("pi_1", "Yi TANG", external_ids={})
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A1",
        "link_status": "confirmed",
    }
    wrong = _work("W1", "Wrong merged paper", author_ids=["A1"])
    wrong["authorships"][0]["author"]["display_name"] = "H. J. Wang"
    wrong["authorships"][0]["raw_author_name"] = "H. J. Wang"

    result = _run(storage, FakeClient(work_batches={"A1": [[wrong]]}), full=True)

    assert result["status"] == "failed"
    assert result["people_failed"] == 1
    assert "zero_compatible_authorship_works" in result["people"][0]["error"]
    assert storage.person_works == {}


def test_strict_name_ror_identity_support_requires_compatible_work_authorship_name():
    person = Person("pi_1", "Yi TANG", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": "Official paper", "doi": None}]
    wrong = _work("W1", "Official paper", author_ids=["A1"])
    wrong["authorships"][0]["author"]["display_name"] = "H. J. Wang"
    wrong["authorships"][0]["raw_author_name"] = "H. J. Wang"
    client = FakeClient(
        authors={"Yi TANG": [_author("A1", "Yi Tang", orcid=None)]},
        work_batches={"A1": [[wrong]]},
    )

    result = _run(storage, client)

    assert result["people_resolved"] == 0
    assert result["unresolved_reasons"] == {
        "official_publication_did_not_match_candidate_works": 1
    }


@pytest.mark.parametrize(
    ("link_changes", "message"),
    [
        ({"institution_id": "inst_other"}, "institution mismatch"),
        ({"expected_display_name": "Different Scholar"}, "display name mismatch"),
        ({"primary_openalex_author_id": "not-an-author"}, "exact OpenAlex Author ID"),
        (
            {
                "primary_openalex_author_id": "A2",
                "confirmed_openalex_author_ids": ["A1"],
            },
            "primary Author ID must be included",
        ),
    ],
)
def test_reviewed_identity_manifest_preflight_fails_before_network(
    tmp_path, link_changes, message
):
    person = Person("pi_1", "Reviewed Scholar", external_ids={})
    storage = FakeStorage([person])
    manifest = tmp_path / "invalid-reviewed-openalex-identities.json"
    link = _reviewed_link()
    link.update(link_changes)
    _write_reviewed_identity_manifest(manifest, link)
    client = FakeClient(work_batches={"A1": [[_work("W1", "Never fetched")]]})

    with pytest.raises(ValueError, match=message):
        _run(storage, client, reviewed_identity_manifest=manifest)

    assert client.author_calls == []
    assert client.work_calls == []
    assert storage.started == []


def test_reviewed_identity_manifest_conflicting_confirmed_link_fails_closed(tmp_path):
    person = Person("pi_1", "Reviewed Scholar", external_ids={})
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A9",
        "link_status": "confirmed",
        "evidence": {
            "confirmed_openalex_author_ids": ["A9"],
            "reviewed_identity": {"reviewed": True},
        },
    }
    manifest = tmp_path / "conflicting-reviewed-openalex-identities.json"
    _write_reviewed_identity_manifest(manifest, _reviewed_link())
    client = FakeClient()

    with pytest.raises(ValueError, match="conflicts with existing reviewed"):
        _run(storage, client, reviewed_identity_manifest=manifest)

    assert storage.author_links["pi_1"]["openalex_author_id"] == "A9"
    assert storage.started == []


@pytest.mark.parametrize(
    "existing_link",
    [
        {
            "match_method": "automatic_old_rule",
            "evidence": {"reviewed_identity": {"manifest_sha256": "legacy"}},
        },
        {
            "match_method": "manual_review",
            "evidence": {"confirmed_openalex_author_ids": ["A9"]},
        },
    ],
)
def test_identity_pending_manifest_never_archives_existing_reviewed_link(
    tmp_path, existing_link
):
    title = "Reviewed business evidence"
    person = Person("pi_1", "Yan PAN", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": title, "doi": None}]
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A9",
        "link_status": "confirmed",
        **existing_link,
    }
    manifest = tmp_path / "identity-pending-conflicts-reviewed.json"
    _write_reviewed_identity_manifest(
        manifest,
        _reviewed_link(
            display_name="Yan PAN",
            primary=None,
            author_ids=[],
            sync_mode="official_evidence_only",
            official_works=[{"openalex_work_id": "W77", "expected_title": title}],
        ),
    )
    client = FakeClient(work_batches={"A_SOURCE": [[_work("W77", title)]]})

    with pytest.raises(ValueError, match="conflicts with existing reviewed"):
        _run(storage, client, reviewed_identity_manifest=manifest)

    assert storage.author_links["pi_1"]["openalex_author_id"] == "A9"
    assert storage.started == []
    assert client.work_get_calls == []


def test_reviewed_identity_manifest_can_replace_an_automatic_confirmed_link(tmp_path):
    person = Person("pi_1", "Reviewed Scholar", external_ids={})
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A9",
        "link_status": "confirmed",
        "match_method": "automatic_old_rule",
        "evidence": {"confirmed_openalex_author_ids": ["A9"]},
    }
    manifest = tmp_path / "replace-auto-link.json"
    _write_reviewed_identity_manifest(manifest, _reviewed_link())
    client = FakeClient(work_batches={"A1": [[_work("W1", "Reviewed work")]]})

    result = _run(
        storage,
        client,
        reviewed_identity_manifest=manifest,
        missing_runs_before_tombstone=1,
    )

    assert result["people_resolved"] == 1
    assert storage.author_links["pi_1"]["openalex_author_id"] == "A1"
    assert storage.author_links["pi_1"]["match_method"] == (
        "reviewed_openalex_identity_manifest_v1"
    )


def test_revalidate_identities_replaces_auto_link_and_tombstones_excluded_works():
    person = Person("pi_1", "Correct Scholar", external_ids={})
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A9",
        "link_status": "confirmed",
        "match_method": "automatic_old_rule",
        "evidence": {"confirmed_openalex_author_ids": ["A9"]},
    }
    storage.person_works["pi_1"] = {
        "W900": {"status": "active", "missing_streak": 0}
    }
    storage.official_evidence["pi_1"] = [{"title": "Correct official work", "doi": None}]
    client = FakeClient(
        authors={
            person.display_name: [
                _author("A1", person.display_name, orcid=None, works_count=1)
            ]
        },
        work_batches={"A1": [[_work("W100", "Correct official work")]]},
    )

    result = _run(
        storage,
        client,
        revalidate_identities=True,
        missing_runs_before_tombstone=1,
    )

    assert result["people_resolved"] == 1
    assert result["identities_revalidation_requested"] == 1
    assert result["identities_marked_stale_for_revalidation"] == 1
    assert storage.author_links["pi_1"]["openalex_author_id"].endswith("/A1")
    assert storage.author_links["pi_1"]["link_status"] == "confirmed"
    assert storage.person_works["pi_1"]["W900"]["status"] == "tombstoned"
    assert storage.person_works["pi_1"]["W100"]["status"] == "active"


def test_unresolved_auto_identity_revalidation_leaves_old_link_stale():
    person = Person("pi_1", "Unresolved Scholar", external_ids={})
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A9",
        "link_status": "confirmed",
        "match_method": "automatic_old_rule",
    }
    storage.official_evidence["pi_1"] = [{"title": "Missing evidence", "doi": None}]
    client = FakeClient(
        authors={
            person.display_name: [
                _author("A1", person.display_name, orcid=None, works_count=1)
            ]
        }
    )

    result = _run(storage, client, revalidate_identities=True)

    assert result["people_unresolved"] == 1
    assert storage.author_links["pi_1"]["openalex_author_id"] == "A9"
    assert storage.author_links["pi_1"]["link_status"] == "stale"


def test_first_sync_strictly_resolves_then_builds_a_complete_openalex_baseline():
    person = Person("pi_1", "Yuanwei Yao")
    storage = FakeStorage([person])
    client = FakeClient(
        authors={person.display_name: [_author()]},
        work_batches={"A1": [[_work("W1", "Paper One"), _work("W2", "Paper Two")]]},
    )

    result = _run(storage, client)

    assert result["coverage"] == {
        "considered": 1,
        "resolved": 1,
        "unresolved": 0,
        "failed": 0,
        "skipped_auth_failure": 0,
        "resolved_fraction": 1.0,
    }
    assert result["full_snapshots"] == 1
    assert result["works_fetched"] == result["works_new"] == 2
    assert result["works_changed"] == 0
    assert result["links_added"] == 2
    assert result["vector_jobs"] == 3
    assert client.work_calls[0]["since_updated_date"] is None
    assert storage.author_links["pi_1"]["match_method"] == "canonical_orcid_exact"
    assert [work["title"] for work in storage.upserted_raw_works] == ["Paper One", "Paper Two"]
    assert all("OFFICIAL TITLE" not in work["title"] for work in storage.upserted_raw_works)


def test_canonical_orcid_singleton_avoids_ambiguous_name_search_results():
    person = Person("pi_1", "Yang Liu")
    correct = _author("A1", "Yang Liu")
    storage = FakeStorage([person])
    client = FakeClient(
        authors={
            person.display_name: [
                correct,
                _author(
                    "A2",
                    "Yang Liu",
                    orcid="https://orcid.org/0000-0002-9999-9999",
                ),
            ]
        },
        work_batches={"A1": [[_work("W1", "Correct Work")]]},
    )

    result = _run(storage, client)

    assert result["people_resolved"] == 1
    assert storage.author_links["pi_1"]["openalex_author_id"].endswith("/A1")
    assert client.author_calls == []


def test_ambiguous_exact_name_and_ror_candidates_remain_unresolved():
    person = Person("pi_1", "Yuanwei Yao")
    storage = FakeStorage([person])
    client = FakeClient(
        authors={person.display_name: [_author("A1"), _author("A2", "Yao Yuanwei")]}
    )

    result = _run(storage, client)

    assert result["people_resolved"] == 0
    assert result["people_unresolved"] == 1
    assert result["unresolved_reasons"] == {"orcid_match_not_unique": 1}
    assert storage.author_links == {}
    assert client.work_calls == []


def test_premium_delta_uses_overlap_and_never_marks_unreturned_links_missing():
    person = Person("pi_1", "Yuanwei Yao")
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "https://openalex.org/A1",
        "link_status": "confirmed",
        "last_successful_sync_at": "2026-07-10T12:00:00Z",
        "last_full_sync_at": "2026-07-10T12:00:00Z",
    }
    storage.works["W1"] = {"hash": "old", "work": _work("W1", "Old title")}
    storage.works["W2"] = {"hash": "same", "work": _work("W2", "Existing")}
    storage.person_works["pi_1"] = {
        "W1": {"status": "active", "missing_streak": 0},
        "W2": {"status": "active", "missing_streak": 0},
    }
    client = FakeClient(
        work_batches={"A1": [[_work("W1", "Changed title"), _work("W3", "New paper")]]}
    )

    result = _run(
        storage,
        client,
        premium_updated_filter=True,
        updated_date_overlap=timedelta(days=1),
    )

    call = client.work_calls[0]
    assert call["since_updated_date"] == "2026-07-09T12:00:00Z"
    assert call["premium_updated_filter"] is True
    assert result["delta_snapshots"] == 1
    assert result["works_new"] == 1
    assert result["works_changed"] == 1
    assert result["links_missing"] == result["links_tombstoned"] == 0
    assert storage.person_works["pi_1"]["W2"] == {
        "status": "active",
        "missing_streak": 0,
    }


def test_two_consecutive_complete_snapshots_are_required_to_tombstone_a_link():
    person = Person("pi_1", "Yuanwei Yao")
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "https://openalex.org/A1",
        "link_status": "confirmed",
        "last_successful_sync_at": "2026-07-10T12:00:00Z",
    }
    storage.works["W1"] = {"hash": FakeStorage._hash(_work("W1", "Kept")), "work": _work("W1", "Kept")}
    storage.works["W2"] = {"hash": FakeStorage._hash(_work("W2", "Gone")), "work": _work("W2", "Gone")}
    storage.person_works["pi_1"] = {
        "W1": {"status": "active", "missing_streak": 0},
        "W2": {"status": "active", "missing_streak": 0},
    }
    client = FakeClient(work_batches={"A1": [[_work("W1", "Kept")], [_work("W1", "Kept")]]})

    first = _run(storage, client, run_id="full_1", full=True)
    second = _run(storage, client, run_id="full_2", full=True)

    assert first["links_missing"] == 1
    assert first["links_tombstoned"] == 0
    assert second["links_missing"] == 0
    assert second["links_tombstoned"] == 1
    assert storage.person_works["pi_1"]["W2"] == {
        "status": "tombstoned",
        "missing_streak": 2,
    }


def test_faculty_filter_person_filter_limit_and_dry_run_are_non_mutating():
    people = [
        Person("pi_b", "Beta Person", department="Department of Medicine"),
        Person(
            "pi_a",
            "Alpha Person",
            department="Department of Computing",
            departments=["Faculty of Engineering"],
        ),
        Person("pi_c", "Gamma Person", institution_id="inst_other"),
    ]
    storage = FakeStorage(people)
    storage.author_links["pi_a"] = {
        "person_id": "pi_a",
        "institution_id": "inst_hku",
        "openalex_author_id": "https://openalex.org/A1",
        "link_status": "confirmed",
    }
    client = FakeClient(work_batches={"A1": [[_work("W1", "Pilot")]]})

    result = _run(
        storage,
        client,
        person_ids=["pi_a"],
        institution_id="inst_hku",
        department_patterns=["faculty OF engineer"],
        limit=1,
        dry_run=True,
    )

    assert result["people_considered"] == 1
    assert result["people"][0]["person_id"] == "pi_a"
    assert storage.works == {}
    assert storage.person_works == {}
    assert storage.started == storage.finished == []


def test_free_delta_sorts_by_updated_date_and_does_not_advance_absence_state():
    person = Person("pi_1", "Yuanwei Yao")
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A1",
        "link_status": "confirmed",
        "last_successful_sync_at": "2026-07-10T12:00:00Z",
        "last_full_sync_at": "2026-07-01T00:00:00Z",
    }
    storage.person_works["pi_1"] = {
        "W1": {"status": "active", "missing_streak": 0},
    }
    client = FakeClient(work_batches={"A1": [[_work("W2", "Recently updated")]]})

    result = _run(storage, client)

    call = client.work_calls[0]
    assert call["sort"] == "updated_date:desc"
    assert call["stop_before_updated_date"] == "2026-07-08T12:00:00Z"
    assert call["since_updated_date"] is None
    assert result["delta_snapshots"] == 1
    assert result["people"][0]["sync_mode"] == "free_updated_date_delta"
    assert result["links_missing"] == result["links_tombstoned"] == 0
    assert storage.person_works["pi_1"]["W1"]["missing_streak"] == 0


def test_full_snapshot_is_automatically_due_after_thirty_days():
    person = Person("pi_1", "Yuanwei Yao")
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A1",
        "link_status": "confirmed",
        "last_successful_sync_at": "2026-07-10T00:00:00Z",
        "last_full_sync_at": "2026-06-14T03:59:59Z",
    }
    client = FakeClient(work_batches={"A1": [[_work("W1", "Full inventory")]]})

    result = _run(storage, client)

    assert result["full_snapshots"] == 1
    assert result["delta_snapshots"] == 0
    assert client.work_calls[0]["sort"] is None
    assert client.work_calls[0]["stop_before_updated_date"] is None


def test_name_ror_candidate_requires_official_publication_overlap_without_orcid():
    person = Person("pi_1", "Yuanwei Yao", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": "Paper One", "doi": None}]
    client = FakeClient(
        authors={person.display_name: [_author(orcid=None)]},
        work_batches={"A1": [[_work("W1", "Paper One")]]},
    )

    result = _run(storage, client)

    assert result["people_resolved"] == 1
    assert storage.author_links["pi_1"]["match_method"] == (
        "exact_name_ror_and_official_publication_overlap"
    )
    evidence = storage.author_links["pi_1"]["evidence"]["identity_evidence"]
    assert evidence["kind"] == "normalized_title"


def test_official_publication_overlap_disambiguates_exact_name_ror_candidates():
    person = Person("pi_1", "Alan P. Kwan", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": "Matching Paper", "doi": None}]
    client = FakeClient(
        authors={
            person.display_name: [
                _author("A1", "Alan P. Kwan", orcid=None),
                _author("A2", "Kwan Alan P.", orcid=None),
            ]
        },
        work_batches={
            "A1": [[_work("W1", "Different Paper")]],
            "A2": [[_work("W2", "Matching Paper")]],
        },
    )

    result = _run(storage, client)

    assert result["people_resolved"] == 1
    assert storage.author_links["pi_1"]["openalex_author_id"].endswith("/A2")
    assert [call["author_id"] for call in client.work_calls] == ["A2"]
    assert len(client.work_search_calls) == 1


def test_alias_is_searched_and_can_confirm_identity():
    person = Person("pi_1", "Matthias Nikolaus", external_ids={})
    person.aliases = ["Matthias FAHN"]
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": "Matching Paper", "doi": None}]
    client = FakeClient(
        authors={"Matthias FAHN": [_author("A1", "Matthias Fahn", orcid=None)]},
        work_batches={"A1": [[_work("W1", "Matching Paper")]]},
    )

    result = _run(storage, client)

    assert result["people_resolved"] == 1
    assert [call[0] for call in client.author_calls] == [
        "Matthias Nikolaus",
        "Matthias FAHN",
    ]


def test_name_ror_top_ten_match_without_orcid_or_official_paper_is_manual_review():
    person = Person("pi_1", "Yuanwei Yao", external_ids={})
    storage = FakeStorage([person])
    client = FakeClient(authors={person.display_name: [_author(orcid=None)]})

    result = _run(storage, client)

    assert result["people_resolved"] == 0
    assert result["people"][0]["status"] == "manual_review"
    assert result["unresolved_reasons"] == {
        "no_orcid_or_official_publication_identity_evidence": 1
    }
    assert storage.author_links == {}
    assert client.work_calls == []


def test_existing_rejected_link_is_never_automatically_overwritten():
    person = Person("pi_1", "Yuanwei Yao")
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A_REJECTED",
        "link_status": "rejected",
    }
    client = FakeClient(authors={person.display_name: [_author()]})

    result = _run(storage, client)

    assert result["unresolved_reasons"] == {"existing_link_rejected": 1}
    assert storage.author_links["pi_1"]["openalex_author_id"] == "A_REJECTED"
    assert client.author_calls == client.work_calls == []


def test_existing_confirmed_link_must_match_the_pi_institution_before_fetch():
    person = Person("pi_1", "Yuanwei Yao")
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_other",
        "openalex_author_id": "A1",
        "link_status": "confirmed",
    }
    client = FakeClient(work_batches={"A1": [[_work("W1", "Must not fetch")]]})

    result = _run(storage, client)

    assert result["people_failed"] == 1
    assert "another institution" in result["errors"][0]["error"]
    assert client.work_calls == []


def test_incomplete_or_duplicate_full_snapshot_fails_closed_before_reconcile():
    person = Person("pi_1", "Yuanwei Yao")
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A1",
        "link_status": "confirmed",
        "last_successful_sync_at": "2026-07-01T00:00:00Z",
        "last_full_sync_at": "2026-07-01T00:00:00Z",
    }
    storage.person_works["pi_1"] = {
        "W1": {"status": "active", "missing_streak": 0},
    }
    duplicate = _audited(
        [_work("W1", "Same"), _work("W1", "Same")],
        meta_count=2,
    )
    client = FakeClient(work_batches={"A1": [duplicate]})

    result = _run(storage, client, full=True)

    assert result["status"] == "failed"
    assert result["full_snapshots_rejected"] == 1
    assert "does_not_equal_unique" in result["people"][0]["error"]
    assert storage.person_works["pi_1"]["W1"] == {
        "status": "active",
        "missing_streak": 0,
    }


def test_empty_or_abruptly_smaller_existing_full_inventory_fails_closed():
    person = Person("pi_1", "Yuanwei Yao")
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A1",
        "link_status": "confirmed",
    }
    storage.person_works["pi_1"] = {
        f"W{index}": {"status": "active", "missing_streak": 0}
        for index in range(1, 11)
    }
    client = FakeClient(work_batches={"A1": [[_work("W1", "Only one")]]})

    result = _run(storage, client, full=True)

    assert result["full_snapshots_rejected"] == 1
    assert "suspicious_inventory_drop" in result["people"][0]["error"]
    assert all(link["missing_streak"] == 0 for link in storage.person_works["pi_1"].values())


def test_403_authentication_failure_stops_before_the_next_faculty_member():
    people = [Person("pi_1", "First Person"), Person("pi_2", "Second Person")]
    storage = FakeStorage(people)
    for index, person in enumerate(people, start=1):
        storage.author_links[person.person_id] = {
            "person_id": person.person_id,
            "institution_id": "inst_hku",
            "openalex_author_id": f"A{index}",
            "link_status": "confirmed",
            "last_successful_sync_at": "2026-07-10T00:00:00Z",
            "last_full_sync_at": "2026-07-10T00:00:00Z",
        }
    client = FakeClient(
        work_batches={
            "A1": [OpenAlexHTTPError(403, "https://api.openalex.org/works", "forbidden")],
            "A2": [[_work("W2", "Must not request")]],
        }
    )

    result = _run(
        storage,
        client,
        person_ids=None,
        department_patterns=["Faculty of Engineering"],
    )

    assert result["status"] == "failed"
    assert result["authentication_failure"]["status_code"] == 403
    assert result["people_skipped_auth_failure"] == 1
    assert len(client.work_calls) == 1


def test_distinct_official_overlaps_confirm_split_profiles_and_union_works():
    person = Person("pi_1", "Split Scholar", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [
        {"title": "Paper One", "doi": None},
        {"title": "Paper Two", "doi": None},
        {"title": "Paper Three", "doi": None},
    ]
    client = FakeClient(
        authors={
            person.display_name: [
                _author("A1", person.display_name, orcid=None, works_count=50),
                _author("A2", person.display_name, orcid=None, works_count=500),
            ]
        },
        work_batches={
            "A1": [[
                _work("W1", "Paper One"),
                _work("W3", "Paper Three"),
                _work("W9", "Shared Work"),
            ]],
            "A2": [[_work("W2", "Paper Two"), _work("W9", "Shared Work")]],
        },
    )

    result = _run(storage, client)

    link = storage.author_links["pi_1"]
    assert link["openalex_author_id"].endswith("/A1")
    assert link["match_method"] == (
        "split_openalex_profiles_with_distinct_official_publication_overlaps"
    )
    assert link["evidence"]["confirmed_openalex_author_ids"] == ["A1", "A2"]
    assert link["evidence"]["split_profile"] is True
    assert [call["author_id"] for call in client.work_calls] == ["A1", "A2"]
    assert result["full_snapshots"] == 2
    assert result["works_fetched"] == 5
    assert result["people"][0]["works_selected"] == 4
    assert set(storage.works) == {"W1", "W2", "W3", "W9"}
    assert storage.person_work_author_ids["pi_1"] == {
        "W1": "A1",
        "W2": "A2",
        "W3": "A1",
        "W9": "A1",
    }


def test_split_profiles_require_distinct_official_evidence():
    person = Person("pi_1", "Shared Scholar", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": "One Shared Paper", "doi": None}]
    shared = _work("W1", "One Shared Paper")
    client = FakeClient(
        authors={
            person.display_name: [
                _author("A1", person.display_name, orcid=None),
                _author("A2", person.display_name, orcid=None),
            ]
        },
        work_batches={"A1": [[shared]], "A2": [[shared]]},
    )

    result = _run(storage, client)

    assert result["unresolved_reasons"] == {
        "official_publication_evidence_not_distinct": 1
    }
    assert storage.author_links == {}
    assert client.work_calls == []


def test_persisted_split_profiles_are_all_fetched_on_future_delta():
    person = Person("pi_1", "Split Scholar", external_ids={})
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A1",
        "link_status": "confirmed",
        "confidence": 0.98,
        "match_method": "split",
        "last_successful_sync_at": "2026-07-10T12:00:00Z",
        "last_full_sync_at": "2026-07-10T12:00:00Z",
        "evidence": {
            "primary_openalex_author_id": "A1",
            "confirmed_openalex_author_ids": ["A1", "A2"],
            "confirmed_profiles": [
                {"openalex_author_id": "A1", "works_count": 10},
                {"openalex_author_id": "A2", "works_count": 20},
            ],
        },
    }
    client = FakeClient(
        work_batches={
            "A1": [[_work("W1", "Delta One")]],
            "A2": [[_work("W2", "Delta Two")]],
        }
    )

    result = _run(storage, client)

    assert [call["author_id"] for call in client.work_calls] == ["A1", "A2"]
    assert all(call["sort"] == "updated_date:desc" for call in client.work_calls)
    assert result["delta_snapshots"] == 2
    assert set(storage.person_works["pi_1"]) == {"W1", "W2"}


def test_each_split_full_cursor_is_audited_before_union_and_reconcile():
    person = Person("pi_1", "Split Scholar", external_ids={})
    storage = FakeStorage([person])
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A1",
        "link_status": "confirmed",
        "evidence": {
            "primary_openalex_author_id": "A1",
            "confirmed_openalex_author_ids": ["A1", "A2"],
            "confirmed_profiles": [
                {"openalex_author_id": "A1", "works_count": 10},
                {"openalex_author_id": "A2", "works_count": 10},
            ],
        },
    }
    duplicate = _audited(
        [_work("W2", "Duplicate"), _work("W2", "Duplicate")],
        meta_count=2,
    )
    client = FakeClient(
        work_batches={"A1": [[_work("W1", "Good")]], "A2": [duplicate]}
    )

    result = _run(storage, client, full=True)

    assert result["status"] == "failed"
    assert result["full_snapshots_rejected"] == 1
    assert "A2: meta_count_2_does_not_equal_unique_1" in result["people"][0]["error"]
    assert storage.person_works == {}


def test_candidate_work_count_guard_runs_before_full_cursor_fetch():
    person = Person("pi_1", "Large Scholar", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": "Matching Paper", "doi": None}]
    client = FakeClient(
        authors={
            person.display_name: [
                _author("A1", person.display_name, orcid=None, works_count=2001)
            ]
        },
        work_batches={"A1": [[_work("W1", "Matching Paper")]]},
    )

    result = _run(storage, client)

    assert result["unresolved_reasons"] == {"candidate_work_count_exceeds_limit": 1}
    assert storage.author_links == {}
    assert client.work_calls == []


@pytest.mark.parametrize(
    ("official_name", "openalex_name"),
    [
        ("Michael C.L. CHAU", "Michael Chau"),
        ("Uta SCHӦNBERG", "Uta Schoenberg"),
        ("Mengzhou (Austin) ZHUANG", "Mengzhou Zhuang"),
    ],
)
def test_exact_official_work_allows_one_compatible_missing_ror_authorship_fallback(
    official_name, openalex_name
):
    person = Person("pi_1", official_name, external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": "Matching Paper", "doi": None}]
    client = FakeClient(
        authors={
            official_name: [
                _author("A1", openalex_name, ror=None, orcid=None, works_count=12)
            ]
        },
        work_batches={"A1": [[_work("W1", "Matching Paper")]]},
    )

    result = _run(storage, client)

    assert result["people_resolved"] == 1
    profile = storage.author_links["pi_1"]["evidence"]["confirmed_profiles"][0]
    assert profile["ror_validation"] == "missing_ror_exception"


@pytest.mark.parametrize(
    ("official_name", "openalex_name"),
    [
        ("Will Peichun WANG", "Peichun Wang"),
        ("Steven Alan BARNETT", "Steven Barnett"),
        ("Lionel Zhepeng LI", "Zixuan Li"),
    ],
)
def test_authorship_fallback_requires_every_substantive_canonical_given_token(
    official_name, openalex_name
):
    person = Person("pi_1", official_name, external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": "Matching Paper", "doi": None}]
    client = FakeClient(
        authors={
            official_name: [
                _author("A1", openalex_name, ror=None, orcid=None, works_count=12)
            ]
        },
        work_batches={"A1": [[_work("W1", "Matching Paper")]]},
    )

    result = _run(storage, client)

    assert result["people_resolved"] == 0
    assert result["unresolved_reasons"] == {
        "official_publication_did_not_match_candidate_works": 1
    }


def test_fallback_rejects_compatible_name_with_conflicting_non_hku_ror():
    person = Person("pi_1", "Steven Alan BARNETT", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": "Matching Paper", "doi": None}]
    client = FakeClient(
        authors={
            person.display_name: [
                _author(
                    "A1",
                    "Steven Barnett",
                    ror="https://ror.org/03yrm5c26",
                    orcid=None,
                )
            ]
        },
        work_batches={"A1": [[_work("W1", "Matching Paper")]]},
    )

    result = _run(storage, client)

    assert result["people_resolved"] == 0
    assert storage.author_links == {}


def test_identity_probe_cache_includes_explicit_misses_across_people():
    people = [
        Person("pi_1", "First Scholar", external_ids={}),
        Person("pi_2", "Second Scholar", external_ids={}),
    ]
    storage = FakeStorage(people)
    for person in people:
        storage.official_evidence[person.person_id] = [
            {"title": "Missing Official Work", "doi": None}
        ]
    client = FakeClient(
        authors={
            person.display_name: [_author(f"A{index}", person.display_name, orcid=None)]
            for index, person in enumerate(people, start=1)
        }
    )

    result = _run(
        storage,
        client,
        person_ids=None,
        department_patterns=["Faculty of Engineering"],
    )

    assert result["people_unresolved"] == 2
    assert result["identity_work_probes"] == 1
    assert result["identity_work_cache_hits"] == 1
    assert len(client.work_search_calls) == 1


def test_identity_probe_cache_persists_explicit_miss_across_runs():
    person = Person("pi_1", "Cached Scholar", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [
        {"title": "Still Missing Official Work", "doi": None}
    ]
    first_client = FakeClient(
        authors={person.display_name: [_author("A1", person.display_name, orcid=None)]}
    )

    first = _run(storage, first_client, run_id="first_probe")

    assert first["identity_work_probes"] == 1
    assert first["identity_work_persistent_cache_writes"] == 1
    assert len(first_client.work_search_calls) == 1

    second_client = FakeClient(
        authors={person.display_name: [_author("A1", person.display_name, orcid=None)]}
    )
    second = _run(storage, second_client, run_id="second_probe")

    assert second["identity_work_probes"] == 0
    assert second["identity_work_persistent_cache_hits"] == 1
    assert second_client.work_search_calls == []


def test_identity_probe_dry_run_does_not_write_persistent_cache():
    person = Person("pi_1", "Dry Run Scholar", external_ids={})
    storage = FakeStorage([person])
    storage.official_evidence["pi_1"] = [{"title": "Missing", "doi": None}]
    client = FakeClient(
        authors={person.display_name: [_author("A1", person.display_name, orcid=None)]}
    )

    result = _run(storage, client, dry_run=True)

    assert result["identity_work_probes"] == 1
    assert result["identity_work_persistent_cache_writes"] == 0
    assert storage.identity_probe_cache == {}


def test_successful_persisted_sync_completes_only_that_persons_source_jobs():
    person = Person("pi_1", "Yuanwei Yao")
    storage = FakeStorage([person])
    storage.pending_openalex_sync_jobs.add("pi_1")
    storage.author_links["pi_1"] = {
        "person_id": "pi_1",
        "institution_id": "inst_hku",
        "openalex_author_id": "A1",
        "link_status": "confirmed",
    }
    client = FakeClient(work_batches={"A1": [[_work("W1", "Complete")]]})

    result = _run(storage, client)

    assert result["source_sync_jobs_completed"] == 1
    assert result["people"][0]["source_sync_jobs_completed"] == 1
    assert storage.pending_openalex_sync_jobs == set()


@pytest.mark.parametrize(
    ("institution_id", "person_ids", "departments", "message"),
    [
        (None, ["pi_1"], None, "institution_id is required"),
        ("inst_hku", None, None, "department_patterns or non-empty person_ids"),
        ("inst_hku", [], [], "department_patterns or non-empty person_ids"),
        ("inst_hku", ["missing"], None, "not all matched"),
        ("inst_hku", None, ["Faculty of Dentistry"], "matched zero"),
    ],
)
def test_selection_scope_fails_closed(
    institution_id, person_ids, departments, message
):
    storage = FakeStorage([Person("pi_1", "Yuanwei Yao")])

    with pytest.raises(ValueError, match=message):
        sync_openalex_publications(
            storage,
            client=FakeClient(),
            institution_id=institution_id,
            person_ids=person_ids,
            department_patterns=departments,
            now=lambda: NOW,
        )
