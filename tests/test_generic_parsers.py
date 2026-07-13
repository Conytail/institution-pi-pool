from pi_index.parsers.faculty_directory import parse_faculty_directory
from pi_index.parsers.generic_html import extract_title, likely_name, parse_profile_page
from pi_index.parsers.jsonld import parse_jsonld_people
from pi_index.parsers.mailto import extract_emails_from_html


def test_extract_emails_filters_asset_like_values():
    html = "<p>jane.doe@example.edu</p><img src='AI-D-image@2x.jpg'>"
    assert extract_emails_from_html(html) == ["jane.doe@example.edu"]


def test_faculty_directory_table_parser():
    html = """
    <table>
      <tr><th>Name</th><th>Title</th><th>Email</th><th>Research Areas</th></tr>
      <tr>
        <td><a href="/people/jane-doe">Jane Doe</a></td>
        <td>Associate Professor</td>
        <td><a href="mailto:jane.doe@example.edu">jane.doe@example.edu</a></td>
        <td><a href="/research-area/machine-learning">Machine Learning</a></td>
      </tr>
    </table>
    """
    people = parse_faculty_directory(html, "https://example.edu/people/faculty", ["Associate Professor"])
    assert len(people) == 1
    assert people[0].name == "Jane Doe"
    assert people[0].emails == ["jane.doe@example.edu"]
    assert people[0].research_areas == ["Machine Learning"]


def test_faculty_directory_extracts_external_identity_links():
    html = """
    <div class="faculty-row">
      <h3>Jane Doe</h3>
      <p>Associate Professor</p>
      <a href="mailto:jane.doe@example.edu">jane.doe@example.edu</a>
      <a href="https://orcid.org/0000-0001-2345-6789">ORCID</a>
      <a href="https://scholar.google.com/citations?user=abc123">Google Scholar</a>
      <a href="https://www.scopus.com/authid/detail.uri?authorId=12345678900">Scopus</a>
    </div>
    """
    people = parse_faculty_directory(html, "https://example.edu/people/faculty", ["Associate Professor"])
    assert people[0].external_ids["orcid"] == "0000-0001-2345-6789"
    assert people[0].external_ids["google_scholar_id"] == "abc123"
    assert people[0].external_ids["scopus_author_id"] == "12345678900"


def test_multiple_person_cards_keep_local_emails_paired():
    html = """
    <div class="faculty-row">
      <h3>Jane Doe</h3><p>Assistant Professor</p>
      <a href="mailto:jane@example.edu">jane@example.edu</a>
    </div>
    <div class="faculty-row">
      <h3>John Smith</h3><p>Associate Professor</p>
      <a href="mailto:john@example.edu">john@example.edu</a>
    </div>
    """
    people = parse_faculty_directory(html, "https://example.edu/faculty", ["Assistant Professor", "Associate Professor"])
    by_name = {person.name: person for person in people}
    assert by_name["Jane Doe"].emails == ["jane@example.edu"]
    assert by_name["John Smith"].emails == ["john@example.edu"]


def test_missing_email_does_not_inherit_page_email():
    html = """
    <div class="faculty-row">
      <h3>Jane Doe</h3><p>Assistant Professor</p>
      <a href="mailto:jane@example.edu">jane@example.edu</a>
    </div>
    <div class="faculty-row">
      <h3>John Smith</h3><p>Associate Professor</p>
    </div>
    """
    people = parse_faculty_directory(html, "https://example.edu/faculty", ["Assistant Professor", "Associate Professor"])
    by_name = {person.name: person for person in people}
    assert by_name["Jane Doe"].emails == ["jane@example.edu"]
    assert by_name["John Smith"].emails == []


def test_separate_admin_contact_block_is_not_assigned_to_people():
    html = """
    <div class="faculty-row">
      <h3>Jane Doe</h3><p>Assistant Professor</p>
    </div>
    <div class="contact-block">
      <p>Faculty office</p><a href="mailto:office@example.edu">office@example.edu</a>
    </div>
    """
    people = parse_faculty_directory(html, "https://example.edu/faculty", ["Assistant Professor"])
    assert len(people) == 1
    assert people[0].emails == []
    assert people[0].ambiguous_emails == []


