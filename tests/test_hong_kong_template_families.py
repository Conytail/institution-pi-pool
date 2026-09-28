from __future__ import annotations

from pathlib import Path

from pi_index.adapters.institution_adapter import discover_profile_links_from_people
from pi_index.config import load_institution_config
from pi_index.parsers.generic_html import parse_profile_page
from pi_index.parsers.hkust import parse_hkust_faculty_directory, parse_hkust_faculty_profile
from pi_index.parsers.hku import hku_directory_candidate_count, parse_hku_directory, parse_hku_profile
from pi_index.parsers.pure import parse_pure_person


def _config():
    return {}


def test_hku_business_people_current_directory_url_is_supported():
    html = """
    <div class="people-item">
      <a href="/people/hongbin-cai/"><span>Prof. Hongbin CAI</span></a>
    </div>
    """

    people = parse_hku_directory(
        html,
        "https://www.hkubs.hku.hk/people/",
        _config(),
    )

    assert len(people) == 1
    assert people[0].name == "Hongbin CAI"
    assert people[0].title == "Professoriate Faculty"
    assert people[0].profile_url == "https://www.hkubs.hku.hk/people/hongbin-cai/"


def test_hku_config_uses_the_federated_profile_adapter():
    root = Path(__file__).parent.parent
    config = load_institution_config(root / "configs" / "institutions" / "hku.yaml")

    assert config["parsing"]["profile_adapters"] == ["hku_federated_profile"]
    assert "https://www.web.smlc.hku.hk/honorary-professors" in config["crawl"][
        "exclude_url_patterns"
    ]


def test_hku_directory_keeps_people_but_does_not_follow_aggregate_or_stale_profiles():
    html = """
    <div class="hkuarts_researcher_listing__item" data-name="Faculty Person"
         data-department="Linguistics" data-url="https://linguistics.hku.hk/people/faculty/"></div>
    <div class="hkuarts_researcher_listing__item" data-name="Honorary Musician"
         data-department="Music" data-url="https://www.music.hku.hk/honorary-parttime-affiliated.html"></div>
    <div class="hkuarts_researcher_listing__item" data-name="Cha Paul S.K."
         data-department="SMLC" data-url="https://www.web.smlc.hku.hk/teachingstaff/cha-paul-s.k."></div>
    <div class="hkuarts_researcher_listing__item" data-name="Elizabeth LaCouture"
         data-department="Gender Studies" data-url="https://genderstudies.hku.hk/elizabeth-lacouture/"></div>
    <div class="hkuarts_researcher_listing__item" data-name="Guerillot Benoit Gilles"
         data-department="SMLC" data-url="https://www.web.smlc.hku.hk/teachingstaff/guerillot-benoit-gilles"></div>
    """

    people = parse_hku_directory(
        html,
        "https://arts.hku.hk/about-us/find-an-expert/",
        _config(),
    )

    assert [person.name for person in people] == [
        "Faculty Person",
        "Honorary Musician",
        "Cha Paul S.K.",
        "Elizabeth LaCouture",
        "Guerillot Benoit Gilles",
    ]
    assert [person.profile_url for person in people[:4]] == [None, None, None, None]
    assert people[4].profile_url == (
        "https://www.web.smlc.hku.hk/teachingstaff/guerillot-benoit-gilles-"
    )


def test_hku_music_profile_uses_the_person_header_from_the_archived_page_shape():
    html = """
    <html><head><title>angus-lee</title></head><body>
      <div class="staff-adress">
        <div class="text-block-8"><span>Mr. Angus LEE</span><span>Assistant Lecturer</span></div>
        <div><a href="mailto:anguslyw@hku.hk?subject=Angus%20Lee">anguslyw@hku.hk</a></div>
      </div>
    </body></html>
    """

    person = parse_hku_profile(html, "https://www.music.hku.hk/angus-lee.html")

    assert person is not None
    assert person.name == "Angus LEE"
    assert person.title == "Assistant Lecturer"
    assert person.department == "Department of Music"
    assert person.emails == ["anguslyw@hku.hk"]
    assert person.extraction_method == "hku_federated_profile"


def test_hku_smlc_profile_extracts_h4_name_person_email_and_research_areas():
    html = """
    <html><head><title>Blasco-García Rocío</title></head><body>
      <h6>Senior Lecturer</h6><h4>Blasco-García Rocío</h4>
      <a href="mailto:roblasco@hku.hk">roblasco@hku.hk</a>
      <div><span>Research Area</span></div>
      <div>Development of Spanish studies in Asia, Intercultural development, Telecollaboration</div>
      <h4>Contact Us</h4><a href="mailto:smlc@hku.hk">smlc@hku.hk</a>
    </body></html>
    """

    person = parse_hku_profile(
        html,
        "https://www.web.smlc.hku.hk/teachingstaff/blasco-garc%C3%ADa-roc%C3%ADo",
    )

    assert person is not None
    assert person.name == "Blasco-García Rocío"
    assert person.title == "Senior Lecturer"
    assert person.department == "School of Modern Languages and Cultures"
    assert person.emails == ["roblasco@hku.hk"]
    assert person.ambiguous_emails == ["smlc@hku.hk"]
    assert person.research_areas == [
        "Development of Spanish studies in Asia",
        "Intercultural development",
        "Telecollaboration",
    ]


def test_hku_comparative_literature_profile_splits_role_from_name():
    html = """
    <html><head><title>Prof. Daniel Elam (Assistant Professor) – Department of Comparative Literature</title></head>
    <body><main class="profile">
      <h1>Prof. Daniel Elam (Assistant Professor)</h1>
      <a href="mailto:jdelam@hku.hk">jdelam@hku.hk</a>
      <p>Daniel specialises in transnational Asian and African literatures in the twentieth century,
         modernism, postcolonial theory, and global intellectual history.</p>
    </main><footer><a href="mailto:complit@hku.hk">complit@hku.hk</a></footer></body></html>
    """

    person = parse_hku_profile(
        html,
        "https://complit.hku.hk/index.php/faculty/daniel-elam/",
    )

    assert person is not None
    assert person.name == "Daniel Elam"
    assert person.title == "Assistant Professor"
    assert person.department == "Department of Comparative Literature"
    assert person.emails == ["jdelam@hku.hk"]
    assert person.ambiguous_emails == ["complit@hku.hk"]
    assert person.research_areas == [
        "transnational Asian and African literatures in the twentieth century",
        "modernism",
        "postcolonial theory",
        "global intellectual history",
    ]


