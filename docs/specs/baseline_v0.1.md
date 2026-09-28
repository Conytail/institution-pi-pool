# Baseline v0.1

This baseline freezes the reusable institution-ingestion contract before expanding the
number of university adapters.

## Implemented

- Config-driven official-site crawling and parser selection.
- Institution-constrained PI records and evidence storage in SQLite.
- Independent institution-fit, research-fit and contact-evidence fields.
- Paper backtrace as a research-fit feature rather than an institution override.
- Adapter Config v1 validation at the ingestion boundary.
- Canonical PI Record v1 with an explicit schema version.
- Immutable institution snapshot writer, quality report and pass-only serving promotion.
- Golden parser suites for all three template families, covering every tracked institution config.

## Experimentally Validated, Not Yet Serving

- Equal-weight 256-feature career vectors.
- Career Top-10 recall followed by publication Top-3 reranking.
- `max(career_score, paper_top3_score)` as the research-fit evidence combination.
- Global publication vectors keyed by OpenAlex Work ID.

The frozen candidate policy is `configs/matching/matching_v1.yaml` and deliberately has
`implementation_status: validated_candidate_not_serving`.

## Not Implemented

- Persistent `career_sum`, `career_vector_256`, yearly buckets and PI-to-Work tables.
- Scheduled incremental OpenAlex publication synchronization.
- Public API or MCP server.
- Human-labelled applicant-to-researcher recommendation validation.

This section describes the frozen v0.1 tag. The current expansion work now persists
confirmed PI-to-Work relationships and runs scheduled full/delta OpenAlex maintenance as
specified in `incremental_capture_v1.md`. Current code also persists global paper vectors
and PI career vectors through a leased local sparse-vector worker; serving-time matcher
integration remains outside the frozen v0.1 baseline.

## Change Control

Changes to PI fields, matching formulas, score semantics or adapter configuration require
a schema or policy version increment. Institution YAML may describe website differences,
but must not contain applicant-specific keywords, PI-specific ranking exceptions or
institution-specific matching weights.
