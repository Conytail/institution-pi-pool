from pi_index.parsers.generic_html import parse_profile_page
from pi_index.parsers.publications import (
    extract_publication_fingerprints,
    extract_publication_snapshot,
    normalize_doi,
)


def test_publication_parser_extracts_doi_title_year_and_deduplicates_jsonld():
    html = """
    <html><body>
      <script type="application/ld+json">
      {
        "@type": "ScholarlyArticle",
        "name": "Causal Representation Learning for Biology",
        "datePublished": "2025-04-03",
        "identifier": "doi:10.1000/Example"
      }
      </script>
      <h2>Selected Publications</h2>
      <ul>
        <li><em>Causal Representation Learning for Biology</em>. Journal, 2025.
            <a href="https://doi.org/10.1000/EXAMPLE">DOI</a></li>
        <li><a href="https://arxiv.org/abs/2606.27315">Gradient Equilibrium Is Equivalent</a>, 2026.</li>
      </ul>
      <h2>Teaching</h2><p>Machine Learning, 2024</p>
    </body></html>
    """

    records = extract_publication_fingerprints(html, "https://example.edu/people/jane")
    by_title = {record["title"]: record for record in records}

    assert len(records) == 2
    causal = by_title["Causal Representation Learning for Biology"]
    assert causal["doi"] == "10.1000/example"
    assert causal["publication_year"] == 2025
    assert causal["publication_url"] == "https://doi.org/10.1000/example"
    assert causal["confidence"] == 0.95
    gradient = by_title["Gradient Equilibrium Is Equivalent"]
    assert gradient["publication_year"] == 2026
    assert gradient["doi"] is None
    assert gradient["publication_url"] == "https://arxiv.org/abs/2606.27315"


def test_profile_parser_attaches_publication_fingerprints_to_person():
    html = """
    <html><head><title>Jane Doe - Profile</title></head><body>
      <h1>Jane Doe</h1><p>Professor</p>
      <h2>Publications</h2>
      <p><strong>Reliable Scientific Workflows</strong>, 2024.
         <a href="https://doi.org/10.5555/workflow.1">doi</a></p>
    </body></html>
    """

    person = parse_profile_page(html, "https://example.edu/people/jane", ["Professor"])

    assert person is not None
    assert person.publication_fingerprints == [
        {
            "title": "Reliable Scientific Workflows",
            "citation_text": "Reliable Scientific Workflows, 2024. doi",
            "publication_year": 2024,
            "doi": "10.5555/workflow.1",
            "publication_url": "https://doi.org/10.5555/workflow.1",
            "confidence": 0.9,
        }
    ]


def test_publication_parser_does_not_treat_unlabelled_year_text_as_a_work():
    html = "<html><body><h1>Jane Doe</h1><p>Copyright 2026</p><a href='/publication-policy'>Publication policy</a></body></html>"
    assert extract_publication_fingerprints(html, "https://example.edu/people/jane") == []
    assert normalize_doi("DOI: 10.1234/ABC.Def).") == "10.1234/abc.def"


def test_publication_parser_supports_structured_items_without_a_heading():
    html = """
    <div class="publication-item">
      <a href="https://example.edu/papers/active-learning">Active Learning for Science</a>, 2023.
    </div>
    """
    assert extract_publication_fingerprints(html, "https://example.edu/people/jane") == [
        {
            "title": "Active Learning for Science",
            "citation_text": "Active Learning for Science, 2023.",
            "publication_year": 2023,
            "doi": None,
            "publication_url": "https://example.edu/papers/active-learning",
            "confidence": 0.7,
        }
    ]


def test_hku_visual_selected_publications_heading_is_bounded_and_titles_are_parsed():
    html = """
    <div>
      <div class="wgl-double_heading">
        <div class="dbl__title-wrapper h3"><span class="dbl__title">Selected Publications</span></div>
      </div>
      <div><ul>
        <li>“Managing Conflicts in Relational Contracts,”<br>
            (with Niko Matouschek), <em>American Economic Review</em>, 2013.</li>
        <li>Asset-market Sentiments and Business-cycle Fluctuations
            (with Pengfei Wang), 2024, <em>International Economic Review</em>.</li>
        <li>Xing Hu, Zhixi Wan, 2026. Token Design for Favor Trading with Dynamic Membership.
            <em>Management Science</em>.</li>
        <li>Li, T., &amp; Gal, D. (2024). Consumers Prefer Natural Medicines More When Treating
            Psychological Than Physical Conditions. Journal of Consumer Psychology, 34(1).</li>
        <li>Ming, A., Barnett, S., &amp; Li, X. (Eds.). (2021).
            <em>Green finance and climate policy</em>. International Monetary Fund.</li>
        <li>Zhang, W., D. Zhou, L. Liu. 2014. Contracts for Changing Times:
            Sourcing with Raw Material Price Volatility. <em>Manufacturing Journal</em>.</li>
        <li>H.H. Zhao, H. Deng, R.P. Chen, S.K. Parker, Zhang, W. Fast or Slow:
            How Temporal Work Design Shapes Experienced Passage of Time and Job Performance.
            <em>Academy of Management Journal</em>.</li>
      </ul></div>
      <div class="wgl-double_heading">
        <div class="dbl__title-wrapper h3"><span class="dbl__title">Service to the University</span></div>
      </div>
      <div><p>Reviewed 30 journals, 2025.</p></div>
    </div>
    """

    snapshot = extract_publication_snapshot(html, "https://www.hkubs.hku.hk/people/example/")

    assert snapshot.authoritative is True
    assert snapshot.inventory_signals == ("labelled_section",)
    assert {record["title"] for record in snapshot.fingerprints} == {
        "Managing Conflicts in Relational Contracts",
        "Asset-market Sentiments and Business-cycle Fluctuations",
        "Token Design for Favor Trading with Dynamic Membership",
        "Consumers Prefer Natural Medicines More When Treating Psychological Than Physical Conditions",
        "Green finance and climate policy",
        "Contracts for Changing Times: Sourcing with Raw Material Price Volatility",
        "Fast or Slow: How Temporal Work Design Shapes Experienced Passage of Time and Job Performance",
    }
    assert "Reviewed 30 journals" not in {
        record["title"] for record in snapshot.fingerprints
    }
