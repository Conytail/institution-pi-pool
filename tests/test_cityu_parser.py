import json
from pathlib import Path

from pi_index.parsers.cityu import (
    cityu_academic_candidate_count,
    cityu_federated_candidate_count,
    cityu_multi_person_profile_candidate_count,
    parse_cityu_academic_directory,
    parse_cityu_federated_directory,
    parse_cityu_multi_person_profile,
    parse_cityu_person_profile,
)


def _config():
    return {
        "pool_scope": {
            "units": [
                {
                    "name": "Department of Management",
                    "seed_urls": ["https://www.cb.cityu.edu.hk/mgt/api/v1/mgt/getmgtpeoplelist"],
                },
                {
                    "name": "Department of Accountancy",
                    "seed_urls": ["https://www.cb.cityu.edu.hk/ac/people/academic/"],
                },
            ]
        },
    }


FIXTURES = Path(__file__).parent / "fixtures"


def test_cityu_directory_keeps_people_without_using_title_as_an_admission_gate():
    html = """
    <div class="component-search-result-1 scholar-result"><div class="result-list">
      <div class="result"><div class="result-content">
        <p class="result-name en">Prof. Espen AARSETH</p>
        <p class="result-category position">Dean (SCM), School of Creative Media, Chair Professor, School of Creative Media</p>
        <p class="result-email"><a href="mailto:eaarseth@cityu.edu.hk">eaarseth@cityu.edu.hk</a></p>
        <p class="result-link"><a href="https://scholars.cityu.edu.hk/en/persons/eaarseth" title="View Profile">View Profile</a></p>
      </div></div>
      <div class="result"><div class="result-content">
        <p class="result-name en">Dr. Example Fellow</p>
        <p class="result-category position">Postdoctoral Fellow, Department of Physics</p>
      </div></div>
    </div></div>
    """

    people = parse_cityu_academic_directory(
        html,
        "https://www.cityu.edu.hk/en/directories/people/academic",
        _config(),
    )

    assert cityu_academic_candidate_count(html) == 2
    assert len(people) == 2
    assert people[0].name == "Espen AARSETH"
    assert people[0].title == "Dean (SCM); Chair Professor"
    assert people[0].department == "School of Creative Media"
    assert people[0].emails == ["eaarseth@cityu.edu.hk"]
    assert people[0].profile_url == "https://scholars.cityu.edu.hk/en/persons/eaarseth"
    assert people[1].name == "Example Fellow"
    assert people[1].title == "Postdoctoral Fellow"
    assert people[1].department == "Department of Physics"


def test_cityu_shared_academic_page_keeps_people_without_position_filters():
    html = """
    <div class="scholar-result">
      <div class="result"><div class="result-content">
        <p class="result-name en">Prof. Core PERSON</p>
        <p class="result-category position">Assistant Professor, Department of Biostatistics</p>
      </div></div>
      <div class="result"><div class="result-content">
        <p class="result-name en">Prof. Affiliate PERSON</p>
        <p class="result-category position">Professor, Department of Physics, Professor, Affiliate, Department of Biostatistics</p>
      </div></div>
      <div class="result"><div class="result-content">
        <p class="result-name en">Prof. Other PERSON</p>
        <p class="result-category position">Professor, Department of Chemistry</p>
      </div></div>
    </div>
    """
    people = parse_cityu_academic_directory(
        html,
        "https://www.cityu.edu.hk/en/directories/people/academic?page=2",
        _config(),
    )

    assert [person.name for person in people] == ["Core PERSON", "Affiliate PERSON", "Other PERSON"]
    assert people[0].department == "Department of Biostatistics"