def test_hku_comparative_literature_does_not_assign_the_department_email_to_a_person():
    html = """
    <html><head><title>Ian Fong – Department of Comparative Literature</title></head>
    <body><main><h1>Ian Fong</h1><p>Academic and research bio.</p></main>
    <footer><a href="mailto:complit@hku.hk">complit@hku.hk</a></footer></body></html>
    """

    person = parse_hku_profile(
        html,
        "https://complit.hku.hk/index.php/faculty/ian_fong/",
    )

    assert person is not None
    assert person.name == "Ian Fong"
    assert person.emails == []
    assert person.ambiguous_emails == ["complit@hku.hk"]


def test_hku_dentistry_archived_profile_uses_single_person_jsonld():
    html = """
    <html><head><script type="application/ld+json">[
      {"@context":"https://schema.org","@type":"Person",
       "name":"Professor Lo, Edward Chin Man","alternateName":"盧展民",
       "jobTitle":["Chair Professor of Dental Public Health","Clinical Professor"],
       "email":"edward-lo@hku.hk",
       "knowsAbout":["Population Oral Health","Epidemiology of oral diseases"],
       "url":"https://facdent.hku.hk/people/professoriate-staff/profile/edward-lo"},
      {"@context":"https://schema.org","@type":"ProfilePage","name":"Professor Lo"}
    ]</script></head><body><h1>Profile</h1><footer>dentistry@hku.hk</footer></body></html>
    """

    person = parse_hku_profile(
        html,
        "https://facdent.hku.hk/people/professoriate-staff/profile/edward-lo",
    )

    assert person is not None
    assert person.name == "Lo, Edward Chin Man"
    assert person.title == "Chair Professor of Dental Public Health; Clinical Professor"
    assert person.department == "Faculty of Dentistry"
    assert person.emails == ["edward-lo@hku.hk"]
    assert person.research_areas == ["Population Oral Health", "Epidemiology of oral diseases"]


def test_hku_civil_archived_static_profile_extracts_person_block_and_research():
    html = """
    <html><head><title>Department of Civil Engineering, HKU</title></head><body>
      <div class="row"><div class="col-md-10">
        <h4 style="color: green">Professor Y. BAI Professor (Structural Engineering)</h4>
        <a href="mailto:ybaihku@hku.hk">ybaihku@hku.hk</a>
      </div></div>
      <div class="col-md-12"><strong>Research Interests</strong><img src="bar.gif">
        <ul><li>Composite Structures</li><li>Robotic Construction</li></ul>
      </div><footer><a href="mailto:civil@hku.hk">civil@hku.hk</a></footer>
    </body></html>
    """

    person = parse_hku_profile(html, "https://www.civil.hku.hk/pp-baiy.html")

    assert person is not None
    assert person.name == "Y. BAI"
    assert person.title == "Professor"
    assert person.department == "Department of Civil Engineering"
    assert person.emails == ["ybaihku@hku.hk"]
    assert person.research_areas == ["Composite Structures", "Robotic Construction"]


def test_hku_ischool_archived_elementor_profile_uses_page_title_without_splitting_comma():
    html = """
    <html><head><title>So, Hayden K.H. - HKU I-School</title></head><body>
      <div class="e-con-inner">
        <div class="elementor-widget-text-editor"><div class="elementor-widget-container">So, Hayden K.H.</div></div>
        <div>Director, Associate Professor, School of Innovation</div>
        <a href="mailto:skhay@hku.hk">skhay@hku.hk</a>
      </div>
      <div class="elementor-element"><h4>Expertise</h4></div>
      <div class="elementor-element"><p>Reconfigurable Computing; Computer Architecture</p></div>
    </body></html>
    """

    person = parse_hku_profile(html, "https://i-school.hku.hk/people/hso/")

    assert person is not None
    assert person.name == "So, Hayden K.H."
    assert person.title == "Director, Associate Professor, School of Innovation"
    assert person.emails == ["skhay@hku.hk"]
    assert person.research_areas == ["Reconfigurable Computing", "Computer Architecture"]


def test_hku_mechanical_archived_elementor_profile_keeps_general_email_ambiguous():
    html = """
    <html><body>
      <div class="elementor-image-box-content">
        <h3 class="elementor-image-box-title">Chan, Paddy K.L. 陳國樑</h3>
        <p class="elementor-image-box-description">Associate Professor</p>
      </div>
      <div class="elementor-element"><h3>Research Areas</h3></div>
      <div class="elementor-element">Organic memory; Microscale heat transfer</div>
      <footer><a href="mailto:mech@hku.hk">mech@hku.hk</a></footer>
    </body></html>
    """

    person = parse_hku_profile(html, "https://mech.hku.hk/academic-staff/chan-pkl/")

    assert person is not None
    assert person.name == "Chan, Paddy K.L."
    assert person.title == "Associate Professor"
    assert person.emails == []
    assert person.ambiguous_emails == ["mech@hku.hk"]
    assert person.research_areas == ["Organic memory", "Microscale heat transfer"]


def test_hku_sbms_archived_drupal_profile_scopes_person_email():
    html = """
    <html><body><div class="staff-info">
      <h4 class="staff-title"><div class="field--name-field-name">Dong-Yan Jin</div></h4>
      <div class="field--name-field-role"><p>BSc; DMedSc</p>
        <p>Clara and Lawrence Fok Professor in Precision Medicine</p>
        <p>Senior Associate Dean, Graduate School</p></div>
      <a href="mailto:dyjin@hku.hk">dyjin@hku.hk</a>
      <h3>Research Interests</h3><p>Molecular virology and oncology</p>
    </div><footer><a href="mailto:sbms@hku.hk">sbms@hku.hk</a></footer></body></html>
    """

    person = parse_hku_profile(html, "https://www.sbms.hku.hk/staff/dong-yan-jin?from=leadership")

    assert person is not None
    assert person.name == "Dong-Yan Jin"
    assert person.title == (
        "Clara and Lawrence Fok Professor in Precision Medicine; Senior Associate Dean, Graduate School"
    )
    assert person.emails == ["dyjin@hku.hk"]
    assert person.ambiguous_emails == ["sbms@hku.hk"]


def test_hku_chinese_medicine_archived_static_profile_extracts_chinese_identity():
    html = """
    <html><body><div class="cv-profile">
      <div class="cv-name">張樟進教授</div>
      <div class="cv-title">香港大學中醫藥學院院長及教授<br>中西醫結合中心副主任</div>
      <a href="mailto:zhangzj@hku.hk">zhangzj@hku.hk</a>
    </div><div id="researchint"><ul><li>• 中藥神經精神藥理</li><li>• 針刺機制</li></ul></div>
    <footer><a href="mailto:chinmed@hku.hk">chinmed@hku.hk</a></footer></body></html>
    """

    person = parse_hku_profile(html, "https://scm.hku.hk/Views/People/ProfessorZhangJinZhang.html")

    assert person is not None
    assert person.name == "張樟進"
    assert person.title == "香港大學中醫藥學院院長及教授; 中西醫結合中心副主任"
    assert person.emails == ["zhangzj@hku.hk"]
    assert person.ambiguous_emails == ["chinmed@hku.hk"]
    assert person.research_areas == ["中藥神經精神藥理", "針刺機制"]


