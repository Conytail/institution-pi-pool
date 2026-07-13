import json
from pathlib import Path

from pi_index.config import load_institution_config
from pi_index.parsers.faculty_directory import parse_faculty_directory
from pi_index.parsers.xjtu import parse_xjtu_teacher_profile


ROOT = Path(__file__).parent
REPO = ROOT.parent
GOLDEN_ROOT = ROOT / "golden"
FIXTURE_ROOT = ROOT / "fixtures"
FIELDS = (
    "name",
    "title",
    "department",
    "profile_url",
    "lab_url",
    "emails",
    "ambiguous_emails",
    "research_areas",
    "external_ids",
)


def _records(people):
    return [
        {field: getattr(person, field) for field in FIELDS}
        for person in sorted(people, key=lambda item: item.name)
    ]


def test_every_tracked_institution_is_covered_by_a_template_family_golden():
    coverage = json.loads(
        (GOLDEN_ROOT / "template_family_coverage_v1.json").read_text(encoding="utf-8")
    )
    configs = sorted((REPO / "configs" / "institutions").glob("*.yaml"))
    assert sorted(coverage["institution_configs"]) == [path.name for path in configs]

    for path in configs:
        config = load_institution_config(path)
        family = config["site"]["template_family"]
        assert coverage["institution_configs"][path.name] == family
        suite_name = coverage["golden_suites"][family]
        assert (GOLDEN_ROOT / suite_name).is_file()


def test_generic_faculty_directory_matches_golden():
    golden = json.loads(
        (GOLDEN_ROOT / "generic_faculty_directory_v1.json").read_text(encoding="utf-8")
    )
    config = load_institution_config(REPO / "configs" / "institutions" / "cornell_cs.yaml")
    html = (FIXTURE_ROOT / golden["fixture"]).read_text(encoding="utf-8")
    people = parse_faculty_directory(
        html,
        golden["source_url"],
        config["pi_detection"]["positive_title_patterns"],
    )
    assert _records(people) == golden["records"]


def test_xjtu_teacher_directory_matches_golden():
    golden = json.loads(
        (GOLDEN_ROOT / "xjtu_teacher_directory_v1.json").read_text(encoding="utf-8")
    )
    config = load_institution_config(
        REPO / "configs" / "institutions" / "xian_jiaotong_university_ai.yaml"
    )
    html = (FIXTURE_ROOT / golden["fixture"]).read_text(encoding="utf-8")
    people = parse_xjtu_teacher_profile(html, golden["source_url"], config)
    assert _records(people) == golden["records"]