def test_cityu_federated_cards_keep_all_people_without_title_admission_rules():
    html = """
    <div class="staff-card">
      <div class="name"><a href="/ac/people/profile/alice">Prof. Alice CHAN 陳愛麗教授</a></div>
      <div class="title">Associate Professor</div>
      <a href="mailto:alice.chan@cityu.edu.hk">Email</a>
    </div>
    <div class="staff-card">
      <div class="name">Prof. Visitor PERSON</div>
      <div class="title">Visiting Professor</div>
    </div>
    <div class="staff-card">
      <div class="name">Ms. Admin PERSON</div>
      <div class="title">Executive Officer</div>
    </div>
    """

    people = parse_cityu_federated_directory(
        html,
        "https://www.cb.cityu.edu.hk/ac/people/academic/",
        _config(),
    )

    assert cityu_federated_candidate_count(html) == 3
    assert [person.name for person in people] == ["Alice CHAN", "Visitor PERSON", "Admin PERSON"]
    assert people[0].department == "Department of Accountancy"
    assert people[0].emails == ["alice.chan@cityu.edu.hk"]
    assert people[1].title == "Visiting Professor"
    assert people[2].title == "Executive Officer"


def test_cityu_federated_parser_reads_management_api_pages():
    payload = {
        "GetMgtPeople": {"Page": 1, "TotalPage": 3},
        "Staffs": [
            {
                "data": [
                    {
                        "eid": {"data": "alice"},
                        "staffName": {"data": "Prof. Alice CHAN"},
                        "staffTitle": {"data": "Associate Professor"},
                        "research_area": "Strategy, Innovation",
                    },
                    {
                        "eid": {"data": "visitor"},
                        "staffName": {"data": "Prof. Visitor PERSON"},
                        "staffTitle": {"data": "Visiting Professor"},
                        "research_area": "Strategy",
                    },
                ]
            }
        ],
    }
    source_url = "https://www.cb.cityu.edu.hk/mgt/api/v1/mgt/getmgtpeoplelist?page=1&fulltext="

    people = parse_cityu_federated_directory(json.dumps(payload), source_url, _config())

    assert cityu_federated_candidate_count(json.dumps(payload)) == 2
    assert len(people) == 2
    assert people[0].name == "Alice CHAN"
    assert people[0].research_areas == ["Strategy", "Innovation"]
    assert people[0].profile_url.endswith("detail?eid=alice")
    assert people[1].name == "Visitor PERSON"
    assert people[1].title == "Visiting Professor"


def test_cityu_federated_parser_reads_provenance_marked_reader_markdown():
    markdown = """Title: Academic Staff | Department of Biomedical Engineering

URL Source: http://www.cityu.edu.hk/bme/staff-acad.htm

Markdown Content:
### [Prof. Alice CHAN](https://www.cityu.edu.hk/bme/alice/)

Associate Professor

alice.chan@cityu.edu.hk

### [Prof. Visiting PERSON](https://www.cityu.edu.hk/bme/visitor/)

Visiting Professor
"""
    config = _config()
    config["pool_scope"]["units"].append(
        {
            "name": "Department of Biomedical Engineering",
            "seed_urls": ["https://www.cityu.edu.hk/bme/staff-acad.htm"],
        }
    )

    people = parse_cityu_federated_directory(
        markdown,
        "https://www.cityu.edu.hk/bme/staff-acad.htm",
        config,
    )

    assert cityu_federated_candidate_count(markdown) == 2
    assert len(people) == 2
    assert people[0].name == "Alice CHAN"
    assert people[0].department == "Department of Biomedical Engineering"
    assert people[0].source_type == "official_directory_via_reader"
    assert people[1].name == "Visiting PERSON"
    assert people[1].title == "Visiting Professor"