def test_hku_business_archived_wordpress_profile_keeps_hyphenated_name():
    html = """
    <html><head><title>Li-An Zhou - HKU Business School</title></head><body>
      <div class="team-info_wrapper"><div class="team-title h5">Prof. Li-An ZHOU</div>
        <div>Honorary Professor</div></div>
    </body></html>
    """

    person = parse_hku_profile(html, "https://www.hkubs.hku.hk/people/li-an-zhou/")

    assert person is not None
    assert person.name == "Li-An ZHOU"
    assert person.title == "Honorary Professor"
    assert person.department == "HKU Business School"


def test_pure_profile_extracts_identity_role_affiliation_and_obfuscated_email():
    html = """
    <html><head>
      <link rel="canonical" href="https://scholars.example.edu/en/persons/jane-doe/">
      <script type="application/ld+json">{
        "@context":"https://schema.org", "@type":"Person",
        "name":"DOE Jane, Prof.",
        "affiliation":[{"@type":"Organization","name":"Department of Computing"}]
      }</script>
    </head><body>
      <div class="header person-details">
        <h1>DOE Jane, Prof.</h1>
        <span class="job-title">Research Assistant Professor</span>
        <div class="rendering_personorganisationlistrendererportal">
          <a rel="Organisation" href="/en/organisations/department-of-computing/"><span>Department of Computing</span></a>
        </div>
        <a class="email" data-md5="bWFpbHRvOnhpb25nendAaGtidS5lZHUuaGs=" href="#">protected</a>
      </div>
      <a href="https://orcid.org/0000-0001-2345-6789">ORCID</a>
      <h3>Research Interests</h3><div>Machine learning</div>
    </body></html>
    """

    people = parse_pure_person(html, "https://scholars.example.edu/en/persons/jane-doe/", _config())

    assert len(people) == 1
    person = people[0]
    assert person.name == "DOE Jane"
    assert person.title == "Research Assistant Professor"
    assert person.department == "Department of Computing"
    assert person.emails == ["xiongzw@hkbu.edu.hk"]
    assert person.profile_url == "https://scholars.example.edu/en/persons/jane-doe/"
    assert person.external_ids["orcid"] == "0000-0001-2345-6789"


def test_polyu_pure_profile_scopes_person_email_away_from_support_contact():
    root = Path(__file__).parent.parent
    config = load_institution_config(root / "configs" / "institutions" / "polyu.yaml")
    assert config["parsing"]["profile_adapters"] == ["elsevier_pure_person", "generic_html"]

    html = """
    <html><head>
      <link rel="canonical" href="https://research.polyu.edu.hk/en/persons/tingting-tian/">
      <script type="application/ld+json">
        {"@context":"https://schema.org", "@type":"Person", "name":"Tingting Tian"}
      </script>
      <script>
        window.appData = {"content":[{"id":"0ee58ae4-e8fa-4be4-9add-eda8c3f9c5c9",
          "title":"Tingting Tian","recordType":"person"}]};
      </script>
    </head><body>
      <div class="header person-details">
        <h1>Tingting Tian</h1>
        <span class="job-title">Research Assistant Professor</span>
        <div class="rendering_personorganisationlistrendererportal">
          <a rel="Organisation" href="/en/organisations/school-of-fashion-and-textiles/">
            School of Fashion and Textiles
          </a>
        </div>
      </div>
      <div class="rendering_personorganisationcontactrendererportal">
        <a href="mailto:tingting.tian@polyu.edu.hk">tingting.tian@polyu.edu.hk</a>
      </div>
      <footer>
        <a href="mailto:scholarshub.support@polyu.edu.hk">scholarshub.support@polyu.edu.hk</a>
      </footer>
    </body></html>
    """

    people = parse_pure_person(
        html,
        "https://research.polyu.edu.hk/en/persons/0ee58ae4-e8fa-4be4-9add-eda8c3f9c5c9",
        config,
    )

    assert len(people) == 1
    assert people[0].name == "Tingting Tian"
    assert people[0].title == "Research Assistant Professor"
    assert people[0].emails == ["tingting.tian@polyu.edu.hk"]
    assert people[0].ambiguous_emails == []
    assert people[0].external_ids["official_person_id"] == (
        "research.polyu.edu.hk:uuid:0ee58ae4-e8fa-4be4-9add-eda8c3f9c5c9"
    )


def test_polyu_sitecore_profiles_keep_fung_surname_and_parse_as_people():
    mike = parse_profile_page(
        """
        <html><head><title>Prof. Mike FUNG | School of Accounting and Finance</title></head>
        <body><h2>Prof. Mike FUNG</h2><p>Professor</p>
        <a href="mailto:mike.king-fai.fung@polyu.edu.hk">Email</a></body></html>
        """,
        "https://www.polyu.edu.hk/af/people/academic-staff/prof-mike-fung/",
    )
    andy = parse_profile_page(
        """
        <html><head><title>Dr Andy FUNG | School of Nursing</title></head>
        <body><h1>Research Assistant Professor</h1><h2>Dr Andy FUNG</h2>
        <a href="mailto:andy.hw.fung@polyu.edu.hk">Email</a></body></html>
        """,
        "https://www.polyu.edu.hk/sn/people/research-assistant-professor/dr-andy-fung/",
    )

    assert mike is not None
    assert mike.name == "Mike FUNG"
    assert mike.title == "Professor"
    assert mike.emails == ["mike.king-fai.fung@polyu.edu.hk"]
    assert andy is not None
    assert andy.name == "Andy FUNG"
    assert andy.title == "Research Assistant Professor"
    assert andy.emails == ["andy.hw.fung@polyu.edu.hk"]


def test_pure_profile_does_not_use_title_as_an_admission_gate():
    html = """
    <script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"Postdoctoral Fellow"}</script>
    <div class="person-details"><h1>Jane Doe</h1><span class="job-title">Postdoctoral Fellow</span></div>
    """

    people = parse_pure_person(html, "https://example.edu/en/persons/jane-doe/", _config())

    assert len(people) == 1
    assert people[0].name == "Jane Doe"
    assert people[0].title == "Postdoctoral Fellow"


