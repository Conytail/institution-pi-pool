# Institution Onboarding

Use this workflow for every institution added after baseline v0.1.

1. Register the institution with a stable ROR ID or official homepage in a versioned regional
   registry. Define whether the coverage claim is sector-level or region-level.
2. Complete an Institution Discovery Manifest v1 from official organisational, person and
   research-degree sources. Record access constraints; do not treat discovery as a runnable adapter.
3. Assign an existing `template_family` and create an Adapter Config v2 file. Configs may
   describe website structure but may not contain applicant or PI ranking exceptions.
4. Expand the manifest's top-level units into official seed URLs so child-unit coverage is measurable.
5. Capture small deterministic HTML fixtures for each distinct page shape and commit a
   golden canonical output. Do not commit bulk crawls.
6. Run ingestion into a local SQLite working database, a content-addressed raw archive,
   and an immutable snapshot.
   Independent domains may run concurrently in isolated SQLite shards. Merge them with
   `python -m pi_index.cli merge-shards`; never use concurrent writers on one SQLite file.
   For transient failures in a completed long run, use `repair-failed-cache` for the failed
   URLs and then run the institution ingestion with `--offline` to rebuild a complete snapshot.
7. Promote the institution only when seed, unit, pagination, profile fetch/parse,
   minimum people, duplicate rate and profile URL coverage gates pass.
8. Add or change parser code only when an existing template family cannot pass those
   checks. Prefer a new reusable template family over an institution-only adapter.

## Ownership Boundaries

| Content | Location | Versioned in Git |
|---|---|---|
| Regional cohort and pre-adapter discovery | `data/institutions/{region}/` | Yes |
| Institution-specific structure and crawl policy | `configs/institutions/` | Yes |
| Reusable website parsing behavior | `src/pi_index/adapters/`, `src/pi_index/parsers/` | Yes |
| Small source examples and expected records | `tests/fixtures/`, `tests/golden/` | Yes |
| Serving pool snapshots | `snapshots/` | No |
| Compressed raw response archive | `data/raw_sources/` | No |
| Experiments, exports and local databases | `outputs/` | No |

An institution is expansion-ready only when its config validates, deterministic fixtures
pass, a snapshot is generated, and the snapshot quality status is `pass`.
