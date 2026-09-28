# PI Research Profile Granularity Experiment

## Objective

Measure the retrieval-quality, latency, storage, and external-call trade-off between one
256-feature vector per PI and 256-feature vectors for every publication. This experiment
does not change the production matching API.

## Frozen Protocol

- 568 OpenAlex-linked PIs from 27 official institution pools.
- 1,701 applicant-like cases built from globally held-out publications.
- 2,837 held-out Work IDs are removed from every collaborator's training Profile.
- Institution membership is a hard candidate-pool constraint.
- Proposal and CV weights remain `0.8 / 0.2`.
- Publication aggregation remains Equal; author position is not used.
- Both the production term encoder and TF-IDF robustness encoder are frozen.

The compared retrieval strategies are:

1. `career256`: one Equal-aggregated 256-feature vector per PI.
2. `paper_max256`: the maximum score across every publication vector.
3. `paper_top3_256`: the mean of the three highest publication scores.
4. `hybrid_top10`: retrieve ten PIs with `career256`, then rerank that shortlist with
   `max(career_score, paper_top3_score)`.

## Results

| Encoder | Strategy | nDCG@10 | Hit@5 | Query ms | Vector bytes/PI | nDCG delta vs Career |
|---|---|---:|---:|---:|---:|---:|
| Production terms | Career | 0.865454 | 0.947678 | 0.3021 | 2,080 | - |
| Production terms | Paper max | 0.861680 | 0.935332 | 2.4615 | 98,769 | -0.003620 |
| Production terms | Paper Top-3 | 0.875285 | 0.948854 | 2.8255 | 98,769 | +0.009853 |
| Production terms | Hybrid Top-10 | 0.880196 | 0.951205 | 1.9189 | 100,849 | +0.014717 |
| TF-IDF | Career | 0.878443 | 0.946502 | 0.1968 | 2,080 | - |
| TF-IDF | Paper max | 0.886258 | 0.949442 | 1.7945 | 83,538 | +0.007910 |
| TF-IDF | Paper Top-3 | 0.900484 | 0.961787 | 1.8987 | 83,538 | +0.022149 |
| TF-IDF | Hybrid Top-10 | 0.897400 | 0.958260 | 1.2160 | 85,618 | +0.018924 |

Hybrid Top-10 is the only paper-resolution strategy that clears the predeclared
`+0.005 nDCG@10`, positive paired-bootstrap lower bound, and Hit@5 guardrail under both
encoders. Its nDCG gain is positive in 20/27 institutions for production terms and 23/27
for TF-IDF.

One-paper maximum is not reliable. A single incidental collaboration can dominate the
ranking. Paper Top-3 is better, but its production-term confidence interval crosses zero.

## Cost Boundary

- Encoding cached publication text costs about `31.8-53.3 ms/PI` and is required by both
  storage choices during initial construction.
- Equal career aggregation adds about `5.2-6.5 ms/PI`.
- Career-only matching compares about 21 PI vectors per case.
- Full paper matching compares about 1,381 paper vectors per case.
- Hybrid matching compares about 21 PI vectors and 680-692 paper vectors per case.
- Timings are warm, local, in-memory vector timings; disk, HTTP, and external API latency
  are intentionally excluded.

The hot index should contain `career256`. If the measured quality gain is required, store
publication vectors once in a secondary Work-ID keyed index and use them only for Top-10
reranking. Do not duplicate publication vectors inside each PI record; maintain PI-to-Work
links instead.

## DOI and Refresh

DOI is not required during matching. In this dataset, DOI coverage is `92.32%`, while
OpenAlex Work ID coverage is `100%`. A naive DOI-per-work refresh would require about
35,372 calls and miss `7.68%` of publications. The capped dataset can instead be refreshed
with about 568 author-level work-list requests, then only new or changed Work IDs need to
be encoded.

Keep DOI as an optional alias for deduplication and evidence links. Use the confirmed
OpenAlex Author ID for synchronization and OpenAlex Work ID as the primary publication key.

## Reproduction

```powershell
python -m pi_index.eval.profile_granularity_experiment `
  --dataset outputs\authorship_weight_eval_20260712\stage2_data\authorship_dataset.json `
  --output-dir outputs\profile_granularity_eval_20260712 `
  --encoders production_terms tfidf `
  --feature-limit 256 `
  --shortlist-size 10 `
  --timing-repeats 3 `
  --bootstrap-samples 4000
```

The publication holdout benchmark measures research-direction retrieval. A final claim
about applicant-to-researcher recommendation quality still requires human-labelled real
CV/proposal cases.