def test_pure_profile_drops_placeholder_family_name_dash():
    html = """
    <script type="application/ld+json">
      {"@type":"Person","name":"Rashmi-Supriya -","jobTitle":"Assistant Professor"}
    </script>
    <div class="person-details">
      <h1>Rashmi-Supriya -</h1><span class="job-title">Assistant Professor</span>
    </div>
    """

    people = parse_pure_person(
        html,
        "https://scholars.hkbu.edu.hk/en/persons/RASHMISUPRIYA/",
        _config(),
    )

    assert len(people) == 1
    assert people[0].name == "Rashmi-Supriya"


def test_hkust_directory_parser_uses_one_row_per_profile():
    html = """
    <div class="row">
      <div><div class="name"><span class="name-eng">Jane DOE</span></div>
        <div class="post">Director of AI Lab<br><br>Associate Professor<br>
          <span class="unit">Department of Computer Science and Engineering<br></span>
        </div>
      </div>
      <div class="contact"><a href="mailto:jane@ust.hk">jane@ust.hk</a>
        <a href="https://jane.example.org">Personal Web</a>
        <a class="profile-link" href="profiles.php?profile=jane-doe-jane">View Profile</a>
      </div>
    </div>
    """

    people = parse_hkust_faculty_directory(
        html,
        "https://facultyprofiles.hkust.edu.hk/facultylisting.php",
        _config(),
    )

    assert len(people) == 1
    person = people[0]
    assert person.name == "Jane DOE"
    assert person.title == "Director of AI Lab; Associate Professor"
    assert person.department == "Department of Computer Science and Engineering"
    assert person.emails == ["jane@ust.hk"]
    assert person.profile_url == "https://facultyprofiles.hkust.edu.hk/profiles.php?profile=jane-doe-jane"


def test_hkust_directory_membership_is_not_gated_by_appointment_title():
    rows = "".join(
        f"""
        <div class="row">
          <span class="name-eng">{name}</span>
          <div class="post">{title}<span class="unit">Department of Science</span></div>
          <a class="profile-link" href="profiles.php?profile={slug}">View Profile</a>
        </div>
        """
        for name, title, slug in [
            ("Dana DR", "Dr", "dana-dr"),
            ("Laura LECTURER", "Senior Lecturer", "laura-lecturer"),
            ("Helen HONORARY", "Honorary Professor", "helen-honorary"),
            ("Victor VISITING", "Visiting Professor", "victor-visiting"),
            ("Rae RAP", "Research Assistant Professor", "rae-rap"),
            ("Noah UNTITLED", "", "noah-untitled"),
        ]
    )
    people = parse_hkust_faculty_directory(
        rows,
        "https://facultyprofiles.hkust.edu.hk/facultylisting.php",
        {},
    )

    assert [person.name for person in people] == [
        "Dana DR",
        "Laura LECTURER",
        "Helen HONORARY",
        "Victor VISITING",
        "Rae RAP",
        "Noah UNTITLED",
    ]
    assert people[-1].title is None


def test_hkust_profile_extracts_person_local_email_research_and_publications():
    html = """
    <html><body>
      <div id="profile-div">
        <span id="title-name" class="name-eng">Abhiroop MUKHERJEE</span>
        <div class="post">Professor<br><span class="unit">Department of Finance</span></div>
        <div class="contact">
          <a href="mailto:amukherjee@ust.hk">amukherjee@ust.hk</a>
          <a href="https://faculty.example.org/abhiroop">Personal Web</a>
        </div>
      </div>
      <a href="https://orcid.org/0000-0001-9321-1592">ORCID</a>
      <div id="researchinterest"><h2>Research Interest</h2>
        <ul><li>Asian financial markets</li><li>Financial intermediation</li></ul>
      </div>
      <h2>Publications</h2>
      <div class="publication-item"><p>
        <a href="https://doi.org/10.1000/example">Superstar Firms and College Major Choice</a>, 2026
      </p></div>
    </body></html>
    """
    source_url = "https://facultyprofiles.hkust.edu.hk/profiles.php?profile=abhiroop-mukherjee-amukherjee"

    person = parse_hkust_faculty_profile(html, source_url)

    assert person is not None
    assert person.name == "Abhiroop MUKHERJEE"
    assert person.title == "Professor"
    assert person.department == "Department of Finance"
    assert person.emails == ["amukherjee@ust.hk"]
    assert person.research_areas == ["Asian financial markets", "Financial intermediation"]
    assert person.external_ids["orcid"] == "0000-0001-9321-1592"
    assert person.publication_fingerprints[0]["doi"] == "10.1000/example"
    assert person.extraction_method == "hkust_faculty_profile"
    assert parse_hkust_faculty_directory(html, source_url, _config()) == [person]


def test_hkust_config_follows_every_discovered_faculty_profile():
    root = Path(__file__).parent.parent
    config = load_institution_config(root / "configs" / "institutions" / "hkust.yaml")

    assert config["crawl"]["max_depth"] == 1
    assert config["crawl"]["max_pages"] > config["quality_gate"]["minimum_people"]
    assert config["crawl"]["profile_link_limit"] > config["quality_gate"]["minimum_people"]
    assert config["crawl"]["profile_links_from_parsed_people_only"] is True
    assert config["parsing"]["profile_adapters"] == ["hkust_faculty_directory"]

    # More than the crawler's historical default of 25 proves that the HKUST
    # limit is applied to the complete parsed directory, not silently truncated.
    html = "".join(
        f"""
        <div class="row"><span class="name-eng">Faculty Member {index}</span>
          <div class="post">Lecturer<span class="unit">Department {index}</span></div>
          <a class="profile-link" href="profiles.php?profile=faculty-{index}">Profile</a>
        </div>
        """
        for index in range(40)
    )
    source_url = config["crawl"]["seed_urls"][0]
    people = parse_hkust_faculty_directory(html, source_url, config)
    links = discover_profile_links_from_people(
        people,
        source_url,
        config["institution"]["official_domains"],
        config["crawl"]["profile_link_limit"],
    )
    assert len(people) == len(links) == 40
    assert links[-1].endswith("profiles.php?profile=faculty-39")


