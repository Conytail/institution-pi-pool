import json
from pathlib import Path

from pi_index.config import load_institution_config
from pi_index.parsers.faculty_directory import parse_faculty_directory


ROOT = Path(__file__).parent
FIXTURES = ROOT / "fixtures" / "sunway"
GOLDEN = ROOT / "golden" / "sunway_parser_v1.json"
CONFIG = ROOT.parent / "configs" / "institutions" / "sunway_university_computing_ai.yaml"
FIELDS = ("name", "title", "department", "profile_url", "emails")


def test_sunway_parser_matches_golden_fixture():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    config = load_institution_config(CONFIG)
    title_patterns = config["pi_detection"]["positive_title_patterns"]

    for fixture_name, expected in golden["fixtures"].items():
        html = (FIXTURES / fixture_name).read_text(encoding="utf-8")
        people = parse_faculty_directory(html, golden["source_url"], title_patterns)
        actual = [
            {field: getattr(person, field) for field in FIELDS}
            for person in sorted(people, key=lambda item: item.name)
        ]
        assert actual == expected, fixture_name