def test_cityu_federated_parser_reads_profile_json_from_reader():
    payload = {
        "profiles": [
            {
                "profile_name": "Prof. Zudi LU",
                "profile_post": ["Professor"],
                "profile_Email": "zudilu@cityu.edu.hk",
                "profile_Research_Interes": "Time Series, Statistical Learning",
                "profile_Site": ["https://www.cityu.edu.hk/stfprofile/zudilu.htm"],
            },
            {
                "profile_name": "Prof. Visiting PERSON",
                "profile_post": ["Visiting Professor"],
                "profile_Email": "visitor@cityu.edu.hk",
            },
        ]
    }
    markdown = (
        "Title: \n\nURL Source: http://www.cityu.edu.hk/bios/people/profile/staffs.htm\n\n"
        "Markdown Content:\n" + json.dumps(payload)
    )
    config = _config()
    config["pool_scope"]["units"].append(
        {
            "name": "Department of Biostatistics",
            "seed_urls": ["https://www.cityu.edu.hk/bios/people/profile/staffs.htm"],
        }
    )

    people = parse_cityu_federated_directory(
        markdown,
        "https://www.cityu.edu.hk/bios/people/profile/staffs.htm",
        config,
    )

    assert cityu_federated_candidate_count(markdown) == 2
    assert len(people) == 2
    assert people[0].name == "Zudi LU"
    assert people[0].title == "Professor"
    assert people[0].department == "Department of Biostatistics"
    assert people[0].emails == ["zudilu@cityu.edu.hk"]
    assert people[0].research_areas == ["Time Series", "Statistical Learning"]
    assert people[0].source_type == "official_api_via_reader"
    assert people[1].name == "Visiting PERSON"
    assert people[1].title == "Visiting Professor"
    assert people[1].emails == ["visitor@cityu.edu.hk"]


def test_cityu_federated_parser_prefers_structural_names_over_interface_and_role_text():
    config = _config()
    config["pool_scope"]["units"].extend(
        [
            {
                "name": "Department of Social and Behavioural Sciences",
                "seed_urls": ["https://ssweb.cityu.edu.hk/people/staff-profile/academic-staff"],
            },
            {
                "name": "Department of Biomedical Sciences",
                "seed_urls": ["https://www.cityu.edu.hk/bms/people/faculty.htm"],
            },
            {
                "name": "Department of Public and International Affairs",
                "seed_urls": ["https://www.cityu.edu.hk/pia/people.aspx"],
            },
        ]
    )

    social_html = """
    <div class="person person-listing">
      <div class="person__name"><span>WONG, Wing Yee Rebecca</span><span>王穎怡</span></div>
      <div class="person__title">Associate Professor</div>
      <a href="mailto:wywon2@cityu.edu.hk">Email</a>
      <div class="person__info--overview">
        <p>She previously held a Visiting Fellow appointment.</p>
        <a href="https://scholars.cityu.edu.hk/en/persons/rebecca">Learn More</a>
      </div>
    </div>
    """
    biomedical_html = """
    <div class="faculty-item"><div class="card"><a class="stretched-link" href="/bms/profile/xiyao.htm">
      <div class="faculty-name">YAO, Xi<br/>姚希</div></a>
      <h3>Associate Head</h3><h3>Professor</h3>
    </div></div>
    """
    pia_html = """
    <table class="table"><tr><th></th><th>Name</th><th>Position</th></tr><tr>
      <td></td><td><a href="https://scholars.cityu.edu.hk/en/persons/yuk-wah-chan">Yuk Wah CHAN<br/>陳玉華</a></td>
      <td>Associate Professor</td>
      <td><h3>Current/Recent Research Projects</h3></td>
      <td><a href="mailto:yukchan@cityu.edu.hk">Email</a></td>
    </tr></table>
    """

    social = parse_cityu_federated_directory(
        social_html,
        "https://ssweb.cityu.edu.hk/people/staff-profile/academic-staff",
        config,
    )
    biomedical = parse_cityu_federated_directory(
        biomedical_html,
        "https://www.cityu.edu.hk/bms/people/faculty.htm",
        config,
    )
    pia = parse_cityu_federated_directory(
        pia_html,
        "https://www.cityu.edu.hk/pia/people.aspx?r=Academic_Staff",
        config,
    )

    assert [person.name for person in social] == ["WONG, Wing Yee Rebecca"]
    assert social[0].title == "Associate Professor"
    assert social[0].profile_url == "https://scholars.cityu.edu.hk/en/persons/rebecca"
    assert [person.name for person in biomedical] == ["YAO, Xi"]
    assert [person.name for person in pia] == ["Yuk Wah CHAN"]
    assert pia[0].department == "Department of Public and International Affairs"


