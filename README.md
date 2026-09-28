# PI Index MVP

Institution-level ingestion and matching for public PI/faculty information from official university sources and academic APIs.

The MVP is intentionally conservative:

- It runs without paid SERP API keys.
- It uses configured official seed URLs, official homepages, robots.txt, sitemap.xml, and common official paths by default.
- ROR, Crossref, and ORCID access can run without paid credentials. Current OpenAlex synchronization requires a free `OPENALEX_API_KEY`; the retired `mailto` polite-pool mechanism is not used for authentication.
- SERP providers are represented only as optional discovery plugins. They are never required, and snippets are never final evidence for contact verdicts.
- Contact verdicts require source evidence. Uncertain records are exported for audit instead of being silently dropped.

## Install

```powershell
python -m pip install -e ".[dev]"
```

## Run

Import the sample institution registry:

```powershell
python -m pi_index.cli import-institutions --input data/institutions/qs_top500_sample.csv --db outputs/sample_run/pi_index.db
```

Run one configured institution:

```powershell
python -m pi_index.cli ingest-institution --config configs/institutions/university_of_british_columbia_cs.yaml --db outputs/sample_run/pi_index.db --snapshot-root snapshots --archive-root data/raw_sources
```

Run a small configured batch:

```powershell
python -m pi_index.cli ingest-batch --institution-list data/institutions/qs_top500_sample.csv --limit 3 --db outputs/sample_run/pi_index.db --snapshot-root snapshots --archive-root data/raw_sources
```

The Hong Kong UGC whole-institution batch is registered at
`data/institutions/hong_kong/ugc_batch.v1.csv`. For long crawls on independent domains,
run each institution into an isolated database and merge completed shards idempotently:

```powershell
python -m pi_index.cli merge-shards --shards outputs/hong_kong_ugc/shards/*.db --db outputs/hong_kong_ugc/pi_index.db
```

Create a new immutable snapshot from an existing database without crawling again:

```powershell
python -m pi_index.cli snapshot-institution --config configs/institutions/sunway_university_computing_ai.yaml --db outputs/sunway_all/pi_index.db --snapshot-root snapshots
```

Reparse entirely from the archived response cache without network requests:

```powershell
python -m pi_index.cli ingest-institution --config configs/institutions/university_of_british_columbia_cs.yaml --db outputs/sample_run/pi_index.db --snapshot-root snapshots --archive-root data/raw_sources --offline
```

If a long run contains transient fetch failures, refill only those cache entries and then
run the offline replay above. The failed run must have finished before repair starts:

```powershell
python -m pi_index.cli repair-failed-cache --config configs/institutions/cuhk.yaml --failed-run-id RUN_ID --db outputs/hong_kong_ugc/shards/cuhk.db --archive-root outputs/hong_kong_ugc/raw_sources
```

Build and maintain an isolated one-Faculty publication pilot without rerunning an
institution or mutating the regional pool:

```powershell
python scripts/build_faculty_pilot_db.py `
  --source-db outputs/hong_kong_ugc_fixed_20260714/pi_index.db `
  --out-db outputs/pilots/hku_business_school/pi_index.db `
  --institution-id inst_77b83f05042f0881 `
  --department-contains "HKU Business School"

python -m pi_index.cli refresh-official-publications `
  --config configs/institutions/hku.yaml `
  --db outputs/pilots/hku_business_school/pi_index.db `
  --department "HKU Business School" --workers 4 --due-only
```

After setting the required free OpenAlex key, build the confirmed Work manifest for the
same scope. The first and periodic 30-day runs are complete snapshots; intervening
free-tier runs use an overlapping `updated_date` watermark scan:

```powershell
$env:OPENALEX_API_KEY = "..."
python -m pi_index.cli sync-openalex-publications `
  --db outputs/pilots/hku_business_school/pi_index.db `
  --institution-id inst_77b83f05042f0881 `
  --department "HKU Business School"

python -m pi_index.cli build-research-vectors `
  --db outputs/pilots/hku_business_school/pi_index.db
