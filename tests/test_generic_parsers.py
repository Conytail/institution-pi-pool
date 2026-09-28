from pi_index.parsers.faculty_directory import parse_faculty_directory
from pi_index.parsers.generic_html import extract_title, likely_name, parse_profile_page
from pi_index.parsers.jsonld import parse_jsonld_people
from pi_index.parsers.mailto import extract_emails_from_html


def test_extract_emails_filters_asset_like_values():
    html = "<p>jane.doe@example.edu</p><img src='AI-D-image@2x.jpg'>"
    assert extract_emails_from_html(html) == ["jane.doe@example.edu"]


def test_extract_emails_decodes_cityu_scholars_data_md5_and_ignores_empty_stale_mailto():
    html = """
    <div class="person-card">
      <a class="email" data-md5="bWFpbHRvOmhlc2hjaGVuQGNpdHl1LmVkdS5oaw==" href="#">protected</a>
      <a href="mailto:singhang@hku.hk"></a>
    </div>
    """

    assert extract_emails_from_html(html) == ["heshchen@cityu.edu.hk"]


def test_extract_emails_preserves_isolated_css_icon_mailto():
    html = '<a class="email-icon" href="mailto:jane.doe@example.edu"></a>'

    assert extract_emails_from_html(html) == ["jane.doe@example.edu"]


def test_extract_emails_recovers_legacy_mailt_typo_without_using_it_as_profile():
    html = '<a href="mailt:sabrilam6@cityu.edu.hk">Email</a>'

    assert extract_emails_from_html(html) == ["sabrilam6@cityu.edu.hk"]


def test_visible_email_wins_when_legacy_mailto_href_points_to_another_person():
    html = '<a href="mailto:xfcheung@hku.hk"><span>patricip@hku.hk</span></a>'

    assert extract_emails_from_html(html) == ["patricip@hku.hk"]


def test_hku_unit_role_addresses_are_ambiguous_not_person_local():
    from pi_index.parsers.mailto import split_person_and_ambiguous_emails

    person, ambiguous = split_person_and_ambiguous_emails(
        [
            "english@hku.hk",
            "ortho@hku.hk",
            "anaes@hku.hk",
            "radiology@hku.hk",
            "jane.doe@hku.hk",
        ]
    )

    assert person == ["jane.doe@hku.hk"]
    assert ambiguous == [
        "anaes@hku.hk",
        "english@hku.hk",
        "ortho@hku.hk",
        "radiology@hku.hk",
    ]


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


def test_faculty_directory_does_not_use_phone_link_as_profile_url():
    html = """
    <div class="person">
      <div class="name">Sandy NG</div>
      <a href="tel:3442-4955">3442-4955</a>
      <a href="mailto:sandy.ng@cityu.edu.hk">sandy.ng@cityu.edu.hk</a>
    </div>
    """

    people = parse_faculty_directory(html, "https://www.cityu.edu.hk/ph/people")

    assert len(people) == 1
    assert people[0].name == "Sandy NG"
    assert people[0].profile_url is None
    assert people[0].emails == ["sandy.ng@cityu.edu.hk"]


def test_faculty_directory_extracts_external_identity_links():
    html = """
    <div class="faculty-row">
      <h3>Jane Doe</h3>
      <p>Associate Professor</p>
      <a href="mailto:jane.doe@example.edu">jane.doe@example.edu</a>
      <a href="https://orcid.org/0000-0001-2345-6789">ORCID</a>
      <a href="https://scholar.google.com/citations?user=abc123">Google Scholar</a>
      <a href="https://www.scopus.com/authid/detail.uri?authorId=12345678900">Scopus</a>
      <a href="https://openalex.org/A5012345678">OpenAlex</a>
    </div>
    """
    people = parse_faculty_directory(html, "https://example.edu/people/faculty", ["Associate Professor"])
    assert people[0].external_ids["orcid"] == "0000-0001-2345-6789"
    assert people[0].external_ids["google_scholar_id"] == "abc123"
    assert people[0].external_ids["scopus_author_id"] == "12345678900"
    assert people[0].external_ids["openalex_author_id"] == "A5012345678"
    assert people[0].external_ids["openalex_url"] == "https://openalex.org/A5012345678"


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


def test_nested_views_rows_keep_each_cityu_email_with_its_own_person():
    html = """
    <section class="people-listing">
      <div class="views-row"><h3>Robert Li</h3><p>Professor</p><a href="mailto:aprkyl@cityu.edu.hk">Email</a></div>
      <div class="views-row"><h3>Chunyi Zhi</h3><p>Professor</p><a href="mailto:cy.zhi@cityu.edu.hk">Email</a></div>
      <div class="views-row"><h3>Jonathan Chung</h3><p>Professor</p><a href="mailto:appchung@cityu.edu.hk">Email</a></div>
    </section>
    """

    people = parse_faculty_directory(html, "https://www.cityu.edu.hk/mse/people", ["Professor"])
    by_name = {person.name: person.emails for person in people}

    assert by_name == {
        "Robert Li": ["aprkyl@cityu.edu.hk"],
        "Chunyi Zhi": ["cy.zhi@cityu.edu.hk"],
        "Jonathan Chung": ["appchung@cityu.edu.hk"],
    }


def test_directory_profile_link_is_enough_to_keep_a_dr_without_a_title_or_email():
    html = """
    <div class="faculty-row">
      <h3>Dr Jane Doe</h3>
      <a href="/people/jane-doe">Profile</a>
    </div>
    """

    people = parse_faculty_directory(html, "https://example.edu/faculty", ["Professor"])

    assert len(people) == 1
    assert people[0].name == "Jane Doe"
    assert people[0].title is None
    assert people[0].profile_url == "https://example.edu/people/jane-doe"


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