def test_cityu_federated_parser_builds_business_profile_from_eid():
    html = """
    <div class="card ms-staff"><h4 class="card-title">
      <a data-eid="gufeng">Prof. FENG Guanhao Gavin</a>
    </h4><div class="job-title">Associate Professor</div></div>
    """
    config = _config()
    config["pool_scope"]["units"].append(
        {
            "name": "Department of Decision Analytics and Operations",
            "seed_urls": ["https://www.cb.cityu.edu.hk/dao/about-us/faculty"],
        }
    )

    people = parse_cityu_federated_directory(
        html,
        "https://www.cb.cityu.edu.hk/dao/about-us/faculty",
        config,
    )

    assert len(people) == 1
    assert people[0].profile_url == "https://www.cb.cityu.edu.hk/staff/gufeng/"


def test_cityu_drupal_person_profile_uses_person_fields_not_navigation_cards():
    html = """
    <html><head><title>Prof. DAOUD, Walid | Department of Mechanical Engineering</title></head>
    <body>
      <nav><a href="/students/departmental-awards">Student Achievement</a></nav>
      <div class="field field--name-title">Prof. DAOUD, Walid</div>
      <div class="block-field-blocknodemne-staffbody">
        <div class="field field--name-body">Professor</div>
      </div>
      <div class="field field--name-field-cityu-email">
        <a href="mailto:wdaoud@cityu.edu.hk">wdaoud@cityu.edu.hk</a>
      </div>
      <div class="field field--name-field-research-interests">
        <div class="field__label">Research Interests</div>
        <ul><li>Hybrid nanogenerators</li><li>Flexible photovoltaics</li></ul>
      </div>
      <a href="https://orcid.org/0000-0001-2345-6789">ORCID</a>
    </body></html>
    """
    config = _config()
    config["pool_scope"]["units"].append(
        {
            "name": "Department of Mechanical Engineering",
            "seed_urls": ["https://www.cityu.edu.hk/en/mne/people/academic-staff"],
        }
    )

    person = parse_cityu_person_profile(
        html,
        "https://www.cityu.edu.hk/en/mne/people/academic-staff/mne-faculty/prof-daoud-walid",
        config,
    )

    assert person is not None
    assert person.name == "DAOUD, Walid"
    assert person.title == "Professor"
    assert person.department == "Department of Mechanical Engineering"
    assert person.emails == ["wdaoud@cityu.edu.hk"]
    assert person.research_areas == ["Hybrid nanogenerators", "Flexible photovoltaics"]
    assert person.external_ids["orcid"] == "0000-0001-2345-6789"
    assert person.extraction_method == "cityu_drupal_person_profile"


def test_cityu_drupal_profile_requires_source_specific_person_title_field():
    navigation_only = """
    <html><body><div class="card-title">Student Achievement</div>
    <a href="mailto:office@cityu.edu.hk">Office</a></body></html>
    """

    assert (
        parse_cityu_person_profile(
            navigation_only,
            "https://www.cb.cityu.edu.hk/mgt/about-us/faculty-staff/detail?eid=alice",
            _config(),
        )
        is None
    )


def test_cityu_ee_legacy_profile_uses_visible_contact_and_research_statement():
    html = """
    <html><head><title>Dr. WONG, Eric Wing-Ming, EE CityU HK</title></head>
    <body>
      <span>WONG, Eric Wing-Ming</span>
      <span>Associate Professor</span>
      <b>Email:</b>
      <a href="mailto:ewong@ee.cityu.edu.hk">eeewong@cityu.edu.hk</a>
      <p>His current research interests include in the analysis and design of
      telecommunications networks, optical networks, and cellular networks.</p>
    </body></html>
    """

    person = parse_cityu_person_profile(
        html,
        "https://www.ee.cityu.edu.hk/~ewong/",
        _config(),
    )

    assert person is not None
    assert person.name == "WONG, Eric Wing-Ming"
    assert person.title == "Associate Professor"
    assert person.department == "Department of Electrical Engineering"
    assert person.emails == ["eeewong@cityu.edu.hk"]
    assert person.ambiguous_emails == ["ewong@ee.cityu.edu.hk"]
    assert person.research_areas == [
        "the analysis and design of telecommunications networks, optical networks, and cellular networks"
    ]
    assert person.extraction_method == "cityu_ee_legacy_profile"


