# Institution Pool Snapshots

Runtime snapshots are written outside version control under:

```text
snapshots/{institution_id}/{run_id}/
```

Each immutable run contains:

```text
manifest.json
pi_records.jsonl
contact_verdicts.jsonl
evidence.jsonl
raw_sources.jsonl
failures.jsonl
changes.jsonl
quality_report.json
```

`latest.json` points to the latest attempted run. `current.json` advances only when the
snapshot passes its configured quality gates, so a failed crawl cannot replace the
serving pool. Snapshot data is not source code and must not be committed. Small
deterministic fixtures and golden outputs belong under `tests/fixtures/` and
`tests/golden/`.

`raw_sources.jsonl` contains source metadata and content hashes, not copied page bodies.
The local working database may retain crawl history; each snapshot keeps only the latest
record for each source URL.