def test_hku_biomedical_profile_scopes_identity_and_ids_to_the_person_card():
    html = """
    <html><head><title>Department of Obstetrics &amp; Gynaecology</title></head><body>
      <nav><h2>About Us</h2><a href="mailto:obsgyn@hku.hk">Contact</a></nav>
      <a class="profile-card staff-card">
        <div class="staff-card-body">
          <div class="staff-card-info">
            <p class="staff-card-name">Professor Queenie L. LI 李凌君教授</p>
          </div>
          <div class="staff-card-title">Associate Professor <span>MBBS; PhD</span></div>
          <a href="mailto:queenie.lingjun.li@hku.hk">Email</a>
          <a href="https://orcid.org/0000-0003-0685-3189">ORCID</a>
        </div>
      </a>
      <h5>Research Interests</h5>
      <ul><li>Maternal and child health</li><li>Digital health</li></ul>
      <footer><a href="https://orcid.org/0000-0001-1111-2222">Another person</a></footer>
    </body></html>
    """

    person = parse_hku_profile(
        html,
        "https://obsgyn.med.hku.hk/en/Staff/Professor-Queenie-L-LI",
        _config(),
    )

    assert person is not None
    assert person.name == "Queenie L. LI"
    assert person.title == "Associate Professor"
    assert person.department == "Department of Obstetrics and Gynaecology"
    assert person.emails == ["queenie.lingjun.li@hku.hk"]
    assert person.ambiguous_emails == []
    assert person.research_areas == ["Maternal and child health", "Digital health"]
    assert person.external_ids == {
        "orcid": "0000-0003-0685-3189",
        "orcid_url": "https://orcid.org/0000-0003-0685-3189",
    }
    assert person.extraction_method == "hku_federated_profile"


def test_hku_oncology_profile_stub_uses_breadcrumb_without_role_email():
    html = """
    <html><head><title>Department of Clinical Oncology</title></head><body>
      <nav><h2>About Us</h2></nav>
      <ol class="breadcrumb"><li>Academic Staff</li><li><span>Dr Lanqi GONG Profit1</span></li></ol>
      <main><p>Department of Clinical Oncology</p><a href="mailto:oncology@hku.hk">Contact</a></main>
    </body></html>
    """

    person = parse_hku_profile(
        html,
        "https://oncology.med.hku.hk/en/Our-Team/Academic-Staff/Dr-Lanqi-GONG/Dr-Lanqi-GONG-Profit1",
        _config(),
    )

    assert person is not None
    assert person.name == "Lanqi GONG"
    assert person.title is None
    assert person.department == "Department of Clinical Oncology"
    assert person.emails == []
    assert person.ambiguous_emails == ["oncology@hku.hk"]
    assert person.email_association == "ambiguous_email"
    assert person.extraction_method == "hku_federated_profile"


def test_hku_history_profile_uses_appointment_heading_not_biography_sentence():
    html = """
    <html><head><title>Xu Guoqi – DEPARTMENT OF HISTORY</title></head><body>
      <h1 class="has-medium-font-size wp-block-heading">David H. Y. Chang Professor of Chinese History</h1>
      <h1 class="wp-block-heading">The inaugural Kerry Group Professor of Globalization History (2017-2022)</h1>
      <h1 class="wp-block-heading">Founding director of the Institute of Transnational History of China</h1>
      <h1 class="has-medium-font-size wp-block-heading">XU GUOQI</h1>
      <p>Professor Xu Guoqi was born in China and taught in Asia and the USA before joining HKU.</p>
      <a href="mailto:xuguoqi@hku.hk">Professor Xu</a>
      <a href="mailto:history@hku.hk">Department contact</a>
      <h4>Research Interests</h4><ul><li>Transnational history</li></ul>
    </body></html>
    """

    person = parse_hku_profile(html, "https://history.hku.hk/staff-gq-xu/", _config())

    assert person is not None
    assert person.name == "Guoqi Xu"
    assert person.title == "David H. Y. Chang Professor of Chinese History"
    assert person.department == "Department of History"
    assert person.emails == ["xuguoqi@hku.hk"]
    assert person.ambiguous_emails == ["history@hku.hk"]
    assert "was born" not in person.title
    assert person.extraction_method == "hku_federated_profile"


def test_hku_social_work_directory_extracts_current_professoriate_staff():
    html = """
    <div id="faculty-members-list-container">
      <a class="bg-white group cursor-pointer"
         href="https://web.socialwork.hku.hk/faculty-members/prof-vivian-lou/">
        <div class="hover-content">
          <div class="h6">Prof. LOU W.Q. Vivian</div>
          <div class="description">Professor, Head of Department</div>
        </div>
      </a>
    </div>
    """

    people = parse_hku_directory(
        html,
        "https://web.socialwork.hku.hk/faculty-members/",
        _config(),
    )

    assert len(people) == 1
    assert people[0].name == "LOU W.Q. Vivian"
    assert people[0].title == "Professor, Head of Department"
    assert people[0].department == "Department of Social Work and Social Administration"


def test_hku_does_not_drop_honorary_or_emeritus_people_by_title():
    html = """
    <div class="staff-card">
      <div class="staff-card-name">Professor Linda Chan</div>
      <ul class="staff-card-title">
        <li>Clinical Associate Professor</li>
        <li>Honorary Consultant</li>
      </ul>
      <a href="/en/our-team/linda-chan">Profile</a>
    </div>
    <div class="staff-card">
      <div class="staff-card-name">Professor Retired Person</div>
      <ul class="staff-card-title"><li>Emeritus Professor</li></ul>
    </div>
    """

    people = parse_hku_directory(
        html,
        "https://fmpc.hku.hk/en/Our-Team/Academic-Academic-related-and-Medical-Staff",
        _config(),
    )

    assert [person.name for person in people] == ["Linda Chan", "Retired Person"]
    assert people[0].title == "Clinical Associate Professor"
    assert people[1].title == "Emeritus Professor"


def test_hku_wix_clinical_unit_extracts_each_current_academic():
    html = """
    <div class="wixui-repeater__item">
      <p>Dr SIN Wai-ching Simon</p>
      <p>Director, Clinical Associate Professor</p>
      <a href="/academic-staff/simon-sin">Profile</a>
    </div>
    """

    people = parse_hku_directory(
        html,
        "https://hkuccmu.hku.hk/academic-staff",
        _config(),
    )

    assert len(people) == 1
    assert people[0].name == "SIN Wai-ching Simon"
    assert people[0].title == "Director, Clinical Associate Professor"
    assert people[0].department == "Critical Care Medicine Unit"


def test_hku_law_reads_structured_name_and_all_profession_lines():
    html = """
    <div class="staff">
      <h2>Prof. <span id="given">Chen</span> <span id="family">Lin</span></h2>
      <p id="profession">Professor<br>CHAIR OF FINANCE (by courtesy)</p>
    </div>
    <div class="staff">
      <h2>Prof. <span id="given">Denis</span> <span id="family">Chang</span>, CBE, KC, SC, JP</h2>
      <p id="profession">Honorary Professor</p>
    </div>
    <div class="staff">
      <h2><span id="given">The Hon Mr Justice Patrick</span> <span id="family">Chan, GBM</span></h2>
      <p id="profession">Honorary Professor</p>
    </div>
    """

    people = parse_hku_directory(
        html,
        "https://www.law.hku.hk/academic-staff/",
        _config(),
    )

    assert [(person.name, person.title) for person in people] == [
        ("Chen Lin", "Professor; CHAIR OF FINANCE (by courtesy)"),
        ("Denis Chang", "Honorary Professor"),
        ("Patrick Chan", "Honorary Professor"),
    ]