def test_cityu_ee_legacy_profile_falls_back_to_first_person_name_line():
    html = """
    <html><body>
      <span>Nelson Sze-Chun Chan</span><span>Professor</span>
      <div>Department of Electrical Engineering</div>
      <a href="mailto:scchan@cityu.edu.hk">scchan@cityu.edu.hk</a>
      <p>His research interests include nonlinear dynamics of semiconductor lasers,
      optical chaos generation, radio-over-fiber, and photonic microwave generation.</p>
    </body></html>
    """

    person = parse_cityu_person_profile(
        html,
        "https://www.ee.cityu.edu.hk/~scchan/",
        _config(),
    )

    assert person is not None
    assert person.name == "Nelson Sze-Chun Chan"
    assert person.title == "Professor"
    assert person.emails == ["scchan@cityu.edu.hk"]
    assert person.research_areas == [
        "nonlinear dynamics of semiconductor lasers, optical chaos generation, "
        "radio-over-fiber, and photonic microwave generation"
    ]


def test_cityu_ee_legacy_placeholder_is_not_a_person():
    html = """
    <html><head><title>Department of EE - City University of Hong Kong</title></head>
    <body><script>window.location = '/en/people/academic_staff/faculty';</script></body></html>
    """

    assert (
        parse_cityu_person_profile(
            html,
            "http://www.ee.cityu.edu.hk/~hangwong/",
            _config(),
        )
        is None
    )


def test_cityu_role_does_not_absorb_research_interest_heading():
    html = """
    <article class="person-card">
      <div class="name">Prof HOU, Junhui David</div>
      <div class="position">Professor</div>
      <div class="interest">
        <div class="title">Research Interests:</div>
        Computer Vision; Machine Learning
      </div>
      <a href="mailto:jh.hou@cityu.edu.hk">Email</a>
      <a href="https://www.cityu.edu.hk/stfprofile/csjhhou.htm">Profile</a>
    </article>
    """

    people = parse_cityu_federated_directory(
        html,
        "https://www.cs.cityu.edu.hk/people/academic-staff",
        _config(),
    )

    assert len(people) == 1
    assert people[0].title == "Professor"


def test_cityu_federated_parser_reads_long_role_from_table_cell():
    html = """
    <table class="table"><tr>
      <td><a href="/en/ph/staff/prof-paudel-surya">Prof. PAUDEL Surya</a></td>
      <td>Assistant Professor/ Interim Director of Jockey Club College of Veterinary Medicine and Life Sciences Research Centre for Applied One Health Research and Policy Advice (OHRP)</td>
      <td><a href="mailto:spaudel@cityu.edu.hk">Email</a></td>
    </tr></table>
    """
    config = _config()
    config["pool_scope"]["units"].append(
        {
            "name": "Department of Infectious Diseases and Public Health",
            "seed_urls": ["https://www.cityu.edu.hk/ph/about-us/our-teams"],
        }
    )

    people = parse_cityu_federated_directory(
        html,
        "https://www.cityu.edu.hk/ph/about-us/our-teams",
        config,
    )

    assert len(people) == 1
    assert people[0].name == "PAUDEL Surya"
    assert people[0].title.startswith("Assistant Professor/ Interim Director")


def test_cityu_bms_alpha_row_enriches_the_matching_card_without_duplication():
    html = """
    <div class="faculty-item"><div class="card">
      <div class="faculty-name">YAN, Jian</div>
      <h3>Associate Professor</h3>
      <a href="/bms/profile/jianyan.htm">View profile</a>
    </div></div>
    <div class="row g-0">
      <div><a class="block-faculty-name" href="/bms/profile/jianyan.htm">YAN, Jian</a></div>
      <div class="researchinterest">Systems Biology &bull; RNA Biology</div>
      <div class="email"><a href="mailto:jian.yan@cityu.edu.hk">jian.yan</a></div>
    </div>
    """

    people = parse_cityu_federated_directory(
        html,
        "https://www.cityu.edu.hk/bms/people/faculty.htm",
        _config(),
    )

    assert len(people) == 1
    assert people[0].name == "YAN, Jian"
    assert people[0].title == "Associate Professor"
    assert people[0].emails == ["jian.yan@cityu.edu.hk"]
    assert people[0].research_areas == ["Systems Biology", "RNA Biology"]


