import pytest

from pi_index.parsers.publications import (
    extract_publication_fingerprints,
    extract_publication_snapshot,
    is_meaningful_publication_fingerprint,
)


@pytest.mark.parametrize(
    "record",
    [
        {"title": "Research output per year"},
        {"title": "Research output: Contribution to journal › Articles › peer-review"},
        {"title": "View all publications"},
        {"title": "Conference Presentations"},
        {"title": "No Publications"},
        {"title": "Journal Publications and Reviews"},
        {"title": "Prize: Awards/ Prizes/ Honours"},
        {"title": "Activity: Talk or Presentation › Invited Talks"},
        {"title": "Press/Media: Media Coverage"},
        {"title": "1 Book Chapter"},
        {"title": "1 item of Media coverage"},
        {"title": "57 Journal Articles"},
        {"title": "1 More 2 Books"},
        {
            "title": "ZHANG, L.",
            "publication_url": "https://example.edu/en/persons/lei-zhang/",
        },
        {"title": "https://scholar.google.com/citations?user=example"},
        {"title": "www.yangliuresearch.com"},
        {
            "title": "1 Authored play, poem, novel, story (Book chapter or short passage)",
            "publication_url": "https://example.edu/person/publications/?type=chapter",
        },
    ],
)
def test_rejects_pure_aggregate_and_navigation_records(record):
    assert not is_meaningful_publication_fingerprint(record)


@pytest.mark.parametrize(
    "record",
    [
        {"title": "12 Angry Men and the Law of Jury Deliberation"},
        {"title": "3 Articles that Changed Competition Law"},
        {"title": "3D Printing of Patient-Specific Implants"},
        {"title": "20 Questions About Reproducible Science", "publication_year": 2024},
        {"title": "1 Book Chapter", "doi": "10.1234/real.chapter"},
        {"title": "융합인재 육성을 위한 대학생 예술역량 모델 개발", "publication_year": 2025},
        {
            "title": "https://figshare.com/articles/dataset/1234",
            "doi": "10.6084/m9.figshare.1234",
        },
    ],
)
def test_preserves_real_titles_that_begin_with_numbers(record):
    assert is_meaningful_publication_fingerprint(record)


def test_extractor_filters_pure_facets_before_returning_fingerprints():
    html = """
    <h2>Publications</h2>
    <div>
      <p>Research output per year</p>
      <p><a href="/person/publications/?type=chapter">1 Book Chapter</a></p>
      <p><a href="/works/twelve-angry-men">12 Angry Men and the Law of Jury Deliberation</a></p>
      <p><strong>Reliable Scientific Workflows</strong>, 2024.</p>
    </div>
    """

    records = extract_publication_fingerprints(html, "https://example.edu/person/jane")

    assert {record["title"] for record in records} == {
        "12 Angry Men and the Law of Jury Deliberation",
        "Reliable Scientific Workflows",
    }


def test_snapshot_marks_only_explicit_inventory_as_authoritative():
    labelled = extract_publication_snapshot(
        "<h1>Jane Doe</h1><h2>Publications</h2><p>Reliable Scientific Workflows, 2024.</p>",
        "https://example.edu/people/jane",
    )
    scattered_doi = extract_publication_snapshot(
        "<h1>Jane Doe</h1><p>Reliable Scientific Workflows <a href='https://doi.org/10.1234/x'>DOI</a></p>",
        "https://example.edu/people/jane",
    )

    assert labelled.authoritative is True
    assert labelled.inventory_signals == ("labelled_section",)
    assert scattered_doi.fingerprints
    assert scattered_doi.authoritative is False