def test_hku_law_profile_uses_person_fields_and_decodes_rot13_email():
    html = """
    <html><body>
      <nav><h2>Dean</h2></nav>
      <div class="int_content_wrapper">
        <div class="left">
          <h2 class="staff_name"><span id="given">Brian</span> <span id="family">Tang</span></h2>
          <p class="staff_info"><span>Principal Professional Practitioner</span></p>
          <p>Executive Director, LITE Lab@HKU</p>
        </div>
        <div class="right">
          <a href="javascript:;" class="mail-link"
             data-enc-email="ojgnat[at]uxh.ux">protected email</a>
        </div>
      </div>
      <footer><a href="mailto:lawfac@hku.hk">Faculty contact</a></footer>
    </body></html>
    """

    person = parse_hku_profile(
        html,
        "https://www.law.hku.hk/academic_staff/brian-tang/",
    )

    assert person is not None
    assert person.name == "Brian Tang"
    assert person.title == "Principal Professional Practitioner"
    assert person.department == "Faculty of Law"
    assert person.emails == ["bwtang@hku.hk"]
    assert person.email_association == "person_local"


def test_hku_law_profile_decodes_only_literal_urlencoded_email_constants():
    html = """
    <div class="int_content_wrapper">
      <div class="left">
        <h2 class="staff_name"><span id="given">Dr Jiahui</span> <span id="family">Duan</span></h2>
        <p class="staff_info"><span>Assistant Professor</span></p>
      </div>
      <div class="right"><script>
        document.getElementById("email").innerHTML =
          eval(decodeURIComponent("%27%6a%68%64%75%61%6e%40%68%6b%75%2e%68%6b%27"))
      </script></div>
    </div>
    """

    person = parse_hku_profile(
        html,
        "https://www.law.hku.hk/academic_staff/dr-jiahui-duan/",
    )

    assert person is not None
    assert person.name == "Jiahui Duan"
    assert person.emails == ["jhduan@hku.hk"]
    assert person.email_association == "person_local"


def test_hku_pathology_keeps_degrees_and_email_out_of_title():
    html = """
    <div class="staff-card">
      <div class="staff-card-name">Dr O Yu, Raymond</div>
      <div class="staff-card-title"><ul>
        <li><b>Clinical Practitioner</b></li>
        <li>MBBS, BSc (Biomedical Sc) (HK)</li>
        <li><a href="mailto:rayo@pathology.hku.hk">rayo@pathology.hku.hk</a></li>
      </ul></div>
      <a href="#">Profile</a>
    </div>
    """

    people = parse_hku_directory(
        html,
        "https://www.patho.hku.hk/en/Our-Team/Academic",
        _config(),
    )

    assert len(people) == 1
    assert people[0].name == "O Yu, Raymond"
    assert people[0].title == "Clinical Practitioner"
    assert people[0].emails == ["rayo@pathology.hku.hk"]
    assert people[0].profile_url is None


def test_hku_sbms_uses_the_person_lightbox_appointment_for_hash_only_cards():
    html = """
    <div class="views-view-grid">
      <div class="views-view-grid__item">
        <h5 class="staff-card__name"><a href="#" data-target="staff-2-1">Lei Chang</a></h5>
      </div>
      <div class="staff-lightbox" id="staff-2-1">
        <h4 class="staff-title">Lei Chang</h4>
        <div class="staff-meta"><p>Honorary Associate Professor</p></div>
      </div>
    </div>
    """

    people = parse_hku_directory(html, "https://www.sbms.hku.hk/faculty", _config())

    assert len(people) == 1
    assert people[0].name == "Lei Chang"
    assert people[0].title == "Honorary Associate Professor"
    assert people[0].profile_url is None


def test_hku_sbms_combines_repeated_leadership_and_academic_rank_cards():
    html = """
    <div class="views-view-grid">
      <div class="views-view-grid__item">
        <h5 class="staff-card__name"><a href="#" data-target="leader">Dong-Yan Jin</a></h5>
      </div>
      <div class="staff-lightbox" id="leader"><div class="staff-meta"><p>Associate Director</p></div></div>
    </div>
    <div class="views-view-grid">
      <div class="views-view-grid__item">
        <h5 class="staff-card__name"><a href="#" data-target="faculty">Dong-Yan Jin</a></h5>
      </div>
      <div class="staff-lightbox" id="faculty"><div class="staff-meta"><p>Professor</p></div></div>
    </div>
    """

    people = parse_hku_directory(html, "https://www.sbms.hku.hk/faculty", _config())

    assert len(people) == 1
    assert people[0].name == "Dong-Yan Jin"
    assert people[0].title == "Associate Director; Professor"


def test_hku_scm_splits_every_list_person_and_inherits_the_group_title():
    html = """
    <div class="row">
      <h5 class="highlightText TCTitleFont">博士後研究員</h5>
      <ul class="staffList">
        <li><span><b>陳薇因</b>博士</span></li>
        <li><span><b>杜巧輝</b>博士</span></li>
      </ul>
      <ul class="staffList"><li><span><b>陳又端</b>博士</span></li></ul>
    </div>
    """

    people = parse_hku_directory(
        html,
        "https://scm.hku.hk/Views/People/ResearchStaff.html",
        _config(),
    )

    assert hku_directory_candidate_count(
        html,
        "https://scm.hku.hk/Views/People/ResearchStaff.html",
        _config(),
    ) == 3
    assert [person.name for person in people] == ["陳薇因", "杜巧輝", "陳又端"]
    assert {person.title for person in people} == {"博士後研究員"}


def test_hku_ece_cleans_trailing_card_delimiters_between_parallel_roles():
    html = """
    <div class="et_pb_blurb">
      <h4 class="et_pb_module_header">Kevin Kin Man TSIA</h4>
      <p>Associate Dean (Teaching &amp; Learning),</p>
      <p>Professor (joint appointment with School of Biomedical Engineering (SBME)),</p>
      <p>Program Director of BEng of Biomedical Engineering, SBME</p>
      <a href="/people/tsia/">Profile</a>
    </div>
    """

    people = parse_hku_directory(html, "https://ece.hku.hk/people/", _config())

    assert len(people) == 1
    assert people[0].title == (
        "Associate Dean (Teaching & Learning); "
        "Professor (joint appointment with School of Biomedical Engineering (SBME)); "
        "Program Director of BEng of Biomedical Engineering, SBME"
    )


