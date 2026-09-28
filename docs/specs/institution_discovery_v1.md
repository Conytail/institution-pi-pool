# Institution Discovery v1

Discovery is the versioned boundary between regional institution selection and executable
Adapter Configs. It records what must be covered without claiming that a crawler already
works.

## Artifacts

- A regional `institution_registry.v1.yaml` fixes the institution cohort, stable identities,
  whole-institution pool scope and coverage claim.
- One `*.discovery.v1.yaml` per institution records official organisational, people/research
  profile and research-degree sources.
- `schemas/institution_registry.v1.schema.json` and
  `schemas/institution_discovery_manifest.v1.schema.json` freeze both contracts.

The Hong Kong pilot is under `data/institutions/hong_kong/`. Its registry includes all eight
UGC-funded universities. This proves UGC-sector selection coverage only. It deliberately keeps
`region_complete: false` until the remaining degree-awarding institutions receive a documented
research-degree eligibility audit.

## Scope Rules

Discovery inventories top-level and cross-faculty academic units from official sources. Child
departments are expanded from those sources during Adapter onboarding instead of being copied
into a second stale hierarchy.

The population is `academic_and_research_personnel`. Institution membership and academic
employment are collected without a title-based eligibility gate. Research
centres are secondary affiliations unless they contain current academic or research staff absent
from the institution-wide person source.

## Status Semantics

- `discovery_status: complete` means official scope, person and research-degree entry points are
  identified and access constraints are recorded.
- `adapter_readiness: ready` means the primary person source was reachable by the current HTTP
  client during discovery.
- `adapter_readiness: conditional` means an anti-bot or transport constraint must be resolved.
- `adapter_config_status: not_started` prevents a Discovery Manifest from being served or crawled
  as though it were a validated Adapter Config.

Each institution now proceeds independently through Adapter Config v2, deterministic fixtures,
full crawl, quality gates and snapshot promotion.
