# Matching v1 Candidate

Matching v1 separates three concerns:

1. `institution_fit_score` is a hard selected-pool constraint.
2. `research_fit_score` compares the applicant query with PI research evidence.
3. `supervisor_validity_score` represents official evidence of PhD supervision ability.

The applicant query is `0.8 * proposal + 0.2 * CV`; when CV is absent, proposal weight is
one. Coarse recall uses `career_vector_256`. Full matching reranks the Career Top-10 using
the mean cosine similarity of each PI's three most similar publication vectors, then uses
`max(career_score, paper_top3_score)`.

Authorship, citations and publication time are unweighted in v1. Recent-vector scoring is
not part of v1 because it has not been independently validated. Paper evidence is never a
filter and cannot override the institution pool.

This policy is experimentally validated but is not wired into the current serving matcher.
The machine-readable source of truth is `configs/matching/matching_v1.yaml`.