def test_hku_scholars_hub_architecture_directory_extracts_research_candidates():
    html = """
    <table class="table crisrp"><tbody>
      <tr>
        <td headers="t1"><a class="authority" href="/cris/rp/rp01304">Bolchover, Joshua Paul</a></td>
        <td headers="t2">-</td>
        <td headers="t3"><em>Department of Architecture</em></td>
        <td headers="t4"><em>Rural urban systems; Architectural design</em></td>
      </tr>
      <tr>
        <td headers="t1"><a class="authority" href="/cris/rp/rp02001">Chan, Jane</a></td>
        <td headers="t2">陳珍</td>
        <td headers="t3"><em>Department of Urban Planning and Design</em></td>
        <td headers="t4"><em>Urban analytics</em></td>
      </tr>
    </tbody></table>
    """
    source_url = (
        "https://hub.hku.hk/simple-search?location=crisrp&filter_field_1=faculty"
        "&filter_type_1=authority&filter_value_1=ou00005"
    )

    people = parse_hku_directory(html, source_url, _config())

    assert hku_directory_candidate_count(html, source_url, _config()) == 2
    assert [person.name for person in people] == [
        "Bolchover, Joshua Paul",
        "Chan, Jane",
    ]
    assert people[0].title is None
    assert people[0].department == "Department of Architecture"
    assert people[0].profile_url == "https://hub.hku.hk/cris/rp/rp01304"
    assert people[0].research_areas == [
        "Rural urban systems",
        "Architectural design",
    ]
    assert people[0].source_type == "official_research_directory"
    assert people[0].confidence == 0.85


def test_hku_architecture_official_directory_extracts_title_unit_and_profile():
    html = """
    <a class="peopleItem filter_academic-staff filter_arch" href="/staff/arch/alain-chiaradia/">
      <span class="name">Alain Chiaradia</span>
      <span class="title">Associate Professor</span>
    </a>
    <a class="peopleItem filter_non-academic-staff filter_arch" href="/staff/arch/office-manager/">
      <span class="name">Office Manager</span>
      <span class="title">Administration</span>
    </a>
    """
    source_url = "https://www.arch.hku.hk/people/arch_staff/"

    people = parse_hku_directory(html, source_url, _config())

    assert hku_directory_candidate_count(html, source_url, _config()) == 1
    assert len(people) == 1
    assert people[0].name == "Alain Chiaradia"
    assert people[0].title == "Associate Professor"
    assert people[0].department == "Department of Architecture"
    assert people[0].profile_url == "https://www.arch.hku.hk/staff/arch/alain-chiaradia/"


def test_hku_psychology_ignores_empty_stale_mailto_in_a_person_row():
    html = """
    <table><tr class="ninja_table_row_0">
      <td><a href="/people/yuanwei-yao/">Dr Yuanwei Yao</a></td>
      <td>Psychometrics</td>
      <td>
        <a href="http://ywyao@hku.hk/">ywyao@hku.hk</a>
        <a href="mailto:singhang@hku.hk"></a>
      </td>
    </tr></table>
    """

    people = parse_hku_directory(
        html,
        "https://psychology.hku.hk/faculty-members/",
        _config(),
    )

    assert len(people) == 1
    assert people[0].name == "Yuanwei Yao"
    assert people[0].emails == ["ywyao@hku.hk"]
    assert "singhang@hku.hk" not in people[0].emails


def test_hku_psychology_keeps_identity_contact_cell_out_of_research_areas():
    html = """
    <table><tr class="ninja_table_row_22">
      <td><img alt="Prof. Yuanwei Yao"></td>
      <td>
        <a href="/people/yuanwei_yao/">Prof. Yuanwei YAO</a><br>
        <strong>Assistant Professor</strong><br>
        Office: 6.04<br>Tel: (852) 3917-5096<br>
        E-mail: <a href="http://ywyao@hku.hk">ywyao@hku.hk</a>
        <a href="mailto:singhang@hku.hk"></a>
      </td>
      <td>
        <strong>Cognitive Psychology and Neuropsychology</strong><br>
        Area of Expertise: Decision making; Addiction; Internet psychology
      </td>
    </tr></table>
    """

    people = parse_hku_directory(
        html,
        "https://psychology.hku.hk/faculty-members/",
        _config(),
    )

    assert len(people) == 1
    assert people[0].name == "Yuanwei YAO"
    assert people[0].emails == ["ywyao@hku.hk"]
    assert people[0].research_areas == [
        "Cognitive Psychology and Neuropsychology Area of Expertise: "
        "Decision making; Addiction; Internet psychology"
    ]


def test_hku_jmsc_directory_does_not_copy_biography_into_title():
    html = """
    <article class="people">
      <h2><a href="/people/richard-allen/">Richard Allen</a></h2>
      <p>Academic Staff</p>
      <div class="biography">
        Richard Allen worked across international newsrooms before joining HKU. This biography describes his
        earlier appointments and notes that he later became a Professor at CityUHK, but it is descriptive prose
        rather than the title attached to this HKU directory entry. It must never be concatenated into the title.
      </div>
    </article>
    """

    people = parse_hku_directory(html, "https://jmsc.hku.hk/people/", _config())

    assert len(people) == 1
    assert people[0].name == "Richard Allen"
    assert people[0].title == "Academic Staff"


def test_hku_paediatrics_prefers_visible_person_email_over_stale_href():
    patrick_html = """
    <a class="profile-card staff-card">
      <div class="staff-card-info"><p class="staff-card-name">Professor IP Patrick (葉柏強)</p></div>
      <div class="staff-card-title"><div class="work-title">Clinical Professor</div></div>
      <div class="staff-card-contact">
        <a href="mailto:xfcheung@hku.hk"><span>patricip@hku.hk</span></a>
        <a href="https://orcid.org/0000-0002-6797-6898">ORCID</a>
      </div>
    </a>
    """
    sabrina_html = """
    <a class="profile-card staff-card">
      <div class="staff-card-info"><p class="staff-card-name">Professor Tsao Siu Ling Sabrina (曹小玲)</p></div>
      <div class="staff-card-title"><div class="work-title">Clinical Associate Professor</div></div>
      <div class="staff-card-contact">
        <a href="mailto:siukk@hku.hk"><span>stsao@hku.hk</span></a>
      </div>
    </a>
    """
    siu_html = """
    <a class="profile-card staff-card">
      <div class="staff-card-info"><p class="staff-card-name">Professor SIU Ka Ka (邵嘉嘉)</p></div>
      <div class="staff-card-title"><div class="work-title">Clinical Associate Professor</div></div>
      <div class="staff-card-contact"><a href="mailto:siukk@hku.hk">siukk@hku.hk</a></div>
    </a>
    """
    cheung_html = """
    <a class="profile-card staff-card">
      <div class="staff-card-info"><p class="staff-card-name">Professor CHEUNG Yiu Fai (張耀輝)</p></div>
      <div class="staff-card-title"><div class="work-title">Clinical Professor</div></div>
      <div class="staff-card-contact"><a href="mailto:xfcheung@hku.hk">xfcheung@hku.hk</a></div>
    </a>
    """

    patrick = parse_hku_profile(
        patrick_html,
        "https://paed.hku.hk/en1/Staff/University-Academic-Staff/doctors/Dr-Patrick-IP.asp",
    )
    sabrina = parse_hku_profile(
        sabrina_html,
        "https://paed.hku.hk/en1/Staff/University-Academic-Staff/doctors/Dr-Sabrina-Siu-ling-Tsao.asp",
    )
    siu = parse_hku_profile(
        siu_html,
        "https://paed.hku.hk/en1/Staff/University-Academic-Staff/doctors/Dr-Siu-Ka-Ka.asp",
    )
    cheung = parse_hku_profile(
        cheung_html,
        "https://paed.hku.hk/en1/Staff/University-Academic-Staff/doctors/Prof-Yiu-fai-Cheung.asp",
    )

    assert patrick is not None and patrick.emails == ["patricip@hku.hk"]
    assert "xfcheung@hku.hk" not in patrick.emails
    assert sabrina is not None and sabrina.emails == ["stsao@hku.hk"]
    assert "siukk@hku.hk" not in sabrina.emails
    assert siu is not None and siu.emails == ["siukk@hku.hk"]
    assert cheung is not None and cheung.emails == ["xfcheung@hku.hk"]