def test_admin_email_inside_card_is_ambiguous_not_person_email():
    html = """
    <div class="faculty-row">
      <h3>Jane Doe</h3><p>Assistant Professor</p>
      <a href="mailto:office@example.edu">office@example.edu</a>
    </div>
    """
    people = parse_faculty_directory(html, "https://example.edu/faculty", ["Assistant Professor"])
    assert people[0].emails == []
    assert people[0].ambiguous_emails == ["office@example.edu"]


def test_broad_directory_container_does_not_pair_all_emails_to_first_name():
    html = """
    <article>
      <div class="faculty-row"><h3>Jane Doe</h3><p>Assistant Professor</p><a href="mailto:jane@example.edu">jane@example.edu</a></div>
      <div class="faculty-row"><h3>John Smith</h3><p>Associate Professor</p><a href="mailto:john@example.edu">john@example.edu</a></div>
      <div class="faculty-row"><h3>Ada Lovelace</h3><p>Professor</p><a href="mailto:ada@example.edu">ada@example.edu</a></div>
      <div class="faculty-row"><h3>Grace Hopper</h3><p>Professor</p><a href="mailto:grace@example.edu">grace@example.edu</a></div>
    </article>
    """
    people = parse_faculty_directory(html, "https://example.edu/faculty", ["Assistant Professor", "Associate Professor", "Professor"])
    assert len(people) == 4
    assert all(len(person.emails) == 1 for person in people)


def test_jsonld_person_parser():
    html = """
    <script type="application/ld+json">
    {"@type": "Person", "name": "Jane Doe", "jobTitle": "Professor", "email": "jane@example.edu"}
    </script>
    """
    people = parse_jsonld_people(html, "https://example.edu/jane")
    assert len(people) == 1
    assert people[0].title == "Professor"


def test_profile_parser_extracts_research_interests_section():
    html = """
    <html>
      <head><title>Dr Jane Doe, Staff Profile</title></head>
      <body>
        <h1>Dr Jane Doe</h1>
        <p>Senior Lecturer</p>
        <a href="mailto:jane@example.edu">jane@example.edu</a>
        <h2>Research Interests</h2>
        <p>Medical Image Analysis</p>
        <p>Histopathological Images</p>
        <h2>Teaching Areas</h2>
        <p>Machine Learning</p>
      </body>
    </html>
    """
    person = parse_profile_page(html, "https://example.edu/staff-profiles/jane", ["Senior Lecturer"])
    assert person is not None
    assert person.research_areas == ["Medical Image Analysis", "Histopathological Images"]


def test_profile_parser_prefers_title_near_name_over_navigation():
    html = """
    <html>
      <head><title>Dr Jane Doe, Staff Profile</title></head>
      <body>
        <nav>Overview Head of School Courses Staff Profiles</nav>
        <h1>Dr Jane Doe</h1>
        <p>Senior Lecturer</p>
        <a href="mailto:jane@example.edu">jane@example.edu</a>
      </body>
    </html>
    """
    person = parse_profile_page(html, "https://example.edu/staff-profiles/jane", ["Head of School", "Senior Lecturer"])
    assert person is not None
    assert person.name == "Jane Doe"
    assert person.title == "Senior Lecturer"


def test_extract_title_prefers_specific_pattern():
    title = extract_title(
        "Jane Doe Assistant Professor of Computer Science",
        ["Professor", "Assistant Professor"],
    )
    assert title == "Assistant Professor"


def test_likely_name_rejects_directory_labels():
    assert likely_name("Jane Doe") is True
    assert likely_name("Dr Wan Siti Halimatul Munirah Wan Yahya @ Wan Ahmad") is True
    assert likely_name("Research Areas") is False
    assert likely_name("Notable Publications") is False
    assert likely_name("Main navigation") is False
    assert likely_name("Associate Member - Electrical & Computer Engineering") is False
    assert likely_name("Professor and Vice-President Research & Innovation") is False
    assert likely_name("Associate Head of Faculty Affairs & Professor") is False
    assert likely_name("their CV, research interests and Google Scholar") is False
    assert likely_name("604-822-6421 (Rarely answered; email preferred)") is False


def test_name_normalization_removes_academic_credentials():
    from pi_index.parsers.generic_html import normalize_name_candidate

    assert normalize_name_candidate("Shirin Bahmanyar, Ph.D") == "Shirin Bahmanyar"
    assert normalize_name_candidate("Adams, Charley, Ph.D., CCC-SLP") == "Adams, Charley"
