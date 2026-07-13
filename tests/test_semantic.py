from pi_index.match.semantic import paper_relevance_score, research_intent_text, shared_terms


def test_paper_relevance_prefers_specific_problem_terms_over_broad_ml_overlap():
    proposal = "causal graph discovery for clinical treatment response and biomarker mechanisms"
    relevant = "clinical treatment response modeling with causal graph discovery for biomarkers"
    broad = "deep learning optimization for cloud scheduling and neural network systems"

    assert paper_relevance_score(proposal, relevant) > paper_relevance_score(proposal, broad)
    assert paper_relevance_score(proposal, relevant) > 0.08


def test_shared_terms_returns_domain_agnostic_overlap():
    terms = shared_terms(
        "active learning for materials discovery with spectroscopy data",
        "spectroscopy-guided active learning improves materials discovery",
    )

    assert "active learning" in terms
    assert "materials discovery" in terms


def test_research_intent_text_keeps_research_sections_and_skips_cv_metadata():
    cv = """
    Ada Example
    Email: ada@example.edu
    1 RESEARCH INTERESTS
    Active learning for materials discovery and spectroscopy workflows.
    2 EDUCATION
    Master of Science, Example University.
    3 PUBLICATIONS
    Example A, Example B. First author.
    4 TECHNICAL SKILLS
    Python, SQL.
    5 SHORT BIO
    I study automated scientific discovery.
    """

    intent = research_intent_text(cv)

    assert "Active learning for materials discovery" in intent
    assert "automated scientific discovery" in intent
    assert "First author" not in intent
    assert "Master of Science" not in intent
    assert "Python, SQL" not in intent