def test_hku_sbme_feifei_discards_copied_wei_ning_lee_contact_block():
    html = """
    <html><head><title>PEOPLE_ Wang_Feifei | School of Biomedical Engineering - HKU</title></head>
    <body>
      <p class="breadcrumb"><a href="/people/leeweining">Wang, Feifei</a></p>
      <h1>Professor<br>Wang, Feifei</h1>
      <p>Programme Director</p>
      <p>Feifei Wang is currently an Assistant Professor at HKU.</p>
      <div class="copied-contact">
        <p>Associate Professor</p><p>Location: CB 506</p>
        <a href="mailto:wnlee@eee.hku.hk">wnlee@eee.hku.hk</a>
        <a href="https://ece.hku.hk/people/wnlee/">Personal Website</a>
        <a href="https://scholar.google.com/citations?user=oh7bGnEAAAAJ">Google Scholar</a>
      </div>
      <footer><a href="mailto:sbme@hku.hk">sbme@hku.hk</a></footer>
    </body></html>
    """

    person = parse_hku_profile(html, "https://sbme.hku.hk/people/wangfeifei")

    assert person is not None
    assert person.name == "Feifei Wang"
    assert person.title == "Assistant Professor"
    assert person.emails == []
    assert person.ambiguous_emails == ["sbme@hku.hk"]
    assert person.email_association == "ambiguous_email"
    assert person.external_ids == {}


def test_hku_sbme_directory_keeps_adjacent_wix_people_cards_separate():
    html = """
    <div class="wixui-box person-card">
      <a href="https://sbme.hku.hk/people/wangfeifei">Portrait</a>
      <p><strong>Professor Wang, Feifei</strong><br>by Courtesy</p>
      <h2>Assistant Professor</h2>
      <a href="https://sbme.hku.hk/people/wangfeifei">DETAILS</a>
    </div>
    <div class="wixui-box person-card">
      <a href="https://sbme.hku.hk/people/leeweining">Portrait</a>
      <p><strong>Professor Lee, Wei-Ning</strong></p>
      <h2>Associate Professor</h2>
      <a href="mailto:wnlee@eee.hku.hk">wnlee@eee.hku.hk</a>
      <a href="https://sbme.hku.hk/people/leeweining">DETAILS</a>
    </div>
    """

    people = parse_hku_directory(html, "https://www.sbme.hku.hk/people", _config())
    by_profile = {person.profile_url: person for person in people}

    feifei = by_profile["https://sbme.hku.hk/people/wangfeifei"]
    wei_ning = by_profile["https://sbme.hku.hk/people/leeweining"]
    assert feifei.name == "Wang, Feifei"
    assert feifei.emails == []
    assert wei_ning.name == "Lee, Wei-Ning"
    assert wei_ning.emails == ["wnlee@eee.hku.hk"]


def test_hku_profile_rejects_page_headings_as_people():
    philosophy_html = """
    <html><head><title>Yaolan (Violet) Luo – Philosophy@HKU</title></head><body>
      <h2>Faculty and staff</h2><h3>Lecturers and Tutors</h3>
      <h4 class="staff_name">Yaolan (Violet) Luo</h4>
    </body></html>
    """
    science_html = """
    <html><head><title>MERILÄ, Juha - Faculty of Science, HKU</title></head><body>
      <h1 class="profile__name">Professor MERILÄ, Juha</h1>
      <h2 class="profile__from">Professor, Chair of Ecology and Biodiversity</h2>
      <h2>Research Interests</h2><ul><li>Local adaptation</li></ul>
      <h2>Current research</h2><ul><li>Population differentiation</li></ul>
    </body></html>
    """
    smlc_html = """
    <html><head><title>Honorary Professors | SMLC</title></head><body>
      <h2>Honorary Professors</h2><h4>Contact Us</h4>
    </body></html>
    """

    philosophy = parse_hku_profile(
        philosophy_html,
        "https://philosophy.hku.hk/staff/yaolan-violet-luo/",
    )
    science = parse_hku_profile(
        science_html,
        "https://www.scifac.hku.hk/people/merilae-juha",
    )
    smlc = parse_hku_profile(
        smlc_html,
        "https://www.web.smlc.hku.hk/honorary-professors",
    )

    assert philosophy is not None and philosophy.name == "Yaolan (Violet) Luo"
    assert science is not None and science.name == "Juha MERILÄ"
    assert science.title == "Professor, Chair of Ecology and Biodiversity"
    assert science.research_areas == ["Local adaptation"]
    assert smlc is None


def test_hku_science_profile_uses_profile_from_even_without_professor_keyword():
    html = """
    <html><body>
      <nav><h2>Dean</h2></nav>
      <h1 class="profile__name">Professor ZHANG, Xiang</h1>
      <h2 class="profile__from">President: Chair of Physics, Department of Physics,
        Faculty of Science, HKU</h2>
      <h2>Research Interest</h2><ul><li>Optical physics</li></ul>
    </body></html>
    """

    person = parse_hku_profile(
        html,
        "https://www.scifac.hku.hk/people/zhang-xiang",
    )

    assert person is not None
    assert person.name == "Xiang ZHANG"
    assert person.title == (
        "President: Chair of Physics, Department of Physics, Faculty of Science, HKU"
    )
    assert person.title != "Dean"