def test_profile_parser_does_not_treat_site_research_navigation_as_personal_areas():
    html = """
    <html><head><title>Yuanwei Yao | Department of Psychology</title></head><body>
      <nav class="research-areas-menu">
        <a href="/research-areas/">Research Areas</a>
        <a href="/research-areas/clinical-health/">Clinical and Health Psychology</a>
        <a href="/research-areas/cognitive-neuropsychology/">Cognitive Psychology and Neuropsychology</a>
        <a href="/research-areas/developmental-educational/">Developmental and Educational Psychology</a>
        <a href="/research-areas/human-neuroscience-ai/">Human Neuroscience and Artificial Intelligence</a>
        <a href="/research-areas/social-personality/">Social and Personality Psychology</a>
        <a href="/research-laboratories/">Research Laboratories</a>
        <a href="/research-projects/">Research Projects</a>
      </nav>
      <main><h1>Prof. Yuanwei Yao</h1><p>Assistant Professor</p></main>
    </body></html>
    """

    person = parse_profile_page(html, "https://psychology.hku.hk/people/yuanwei_yao/")

    assert person is not None
    assert person.research_areas == []


def test_profile_parser_extracts_explicit_personal_research_areas_block():
    html = """
    <html><head><title>Jane Doe | Staff Profile</title></head><body>
      <main class="person-profile">
        <h1>Dr Jane Doe</h1>
        <p>Senior Lecturer</p>
        <div class="wgl-double_heading">
          <div class="dbl__title-wrapper h3"><span class="dbl__title">Research Interest</span></div>
        </div>
        <div><ul><li>Medical Image Analysis</li><li>Computational Pathology</li></ul></div>
        <div class="site-search"><p>What are you looking for?</p><p>Search this site</p></div>
        <div class="wgl-double_heading">
          <div class="dbl__title-wrapper h3"><span class="dbl__title">Selected Publications</span></div>
        </div>
        <div><ul><li>This publication title is not a research area</li></ul></div>
      </main>
    </body></html>
    """

    person = parse_profile_page(html, "https://example.edu/staff-profiles/jane")

    assert person is not None
    assert person.research_areas == ["Medical Image Analysis", "Computational Pathology"]


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


def test_profile_parser_does_not_promote_site_navigation_dean_to_person_title():
    html = """
    <html><body>
      <nav><h2>Dean</h2><a href="/faculty">Faculty</a></nav>
      <main><h1>Jane Doe</h1><h2>Biography</h2><p>Research biography.</p></main>
    </body></html>
    """

    person = parse_profile_page(html, "https://example.edu/people/jane-doe")

    assert person is not None
    assert person.name == "Jane Doe"
    assert person.title is None


def test_profile_parser_keeps_department_contact_ambiguous_and_title_out_of_biography():
    html = """
    <html><head><title>Richard Allen | HKU Journalism</title></head><body>
      <h1>Richard Allen</h1>
      <p>Honorary Professor</p>
      <h2>Biography</h2>
      <p>Richard Allen previously served in several editorial roles and is now a Professor at another university.</p>
      <h2>General enquiries</h2>
      <a href="mailto:jmsc@hku.hk">jmsc@hku.hk</a>
    </body></html>
    """

    person = parse_profile_page(
        html,
        "https://jmsc.hku.hk/people/richard-allen/",
        ["Honorary Professor", "Professor"],
    )

    assert person is not None
    assert person.title == "Honorary Professor"
    assert person.emails == []
    assert person.ambiguous_emails == ["jmsc@hku.hk"]


def test_hku_hub_profile_recovers_full_name_and_exact_title_from_profile_evidence():
    html = """
    <html><head><title>Alex | HKU Scholars Hub</title></head><body>
      <h1>Alex</h1>
      <h2>Assistant Professor</h2>
      <h3>Biography</h3>
      <p>Prof. Alex Shi is an Assistant Professor in the Faculty of Architecture.</p>
    </body></html>
    """

    person = parse_profile_page(
        html,
        "https://hub.hku.hk/cris/rp/rp02773",
        ["Assistant Professor", "Professor"],
    )

    assert person is not None
    assert person.name == "Alex Shi"
    assert person.title == "Assistant Professor"


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
    assert likely_name("Other Academic Affiliations") is False
    assert likely_name("Lu serves or served as a member of editorial board") is False
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
    assert normalize_name_candidate("Example Fellow") == "Example Fellow"


def test_name_normalization_keeps_all_caps_f_surname_and_requires_comma_for_fellowship():
    from pi_index.parsers.generic_html import normalize_name_candidate

    assert normalize_name_candidate("Prof. Mike FUNG") == "Mike FUNG"
    assert normalize_name_candidate("Dr Andy FUNG") == "Andy FUNG"
    assert normalize_name_candidate("Jane Doe, FIEEE") == "Jane Doe"


def test_generic_profile_does_not_treat_institution_name_as_a_person():
    html = """
    <html><head><title>City University of Hong Kong</title></head>
    <body>
      <h1>City University of Hong Kong</h1>
      <p>Dean</p>
      <a href="mailto:chiachen@cityu.edu.hk">chiachen@cityu.edu.hk</a>
    </body></html>
    """

    assert parse_profile_page(html, "https://www.cityu.edu.hk/bme/chiachen/") is None
