# Incremental Capture v1

## Storage Layers

The working database separates current serving facts from append-only observations:

| Layer | Purpose |
|---|---|
| `canonical_pi_records` | Current PI identity, affiliation state and contact fields |
| `pi_observations` | Parsed PI payload for each crawl run and source URL |
| `person_evidence` | Field-level source evidence linked to a crawl run |
| `official_publication_fingerprints` | Official title/year/DOI/link fingerprints with first/last seen metadata |
| `official_publication_source_claims` | Per-profile claim lifecycle; disappearance is source-local, not a paper retraction |
| `official_publication_refresh_state` | HTTP hash plus parser/config key for parse invalidation and due checks |
| `publication_refresh_runs` | Auditable official-publication maintenance runs and metrics |
| `openalex_author_links` | Evidence-backed PI-to-OpenAlex Author links and sync watermarks |
| `openalex_works` | Deduplicated normalized OpenAlex Work metadata and vector input text |
| `openalex_person_works` | Per-PI Work lifecycle; delta absence never means removal |
| `openalex_sync_runs` | Auditable full/delta OpenAlex runs and completeness metrics |
| `vector_dirty_queue` | Idempotent downstream OpenAlex/vector work for affected entities only |
| `pi_identity_aliases` | Stable mapping from changed source-derived IDs to canonical PI IDs |
| `raw_sources` | HTTP metadata and references to archived response bytes |
| `ingestion_runs` | Config hash, completeness metrics, status and run timestamps |

Raw response bytes are not stored in SQLite. `ContentArchive` writes deterministic gzip
blobs to `blobs/sha256/{prefix}/{sha256}.gz`; identical responses share one blob.

## Refresh Behavior

1. Load the latest archived response metadata for the institution and URL.
2. Send `If-None-Match` and/or `If-Modified-Since` when validators exist.
3. On `200`, archive the new bytes. Publication maintenance parses only when the body
   hash or `(parser name, parser version, publication config hash)` changed.
4. On `304`, reuse the archived bytes. A valid publication baseline skips parsing;
   a parser/config change reparses the archived body without a network download.
5. In `--offline` mode, replay archived bytes and fail closed with `offline_cache_miss`.

Every attempt receives one `run_id`. Raw sources, evidence, observations, duplicates,
parse metrics, contact verdicts and crawl errors are linked to that run.

## Completeness And Lifecycle

A crawl is complete only when all configured thresholds pass: successful pages, seed URL
coverage, named unit coverage, profile fetch coverage, profile parse coverage, pagination
completion and minimum PI count.

Incomplete crawls never mark a PI missing. After a configurable number of consecutive
complete crawls where a PI is absent, the current record changes from `active` to
`missing` and then `inactive`. Reappearance restores `active` while preserving
`first_seen_at`.

`latest.json` records every snapshot attempt. `current.json` advances only when all
quality checks pass. Inactive records remain auditable but are excluded from matching and
current service exports.

## Publication Maintenance

First create an isolated one-Faculty database. The source pool is opened read-only, and
only referenced archive blobs are copied and hash-verified:

```powershell
python scripts/build_faculty_pilot_db.py `
  --source-db outputs/hong_kong_ugc_fixed_20260714/pi_index.db `
  --out-db outputs/pilots/hku_business_school/pi_index.db `
  --institution-id inst_77b83f05042f0881 `
  --department-contains "HKU Business School"
```

Official profile maintenance is independent of full institution ingestion and never
changes PI membership or contact evidence. A Faculty/department or explicit person
allowlist is mandatory, so an omitted/empty selector cannot expand to the whole pool:

```powershell
python -m pi_index.cli refresh-official-publications `
  --config configs/institutions/hku.yaml `
  --db outputs/pilots/hku_business_school/pi_index.db `
  --department "HKU Business School" `
  --workers 4 --due-only --missing-confirmations 2
```