```

The two scoped refresh commands fail closed when their explicit Faculty/PI scope is missing
or matches nothing. OpenAlex changes enqueue paper/career vector work. The vector worker uses
the validated local `production_terms_v1` sparse encoder: one global top-256 vector per
OpenAlex Work ID and one equal-weight top-256 career centroid per PI. It makes no external
model or embedding API call.

Audit and export:

```powershell
python -m pi_index.cli audit --db outputs/sample_run/pi_index.db
python -m pi_index.cli audit-sample --db outputs/sample_run/pi_index.db --out outputs/sample_run/audit_sample.csv --sample-size 60
python -m pi_index.cli export --db outputs/sample_run/pi_index.db --out outputs/sample_run/
```

Match an applicant against the local index:

```powershell
python -m pi_index.cli match-applicant --applicant examples/applicant_profile.txt --db outputs/sample_run/pi_index.db --top-k 20
```

## Tests

```powershell
python -m pytest
```

## Configuration

Institution configs live in `configs/institutions/`. Baseline v0.1 includes five real
institution configurations across three reusable template families:

- `university_of_british_columbia_cs.yaml`
- `cornell_cs.yaml`
- `columbia_cs.yaml`
- `sunway_university_computing_ai.yaml`
- `xian_jiaotong_university_ai.yaml`

Each config declares a stable institution identity anchor, pool scope, reusable template
family, official sources, refresh policy, parser order, PI detection signals, and measurable
quality gates. See `docs/specs/adapter_config_v2.md` and
`docs/specs/institution_onboarding.md`.

Regional expansion begins before Adapter Config creation. The Hong Kong UGC pilot registry,
eight institution-level discovery manifests and runnable batch list live under
`data/institutions/hong_kong/`. All eight institutions have Adapter Config v2 files; an adapter is
considered serving-ready only when its latest immutable snapshot passes the configured quality
gates. See `docs/specs/institution_discovery_v1.md`.

The sample CSV in `data/institutions/qs_top500_sample.csv` is a small registry seed, not a current authoritative QS Top 500 ranking. To scale to QS Top 500, provide a legally obtained QS CSV and run `ingest-batch` with configs added for institutions or source templates.

## Optional Environment Variables

```text
SERPER_API_KEY
BRAVE_SEARCH_API_KEY
BING_SEARCH_API_KEY
JINA_API_KEY
OPENALEX_API_KEY
CROSSREF_MAILTO
PI_INDEX_SEARCH_PLUGIN_MODULES
```

`OPENALEX_API_KEY` is required for OpenAlex synchronization under the current API.
`OPENALEX_MAILTO` belongs to the retired polite-pool mechanism and is not used as an
authentication substitute. `PI_INDEX_SEARCH_PLUGIN_MODULES` may contain comma-separated
Python modules that expose `build_provider()` and return a discovery provider. Paid SERP
keys are not read by the core crawler except to report availability for optional plugins.
If no plugin is installed, discovery remains no-key.

## Contracts

Baseline v0.1 remains tagged in Git. The expansion-ready workflow uses:

- `schemas/pi_record.v2.schema.json`
- `schemas/institution_config.v2.schema.json`
- `schemas/institution_registry.v1.schema.json`
- `schemas/institution_discovery_manifest.v1.schema.json`
- `configs/matching/matching_v1.yaml`
- `docs/specs/incremental_capture_v1.md`

The matching policy is a validated candidate and is not yet the serving implementation.
See `docs/specs/baseline_v0.1.md` for the exact implemented boundary.

## Runtime Data

Production pool snapshots are written to `snapshots/{institution_id}/{run_id}/`. Each run
has a manifest, checksums, run metrics, source evidence, PI observations, official
publication fingerprints and changes from the prior run.
`latest.json` points to the latest attempt; `current.json` advances only for a passing run.

Raw HTML, JSON and PDF responses are stored once by SHA-256 under `data/raw_sources/` as
gzip blobs. SQLite stores only archive references and HTTP validators. Later crawls use
`ETag`/`Last-Modified`; a `304` reuses the archived body for parsing.

Ad hoc exports and evaluation results remain under `outputs/`; they are not production
snapshots and neither directory is committed to Git.

Exports are written to the requested output directory:

- `institutions.csv`
- `pi_records.jsonl`
- `inactive_pi_records.jsonl`
- `pi_identity_aliases.csv`
- `contact_verdicts.csv`
- `high_confidence_contactable.csv`
- `active_research_pool.csv`
- `research_evidence_ready.csv`
- `research_evidence_review_queue.csv`
- `contact_review_queue.csv`
- `stale_risk.csv`
- `failures.csv`
- `evidence.jsonl`
- `match_results.csv`
- `audit_sample.csv`
- `institution_quality_report.csv`
- `duplicates.csv`

## Known MVP Limits

- Generic parsers favor precision over recall and will miss pages with heavily client-rendered directories.
- OpenAlex enrichment is conservative and optional, but its current API requires `OPENALEX_API_KEY` and enforces account usage limits. Generated research vectors are deterministic sparse term vectors, not dense neural embeddings; non-English text without English title/topic metadata can therefore have weak or empty coverage.
- No paid SERP provider is bundled. Discovery failures are logged instead of fabricating URLs.
- QS Top 500 coverage is not claimed until a current licensed/exported QS CSV and institution configs are supplied and run.

## Scaling Path

1. Add or import a current QS CSV with institution names, countries, homepages, and optional ROR IDs.
2. Normalize institutions with ROR and group them by CMS/directory template.
3. Reuse a `template_family`, add deterministic fixtures and pass its quality gates before writing institution-specific code.
4. Run `ingest-batch` in small batches, inspect snapshot quality and uncertain verdicts, then expand.
5. Add optional OpenAlex enrichment and vector search where local infrastructure is available.