def test_cityu_neuro_alpha_row_without_mailto_does_not_invent_an_email():
    html = """
    <div class="faculty-card">
      <div class="faculty-name">Hee-Sup Shin</div>
      <div class="faculty-position">Distinguished Visiting Professor</div>
      <a href="/neuro/profile/heesupshin.htm">View profile</a>
    </div>
    <div class="row g-0">
      <div><a class="block-faculty-name" href="/neuro/profile/heesupshin.htm">Prof. Shin, Hee-Sup</a></div>
      <div class="researchinterest">Neurobiology of social behaviors</div>
      <div class="email"></div>
    </div>
    """

    people = parse_cityu_federated_directory(
        html,
        "https://www.cityu.edu.hk/neuro/people/faculty.htm",
        _config(),
    )

    assert len(people) == 1
    assert people[0].name == "Hee-Sup Shin"
    assert people[0].emails == []
    assert people[0].research_areas == ["Neurobiology of social behaviors"]


def test_cityu_alpha_rows_never_cross_assign_adjacent_people_emails():
    html = """
    <div class="faculty-card">
      <div class="faculty-name">Alice CHAN</div><div class="faculty-position">Lecturer</div>
      <a href="/bms/profile/alice.htm">View profile</a>
    </div>
    <div class="faculty-card">
      <div class="faculty-name">Bob WONG</div><div class="faculty-position">Research Fellow</div>
      <a href="/bms/profile/bob.htm">View profile</a>
    </div>
    <div class="row g-0">
      <a class="block-faculty-name" href="/bms/profile/alice.htm">Alice CHAN</a>
      <div class="researchinterest">Cancer Biology</div>
      <a href="mailto:alice@cityu.edu.hk">alice</a>
    </div>
    <div class="row g-0">
      <a class="block-faculty-name" href="/bms/profile/bob.htm">Bob WONG</a>
      <div class="researchinterest">Neuroscience</div>
      <a href="mailto:bob@cityu.edu.hk">bob</a>
    </div>
    """

    people = parse_cityu_federated_directory(
        html,
        "https://www.cityu.edu.hk/bms/people/faculty.htm",
        _config(),
    )
    by_name = {person.name: person for person in people}

    assert by_name["Alice CHAN"].emails == ["alice@cityu.edu.hk"]
    assert by_name["Alice CHAN"].research_areas == ["Cancer Biology"]
    assert by_name["Bob WONG"].emails == ["bob@cityu.edu.hk"]
    assert by_name["Bob WONG"].research_areas == ["Neuroscience"]


def test_cityu_collective_navigation_heading_is_not_parsed_as_a_person():
    html = """
    <div class="profile">
      <a href="/neuro/people/faculty.htm">
        <h2>Academic Faculty, Visiting Staff &amp; Adjunct Professors</h2>
      </a>
    </div>
    <div class="faculty-card">
      <div class="faculty-name">Hee-Sup Shin</div>
      <div class="faculty-position">Distinguished Visiting Professor</div>
      <a href="/neuro/profile/heesupshin.htm">View profile</a>
    </div>
    """

    people = parse_cityu_federated_directory(
        html,
        "https://www.cityu.edu.hk/neuro/people/faculty.htm",
        _config(),
    )

    assert [person.name for person in people] == ["Hee-Sup Shin"]


