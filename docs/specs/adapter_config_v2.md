# Adapter Config v2

Institution configs describe official website structure and measurable crawl scope. They
must not contain applicant-specific terms, PI ranking exceptions or cross-institution
matching behavior.

Every v2 config declares:

- a stable institution identity anchor and official domains;
- the exact pool scope and optional named units with their official seed URLs;
- a reusable `template_family` and parser order;
- crawl limits, profile discovery rules and pagination rules;
- structural person-block inclusion and non-person exclusion rules;
- directory and profile refresh intervals;
- mandatory gzip archive and conditional-request behavior;
- quality thresholds for seed, unit, profile fetch, profile parse and pagination coverage.

`crawl.max_pages` must be at least the number of configured seed URLs. Every URL assigned
to `pool_scope.units` must also appear in `crawl.seed_urls`.

The executable validator is `pi_index.config.load_institution_config`; the formal schema
is `schemas/institution_config.v2.schema.json`.
