# Research Profile Resolution Experiment

## Objective

Select the smallest PI Research Profile representation that preserves retrieval quality for applicant-like CV and proposal queries.

The experiment optimizes representation resolution, not institution membership, identity resolution, or supervisor eligibility. Those remain separate gates.

## Evaluation Lines

1. **Publication holdout benchmark (parameter selection)**
   - Start from official institution PI pools.
   - Resolve a high-confidence OpenAlex Author link using name plus official-institution ROR evidence.
   - Hold out recent works globally from every coauthor profile.
   - Construct proposal text from a held-out work and CV text from separate held-out works.
   - Retrieve only among PIs in the same official institution pool.
   - Treat every resolved in-pool author of the held-out work as relevant.

2. **Real applicant benchmark (external validation only)**
   - Use CV and proposal packets that were never used to tune representation parameters.
   - Obtain graded relevance labels from at least two reviewers.
   - Resolve disagreements before opening the final test results.

The publication benchmark is objective and scalable, but it does not replace human labels for final supervisor suitability.

## Leakage Controls

- Proposal and CV works are removed from every PI profile before scoring.
- Candidate pools come from official institution websites, not OpenAlex affiliations.
- OpenAlex institution data is used only to accept high-confidence author identity links during benchmark preparation.
- Parameter selection never uses the final real-applicant test set.
- Applicant-family duplicates must remain in the same train/development/test split.

## Ablation Space

| Factor | Values |
|---|---|
| Profile layers | career; career + recent; career + recent + topic clusters |
| Recent window | 3, 5, 8 years |
| Topic clusters | 3, 5, 8, adaptive sqrt(publication count), capped at 12 |
| Sparse profile resolution | 64, 128, 256 weighted terms per vector |
| Representative works per cluster | 1, 3, 5 |
| Publication rerank | none; representative works; all work vectors |

CV/proposal weighting and institution/supervisor logic are frozen so the experiment isolates Research Profile storage resolution.

## Metrics

- Primary: nDCG@10 and MRR.
- Recall: Recall@1, @3, @5, and @10.
- Operational: bytes per PI, build milliseconds per PI, query milliseconds per case.
- Robustness: per-institution metrics and leave-one-institution-out validation.

The selected configuration is the lowest-storage configuration within one PI-clustered standard error of the best nDCG@10 and no more than 0.02 below the best Recall@5. PI clustering prevents two held-out works from the same PI from being treated as independent applicants.

Storage is reported as two separate quantities:

- `publication_manifest_bytes`: lightweight Work ID/DOI/year/title fingerprint data required for incremental refresh and provenance;
- `research_profile_bytes`: vectors actually used by retrieval, including optional topic or paper vectors.

## Pilot Results (2026-07-12)

The pilot used 36 high-confidence publication-linked PIs from the official UBC, Columbia, and Cornell pools, 2,879 works, 72 globally held-out applicant-like cases, and 72 profile configurations. Institution candidate pools contained 9 to 14 eligible PIs.

| Decision scope | Selected representation | nDCG@10 | Recall@5 | Research Profile | Total incl. manifest |
|---|---|---:|---:|---:|---:|
| Current production-term encoder | career 64 + recent 64, 3-year window | 0.9052 | 0.9583 | 1,088 B/PI | 13,721 B/PI |
| TF-IDF robustness baseline | career 256 | 0.9079 | 0.9861 | 2,080 B/PI | 14,713 B/PI |
| Cross-encoder conservative default | career 256 | 0.9061 mean | 0.9583 minimum | 2,080 B/PI | 14,713 B/PI |

The highest raw production-term result added three topic clusters (`nDCG@10=0.9084`), but the selected-minus-best paired 95% bootstrap interval was `[-0.0174, 0.0093]`. The gain is not established on this pilot.

Publication-vector reranking was not stable. Representative vectors changed nDCG@10 by `+0.0031` on the production-term encoder and `-0.0053` on TF-IDF while multiplying Research Profile storage by roughly `2.5x`. All-publication vectors changed it by `-0.0010` and `-0.0055`, respectively, while multiplying storage by roughly `9x`. Therefore publication vectors are not part of the default Research Profile; individual publications remain retrievable evidence on demand.

Leave-one-institution-out selection chose the economical career-only or career-plus-recent family, but exact dimensions varied by institution. The result is suitable for a pilot default, not a permanent global optimum.

## Reproduction

```powershell
python -m pi_index.eval.research_profile_experiment run --dataset outputs\research_profile_eval_20260712\data\research_profile_dataset.json --output-dir outputs\research_profile_eval_20260712\production_terms --encoder production_terms
python -m pi_index.eval.research_profile_experiment run --dataset outputs\research_profile_eval_20260712\data\research_profile_dataset.json --output-dir outputs\research_profile_eval_20260712\tfidf --encoder tfidf
python -m pi_index.eval.research_profile_experiment compare --runs production_terms=outputs\research_profile_eval_20260712\production_terms tfidf=outputs\research_profile_eval_20260712\tfidf --output-dir outputs\research_profile_eval_20260712\comparison
```

## Minimum Real-Applicant Inputs Still Needed

The locally available `fixed_slot_001` through `fixed_slot_054` records are empty placeholders, not populated virtual applicants. A valid external test requires:

- populated CV and proposal text for each slot;
- target institution or allowed institution set;
- graded relevant PI labels (`3=strong`, `2=adjacent`, `1=weak`, `0=not relevant`);
- reviewer identity and evidence URL for each positive label;
- applicant-family/group ID to prevent near-duplicate leakage across splits.

The two currently available real slots can be used as a smoke test, but cannot establish a globally optimal parameter configuration by themselves.