def test_cityu_marketing_role_labels_are_not_parsed_as_people():
    html = """
    <div class="person"><div class="name">Clerical Officer</div>
      <a href="mailto:cylam465@cityu.edu.hk">cylam465@cityu.edu.hk</a></div>
    <div class="person"><div class="name">LAM Hamish</div>
      <a href="mailto:cylam465@cityu.edu.hk">cylam465@cityu.edu.hk</a></div>
    <div class="person"><div class="name">Research Assistant</div>
      <a href="mailto:tclo48@cityu.edu.hk">tclo48@cityu.edu.hk</a></div>
    <div class="person"><div class="name">LO Logan</div>
      <a href="mailto:tclo48@cityu.edu.hk">tclo48@cityu.edu.hk</a></div>
    """

    people = parse_cityu_federated_directory(
        html,
        "https://www.cb.cityu.edu.hk/mkt/about/people/",
        _config(),
    )

    assert [person.name for person in people] == ["LAM Hamish", "LO Logan"]


def test_cityu_name_cleaning_removes_affiliate_rank_without_losing_person():
    html = """
    <div class="faculty-card">
      <div class="faculty-name">Affiliate Prof. CHOW Ho Fai Andy</div>
      <div class="faculty-position">Associate Professor, Affiliate</div>
      <a href="/stfprofile/andychow.htm">View profile</a>
    </div>
    """

    people = parse_cityu_federated_directory(
        html,
        "https://www.cityu.edu.hk/sye/stafflist.htm",
        _config(),
    )

    assert [person.name for person in people] == ["CHOW Ho Fai Andy"]


def test_cityu_legacy_homepage_chrome_is_cleaned_or_rejected():
    valid_html = """
    <html><body>
      <p>Welcome to Lin Dai's HomePage</p>
      <p>Professor</p>
      <p>lindai@cityu.edu.hk</p>
    </body></html>
    """
    invalid_html = """
    <html><body>
      <p>A mist that appears for a little time and then vanishes</p>
      <p>Associate Professor</p>
    </body></html>
    """

    valid = parse_cityu_person_profile(
        valid_html,
        "https://www.ee.cityu.edu.hk/~lindai/",
        _config(),
    )
    invalid = parse_cityu_person_profile(
        invalid_html,
        "https://www.ee.cityu.edu.hk/~not-a-person/",
        _config(),
    )

    assert valid is not None
    assert valid.name == "Lin Dai"
    assert invalid is None


def test_cityu_plural_directory_and_news_headings_are_not_people():
    html = """
    <div class="faculty-card"><div class="faculty-name">Academic Rankings</div></div>
    <div class="faculty-card"><div class="faculty-name">Adjunct Professors</div></div>
    <div class="faculty-card"><div class="faculty-name">Research Professors</div></div>
    <div class="faculty-card"><div class="faculty-name">Distinguished Visiting Professors</div></div>
    <div class="faculty-card"><div class="faculty-name">Our People</div></div>
    <div class="faculty-card"><div class="faculty-name">Page Not Found</div></div>
    <div class="faculty-card"><div class="faculty-name">CityUHK Scholars</div></div>
    <div class="faculty-card"><div class="faculty-name">Congratulations to Dr. A and Research Team on Patent Grant</div></div>
    <div class="faculty-card">
      <div class="faculty-name">Sean Li</div>
      <div class="faculty-position">Visiting Professor</div>
    </div>
    """

    people = parse_cityu_federated_directory(
        html,
        "https://www.cityu.edu.hk/sye/distinguished-visiting-professors.htm",
        _config(),
    )

    assert [person.name for person in people] == ["Sean Li"]


def test_cityu_external_identity_links_are_not_people_or_profile_urls():
    html = """
    <div class="faculty-card">
      <div class="faculty-name">Scopus Author ID</div>
      <a href="https://www.scopus.com/authid/detail.uri?authorId=123456">Scopus</a>
    </div>
    <div class="faculty-card">
      <div class="faculty-name">Youngsub LEE</div>
      <div class="faculty-position">Assistant Professor</div>
      <a href="https://scholar.google.com/citations?user=abc">Google Scholar</a>
      <a href="https://scholars.cityu.edu.hk/">CityUHK Scholars</a>
      <a href="https://www.cityu.edu.hk/error/404?item=x">Broken profile</a>
    </div>
    """

    people = parse_cityu_federated_directory(
        html,
        "https://ssweb.cityu.edu.hk/people/staff-profile/academic-staff",
        _config(),
    )

    assert [person.name for person in people] == ["Youngsub LEE"]
    assert people[0].profile_url is None


