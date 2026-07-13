# Institution Onboarding

Use this workflow for every institution added after baseline v0.1.

1. Register the institution with a stable ROR ID or official homepage and define the exact
   faculty, school, department or multi-unit pool scope.
2. Assign an existing `template_family` and create an Adapter Config v1 file. Configs may
   describe website structure but may not contain applicant or PI ranking exceptions.
3. Capture small deterministic HTML fixtures for each distinct page shape and commit a
   golden canonical output. Do not commit bulk crawls.
4. Run ingestion into a local SQLite working database and produce an immutable snapshot.
5. Promote the institution only when minimum people, duplicate rate and profile URL
   coverage gates pass and the golden fixture remains unchanged.
6. Add or change parser code only when an existing template family cannot pass those
   checks. Prefer a new reusable template family over an institution-only adapter.

## Ownership Boundaries

| Content | Location | Versioned in Git |
|---|---|---|
| Institution-specific structure and crawl policy | `configs/institutions/` | Yes |
| Reusable website parsing behavior | `src/pi_index/adapters/`, `src/pi_index/parsers/` | Yes |
| Small source examples and expected records | `tests/fixtures/`, `tests/golden/` | Yes |
| Serving pool snapshots | `snapshots/` | No |
| Experiments, exports and local databases | `outputs/` | No |

An institution is expansion-ready only when its config validates, deterministic fixtures
pass, a snapshot is generated, and the snapshot quality status is `pass`.
