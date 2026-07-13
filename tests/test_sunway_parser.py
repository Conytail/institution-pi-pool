from pathlib import Path

from pi_index.parsers.faculty_directory import parse_faculty_directory
from pi_index.parsers.mailto import extract_emails_from_html
from pi_index.verify.supervisor_signal import supervisor_confidence


FIXTURES = Path(__file__).parent / "fixtures" / "sunway"
SOURCE_URL = "https://sunwayuniversity.edu.my/school-of-computing-and-artificial-intelligence/staff-profiles"
POSITIVE_TITLES = [
    "Programme Leader of Doctor of Philosophy",
    "Programme Leader of PhD",
    "Principal Investigator",
    "Group Leader",
    "Lab Director",
    "Associate Professor",
    "Assistant Professor",
    "Professor",
    "Dean",
    "Deputy Dean",
]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def parse_fixture(name: str):
    return parse_faculty_directory(fixture(name), SOURCE_URL, POSITIVE_TITLES)


def test_sunway_person_card_extracts_name_role_unit_email_and_profile_link():
    people = parse_fixture("person_card.html")
    assert len(people) == 1
    person = people[0]
    assert person.name == "Maya Chen"
    assert person.title == "Associate Professor"
    assert person.department == "Department of Data Science and Artificial Intelligence"
    assert person.emails == ["maya.chen@sunway.edu.my"]
    assert person.profile_url.endswith("/staff-profiles/associate-professor-dr-maya-chen")


def test_sunway_cloudflare_email_decoding():
    assert extract_emails_from_html(fixture("person_card.html")) == ["maya.chen@sunway.edu.my"]


def test_sunway_professor_programme_leader_of_phd_is_verified_supervisor_signal():
    people = parse_fixture("professor_phd_programme_leader.html")
    assert len(people) == 1
    assert "Doctor of Philosophy" in people[0].title
    pi_confidence, likely_supervisor, reasons = supervisor_confidence(people[0].title, "", [])
    assert pi_confidence == "high"
    assert likely_supervisor == "true"
    assert "PhD programme leadership" in reasons[0]


def test_sunway_senior_lecturer_and_lecturer_route_to_review_queue():
    people = parse_fixture("lecturer_cards.html")
    by_name = {person.name: person for person in people}
    assert sorted(by_name) == ["Denise Ng", "Siti Hassan"]
    for person in by_name.values():
        pi_confidence, likely_supervisor, reasons = supervisor_confidence(person.title, "", [])
        assert pi_confidence == "medium"
        assert likely_supervisor == "unknown"
        assert "review-queue role indicator" in reasons[0]
    assert by_name["Denise Ng"].emails == ["denise.ng@sunway.edu.my"]


def test_sunway_dean_or_deputy_dean_with_professor_title_is_verified_signal():
    people = parse_fixture("dean_professor_cards.html")
    assert len(people) == 2
    for person in people:
        pi_confidence, likely_supervisor, _reasons = supervisor_confidence(person.title, "", [])
        assert pi_confidence == "high"
        assert likely_supervisor == "true"


def test_sunway_alumni_office_portfolio_does_not_override_dean_professor_title():
    pi_confidence, likely_supervisor, _reasons = supervisor_confidence(
        "Deputy Dean (Employability & Alumni); Professor",
        "",
        ["Alumni"],
    )
    assert pi_confidence == "high"
    assert likely_supervisor == "true"


def test_sunway_non_person_blocks_are_filtered():
    assert parse_fixture("non_person_block.html") == []


def test_sunway_page_level_email_is_not_assigned_to_person():
    people = parse_fixture("page_level_email.html")
    assert len(people) == 1
    assert people[0].name == "Ari Rahman"
    assert people[0].emails == []
    assert extract_emails_from_html(fixture("page_level_email.html")) == ["info@sunway.edu.my"]
