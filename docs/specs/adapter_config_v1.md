# Adapter Config v1

Institution configs describe official website differences only. Every config must declare:

- institution identity, official domains and a stable `ror_id` or `homepage_url` anchor;
- the exact pool scope;
- a reusable `template_family`;
- official seed URLs and crawl limits;
- parser order and local title signals;
- refresh intervals and measurable quality gates.

The executable validator is `pi_index.config.load_institution_config`; the formal contract
is `schemas/institution_config.v1.schema.json`.

Matching weights, applicant terms, PI-specific exceptions and cross-institution fallbacks
are forbidden in institution configs. A custom parser should be added only after the
configured reusable template family fails its fixture and quality gates.
