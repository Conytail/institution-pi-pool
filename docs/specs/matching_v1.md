# Matching v1 Candidate

Matching v1 separates two concerns:

1. `institution_fit_score` is a hard selected-pool constraint.
2. `research_fit_score` compares the applicant query with PI research evidence.

Directory membership is not interpreted as a title-based eligibility
decision. Lecturer, research, clinical, honorary, adjunct and visiting titles remain in
the official pool; downstream ranking uses research evidence and reports contact/current-
appointment uncertainty separately.

The applicant query is `0.8 * proposal + 0.2 * CV`; when CV is absent, proposal weight is
one. Coarse recall uses `career_vector_256`. Full matching reranks the Career Top-10 using
the mean cosine similarity of each PI's three most similar publication vectors, then uses
`max(career_score, paper_top3_score)`.

Authorship, citations and publication time are unweighted in v1. Recent-vector scoring is
not part of v1 because it has not been independently validated. A specific paper match is
not a hard filter and cannot override the institution pool; however, a record must contain
some research evidence (official research areas or a meaningful publication fingerprint)
before it can enter research-fit recommendations.

This policy is experimentally validated but is not wired into the current serving matcher.
The machine-readable source of truth is `configs/matching/matching_v1.yaml`.
