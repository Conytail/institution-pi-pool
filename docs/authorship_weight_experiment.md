# Authorship Weight Experiment

## Objective

Test whether authorship metadata improves institution-constrained PI retrieval. The experiment does not interpret author order as actual scientific credit.

The production matcher and public PI Pool API remain unchanged. Profile resolution is frozen to one 256-feature career vector; recent vectors, topic clusters, and paper reranking are disabled so that only authorship weighting changes.

## Data

Stage one uses 36 identity-linked PIs from three official institution pools and 2,879 cached works. The enriched work record stores:

- target PI author position (`first`, `middle`, `last`, `solo`, `unknown`);
- confirmed corresponding status (`true` or `unknown`);
- author count and count band;
- OpenAlex primary field and domain;
- normalized work type and publication year.

OpenAlex `is_corresponding=false` is treated as unknown, not as confirmed non-corresponding. The current data contains 172 confirmed corresponding records (6% of works).

Proposal holdouts are selected to balance author role, corresponding status, and team size. Each Work ID produces at most one proposal case. Two separate works form CV context, and every held-out work is removed globally from all coauthor profiles. Each retained PI keeps at least eight profile works.

## Weight Grid

The benchmark contains one Equal baseline plus 1,151 non-duplicate Role-aware configurations:

| Factor | Values |
|---|---|
| First author | 0.75, 1.0, 1.25, 1.5 |
| Last author | 0.75, 1.0, 1.25, 1.5 |
| Confirmed corresponding | 1.0, 1.25, 1.5, 2.0 |
| Solo author | 1.0, 1.25, 1.5 |
| Middle, 5-10 authors | 0.75, 1.0 |
| Middle, 11+ authors | 0.5, 0.75, 1.0 |

Only non-corresponding middle-author works receive team-size attenuation. Final work weight is clipped to 0.5-2.5. Profiles use the normalized weighted centroid of work vectors.

## Stage-One Result

The full run evaluates 107 leakage-controlled cases and 1,152 configurations under both the production-term and TF-IDF encoders.

The cross-encoder screening optimum has two exactly equivalent parameterizations. Both share first=`1.5`, last=`0.75`, middle 5-10=`1.0`, and middle 11+=`0.75`. The corresponding/solo effects are not identifiable: corresponding=`1.0`, solo=`1.25` and corresponding=`1.25`, solo=`1.0` produce identical ranking metrics.

Against Equal, the deterministic representative produces:

| Encoder | nDCG@10 delta | Paired 95% CI | Hit@5 delta |
|---|---:|---:|---:|
| Production terms | +0.0100 | [-0.0035, +0.0288] | -0.0187 |
| TF-IDF | +0.0132 | [+0.0002, +0.0322] | +0.0093 |

This is a screening signal, not a production recommendation. The production-term confidence interval crosses zero, its Hit@5 regression exceeds the allowed 0.01, only three institution folds are available, and no field meets the Field-aware support threshold.

## Phase-Two Gate

Field-aware learning and production acceptance require all of the following:

- at least 500 official-pool PIs;
- at least 25 institutions;
- at least four OpenAlex domains with 100 PIs each;
- at least 100 PIs from five institutions for every field-specific override;
- positive nDCG gain in at least four of five institution-grouped folds;
- paired CI lower bound above zero and no supported-stratum nDCG regression over 0.02.

Fields below the support threshold always fall back to the global Role-aware configuration. No institution or discipline-specific weights are hard-coded.

## Stage-Two Result

Stage two uses 568 publication-linked PIs from 27 official department/school pools, 39,761 works, and 1,701 leakage-controlled cases. All readiness requirements pass, including four OpenAlex domains with at least 100 PIs and 17 fields eligible for Field-aware evaluation.

The cross-encoder robust configuration is first=`1.25`, last=`1.25`, corresponding=`1.0`, solo=`1.0`, middle 5-10=`0.75`, and middle 11+=`1.0`. Its nDCG@10 gains versus Equal are `+0.00395` for production terms and `+0.00221` for TF-IDF. Neither reaches the pre-registered `+0.005` practical-gain threshold; the TF-IDF robust confidence interval also crosses zero.

The encoder-specific Role-aware optima gain `+0.00395` and `+0.00380`. Field-aware changes relative to Role-aware are negative (`-0.00147` and `-0.00013`). The resulting status is `phase2_no_change`: retain Equal in production. This does not mean Equal has the highest observed score; it means no more complex scheme demonstrated enough robust benefit to pass the deployment gates.

The full report and machine-readable outputs are under `outputs/authorship_weight_eval_20260712/stage2/`.

## Reproduction

```powershell
python -m pi_index.eval.authorship_weight_experiment prepare --dataset outputs\research_profile_eval_20260712\data\research_profile_dataset.json --openalex-cache outputs\research_profile_eval_20260712\data\openalex_cache --output outputs\authorship_weight_eval_20260712\data\authorship_dataset.json --refresh-work-types

python -m pi_index.eval.authorship_weight_experiment run --dataset outputs\authorship_weight_eval_20260712\data\authorship_dataset.json --output-dir outputs\authorship_weight_eval_20260712\stage1

python -m pi_index.eval.stage2_pool --manifest configs\evaluation\authorship_stage2_cohort.yaml --db outputs\authorship_weight_eval_20260712\stage2_pool\pi_index.db --output-dir outputs\authorship_weight_eval_20260712\stage2_pool

python -m pi_index.eval.research_profile_experiment prepare --db outputs\authorship_weight_eval_20260712\stage2_pool\pi_index.db --output-dir outputs\authorship_weight_eval_20260712\stage2_data --max-pis-per-institution 35 --min-works 12 --max-works 100 --start-year 2012 --reserve-credits 40 --official-pool-mode --batch-identity

python -m pi_index.eval.authorship_weight_experiment prepare --dataset outputs\authorship_weight_eval_20260712\stage2_data\research_profile_dataset.json --openalex-cache outputs\authorship_weight_eval_20260712\stage2_data\openalex_cache --output outputs\authorship_weight_eval_20260712\stage2_data\authorship_dataset.json

python -m pi_index.eval.authorship_weight_experiment run --dataset outputs\authorship_weight_eval_20260712\stage2_data\authorship_dataset.json --output-dir outputs\authorship_weight_eval_20260712\stage2 --encoders production_terms tfidf

python -m pi_index.eval.verify_authorship_experiment --dataset outputs\authorship_weight_eval_20260712\stage2_data\authorship_dataset.json --output-dir outputs\authorship_weight_eval_20260712\stage2
```