HTTP requests run with four workers and a shared per-host start cadence. `--due-only`
uses the last successful refresh, retries failed/quarantined sources after a short
backoff, and bypasses both timers when the parser or config key changes. Fingerprints,
source claims, state, raw metadata, and queue invalidation commit atomically per source.

A complete, identity-unique changed snapshot may move a missing source claim to
`no_longer_observed`; a second complete changed snapshot soft-tombstones that claim.
`304`, same-hash responses, parser errors, ambiguous identities, incomplete inventories,
WAF pages, and suspicious inventory collapses never advance missing state. A publication
remains current while any trusted source still claims it.

Official titles are not embedding input. Claim changes enqueue `openalex_works_sync` for
the affected PI. Confirmed OpenAlex Work records then determine which paper vectors and
which PI career vectors need rebuilding.

## OpenAlex Work Maintenance

The current OpenAlex API requires `OPENALEX_API_KEY`. Synchronization also requires an
institution plus a Faculty/department or explicit PI allowlist:

```powershell
$env:OPENALEX_API_KEY = "..."
python -m pi_index.cli sync-openalex-publications `
  --db outputs/pilots/hku_business_school/pi_index.db `
  --institution-id inst_77b83f05042f0881 `
  --department "HKU Business School"
```

An author is auto-confirmed only by an exact ORCID match, or by one unique exact
name+ROR candidate whose OpenAlex Works overlap an official DOI or normalized title.
Official titles are identity evidence only; they never become synthetic OpenAlex Works
or embedding text. Rejected links are not automatically revived.

Explicitly reviewed exceptions use `--reviewed-identity-manifest`. The JSON manifest
binds an exact current `person_id`, institution, display name, reason, review timestamp,
and one or more exact OpenAlex Author IDs. Its SHA-256 and decision are persisted in the
author-link evidence. Existing reviewed links conflict closed; a reviewed manifest may
replace an older automatic link. `sync_mode: full_profile` remains the default. A
review-only `field_allowlist` policy may select fields from a completely fetched and
cursor-audited profile, while recording raw, authorship-compatible, selected, rejected,
and policy-hash metrics. `sync_mode: official_evidence_only` is the last-resort path for
explicit Work IDs/DOIs backed by official fingerprints. It may omit an Author ID, stores
a nullable identity-pending PI-to-Work relationship, builds paper/career vectors, and
does not complete the pending identity-refresh job.

Every profile Work is admitted only when the profile Author ID occurs in the Work's
authorships and the raw/display authorship name strongly matches the canonical surname
and every substantive given-name token (full token or initial). The complete cursor is
audited before this filter. `--revalidate-identities` reruns bounded identity proof for
non-reviewed confirmed links; unresolved prior links become `stale`. A reviewed pilot
cleanup may combine it with `--full --missing-confirmations 1` to tombstone excluded old
relationships immediately.

Exact official-Work identity probes are cached persistently by versioned DOI/title key.
Both raw hits and explicit misses retain `fetched_at`/expiry metadata; dry runs never
write this cache.

The first sync, an explicit `--full`, and a run at least 30 days after the last full
snapshot use complete cursor pagination. Total count, terminal cursor, unique Work count,
empty inventory, and suspicious drops are checked before absence can advance. Between
full snapshots, free accounts scan `updated_date:desc` only to the previous successful
watermark minus a two-day overlap. Premium accounts may explicitly use
`--premium-updated-filter`. Delta absence never advances missing/tombstone state.

New or vector-text-changed Works enqueue `paper_vector_256`; changed PI Work membership
or a linked paper dependency enqueues `career_vector_256`. The queue has atomic leases
and crash recovery. `build-research-vectors` drains only those two job kinds. It persists
the frozen `production_terms_v1` representation as at most 256 sparse, L2-normalized
features: paper vectors are global by OpenAlex Work ID and career vectors are equal-weight
centroids over current (`active` or provisional `missing`) PI-to-Work links. A queued job
must still not be reported as generated until the corresponding vector row exists.