def test_cityu_english_adjunct_views_rows_are_person_scoped_cards():
    source_url = "https://www.en.cityu.edu.hk/en/our-people/adjunct-visiting-professors"
    html = (FIXTURES / "cityu_english_adjunct.html").read_text(encoding="utf-8")
    config = _config()
    config["pool_scope"]["units"].append(
        {
            "name": "Department of English",
            "seed_urls": [source_url],
        }
    )

    people = parse_cityu_federated_directory(html, source_url, config)
    by_name = {person.name: person for person in people}

    assert cityu_federated_candidate_count(html) == 4
    assert list(by_name) == [
        "Vijay Bhatia",
        "Stella Bruzzi",
        "Jonathan Culpeper",
        "Elena Semino",
    ]
    assert "Adjunct/Visiting Professors" not in by_name
    assert by_name["Vijay Bhatia"].title == "Adjunct Professor"
    assert by_name["Vijay Bhatia"].department == "Department of English"
    assert by_name["Vijay Bhatia"].emails == ["enbhatia@cityu.edu.hk"]
    assert by_name["Vijay Bhatia"].external_ids["orcid"] == "0000-0001-7336-7426"
    assert by_name["Vijay Bhatia"].external_ids["scopus_author_id"] == "7103071335"
    assert by_name["Vijay Bhatia"].research_areas == [
        "(Critical) Genre Analysis, Professional Communication, ESP, Analysis of Legal, "
        "Business, Academic Discourse, Simplification and Easification of Legislative Discourse."
    ]
    assert by_name["Stella Bruzzi"].title == "Distinguished Visiting Professor"
    assert by_name["Jonathan Culpeper"].external_ids["orcid"] == "0000-0001-9833-6087"
    assert by_name["Jonathan Culpeper"].external_ids["scopus_author_id"] == "6508323747"
    assert by_name["Elena Semino"].external_ids["orcid"] == "0000-0002-3421-2963"
    assert by_name["Elena Semino"].external_ids["scopus_author_id"] == "6505945949"

    profile_people = parse_cityu_multi_person_profile(html, source_url, config)
    assert [person.name for person in profile_people] == list(by_name)
    assert cityu_multi_person_profile_candidate_count(html) == 4


def test_cityu_multi_person_profile_adapter_is_inert_on_an_individual_page():
    html = "<html><body><h1>Jane Doe</h1><p>Professor</p></body></html>"

    assert parse_cityu_multi_person_profile(
        html,
        "https://www.cityu.edu.hk/people/jane-doe",
        _config(),
    ) == []
    assert cityu_multi_person_profile_candidate_count(html) == 0


def test_cityu_physics_cards_join_their_target_modal_without_pseudo_people():
    source_url = "https://www.cityu.edu.hk/en/phy/faculty"
    html = (FIXTURES / "cityu_physics_affiliated_modals.html").read_text(encoding="utf-8")
    config = _config()
    config["pool_scope"]["units"].append(
        {
            "name": "Department of Physics",
            "seed_urls": [source_url],
        }
    )

    people = parse_cityu_federated_directory(html, source_url, config)
    by_name = {person.name: person for person in people}

    assert cityu_federated_candidate_count(html) == 2
    assert list(by_name) == ["CHEN Hesheng", "CLARKE David R."]
    assert "Link to profile" not in by_name
    assert len([person for person in people if person.name == "CLARKE David R."]) == 1
    clarke = by_name["CLARKE David R."]
    assert clarke.title == "Honorary Professor"
    assert clarke.department == "Department of Physics"
    assert clarke.profile_url == "https://clarke.seas.harvard.edu/"
    assert clarke.emails == ["clarke@seas.harvard.edu"]
    assert clarke.research_areas == [
        "Nonlinear conductors and varistors",
        "Thermal barrier coatings",
        "High temperature thermoelectrics",
        "Dielectric elastomer materials and devices",
    ]
    assert clarke.extraction_method == "cityu_affiliated_modal_card"
    assert by_name["CHEN Hesheng"].emails == ["chenhs@ihep.ac.cn"]
