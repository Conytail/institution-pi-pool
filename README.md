# PI Index MVP

Institution-level ingestion and matching for public PI/faculty information from official university sources and no-key academic APIs.

The MVP is intentionally conservative:

- It runs without paid SERP API keys.
- It uses configured official seed URLs, official homepages, robots.txt, sitemap.xml, and common official paths by default.
- ROR, OpenAlex, Crossref, and ORCID clients are no-key modules. `OPENALEX_MAILTO` and `CROSSREF_MAILTO` are used when present, but are optional.
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
python -m pi_index.cli ingest-institution --config configs/institutions/university_of_british_columbia_cs.yaml --db outputs/sample_run/pi_index.db --snapshot-root snapshots
```

Run a small configured batch:

```powershell
python -m pi_index.cli ingest-batch --institution-list data/institutions/qs_top500_sample.csv --limit 3 --db outputs/sample_run/pi_index.db --snapshot-root snapshots
```

Create a new immutable snapshot from an existing database without crawling again:

```powershell
python -m pi_index.cli snapshot-institution --config configs/institutions/sunway_university_computing_ai.yaml --db outputs/sunway_all/pi_index.db --snapshot-root snapshots
```

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
quality gates. See `docs/specs/adapter_config_v1.md` and
`docs/specs/institution_onboarding.md`.

The sample CSV in `data/institutions/qs_top500_sample.csv` is a small registry seed, not a current authoritative QS Top 500 ranking. To scale to QS Top 500, provide a legally obtained QS CSV and run `ingest-batch` with configs added for institutions or source templates.

## Optional Environment Variables

```text
SERPER_API_KEY
BRAVE_SEARCH_API_KEY
BING_SEARCH_API_KEY
JINA_API_KEY
OPENALEX_MAILTO
CROSSREF_MAILTO
PI_INDEX_SEARCH_PLUGIN_MODULES
```

`PI_INDEX_SEARCH_PLUGIN_MODULES` may contain comma-separated Python modules that expose `build_provider()` and return a discovery provider. Paid SERP keys are not read by the core crawler except to report availability for optional plugins. If no plugin is installed, discovery remains no-key.

## Contracts

Baseline v0.1 freezes three versioned contracts:

- `schemas/pi_record.v1.schema.json`
- `schemas/institution_config.v1.schema.json`
- `configs/matching/matching_v1.yaml`

The matching policy is a validated candidate and is not yet the serving implementation.
See `docs/specs/baseline_v0.1.md` for the exact implemented boundary.

## Runtime Data

Production pool snapshots are written to `snapshots/{institution_id}/{run_id}/`. Each run
has a manifest, checksums, quality report, source evidence and changes from the prior run.
`latest.json` points to the latest attempt; `current.json` advances only for a passing run.

Ad hoc exports and evaluation results remain under `outputs/`; they are not production
snapshots and neither directory is committed to Git.

Exports are written to the requested output directory:

Exports are written to the requested output directory:

- `institutions.csv`
- `pi_records.jsonl`
- `contact_verdicts.csv`
- `high_confidence_contactable.csv`
- `stale_risk.csv`
- `failures.csv`
- `evidence.jsonl`
- `match_results.csv`
- `audit_sample.csv`
- `institution_quality_report.csv`
- `duplicates.csv`
- `supervisor_candidates.csv`

## Known MVP Limits

- Generic parsers favor precision over recall and will miss pages with heavily client-rendered directories.
- OpenAlex, Crossref, and ORCID clients are implemented as no-key modules but enrichment is conservative and optional.
- No paid SERP provider is bundled. Discovery failures are logged instead of fabricating URLs.
- QS Top 500 coverage is not claimed until a current licensed/exported QS CSV and institution configs are supplied and run.

## Scaling Path

1. Add or import a current QS CSV with institution names, countries, homepages, and optional ROR IDs.
2. Normalize institutions with ROR and group them by CMS/directory template.
3. Reuse a `template_family`, add deterministic fixtures and pass its quality gates before writing institution-specific code.
4. Run `ingest-batch` in small batches, inspect snapshot quality and uncertain verdicts, then expand.
5. Add optional OpenAlex enrichment and vector search where local infrastructure is available.
