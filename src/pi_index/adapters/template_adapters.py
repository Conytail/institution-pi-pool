from __future__ import annotations

from ..models import ParsedPerson
from ..parsers.faculty_directory import faculty_directory_candidate_block_count, parse_faculty_directory
from ..parsers.generic_html import parse_profile_page
from ..parsers.jsonld import parse_jsonld_people
from ..parsers.mailto import extract_emails_from_html
from ..parsers.xjtu import parse_xjtu_teacher_profile


class JSONLDPersonAdapter:
    name = "jsonld_person"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        return parse_jsonld_people(html_text, source_url)


class FacultyDirectoryAdapter:
    name = "faculty_directory"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        positive = config.get("pi_detection", {}).get("positive_title_patterns") or []
        return parse_faculty_directory(html_text, source_url, positive)

    def stats(self, html_text: str, people: list[ParsedPerson]) -> dict[str, int]:
        candidate_blocks = faculty_directory_candidate_block_count(html_text)
        return {
            "candidate_blocks": candidate_blocks,
            "people_extracted": len(people),
            "filtered_blocks": max(0, candidate_blocks - len(people)),
        }


class MailtoProfileAdapter:
    name = "mailto_profile"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        emails = extract_emails_from_html(html_text)
        if not emails:
            return []
        person = parse_profile_page(html_text, source_url, config.get("pi_detection", {}).get("positive_title_patterns") or [])
        return [person] if person else []


class GenericHTMLAdapter:
    name = "generic_html"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        person = parse_profile_page(html_text, source_url, config.get("pi_detection", {}).get("positive_title_patterns") or [])
        return [person] if person else []


class XJTUTeacherProfileAdapter:
    name = "xjtu_teacher_profile"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        return parse_xjtu_teacher_profile(html_text, source_url, config)


ADAPTERS = {
    adapter.name: adapter
    for adapter in [
        XJTUTeacherProfileAdapter(),
        JSONLDPersonAdapter(),
        FacultyDirectoryAdapter(),
        MailtoProfileAdapter(),
        GenericHTMLAdapter(),
    ]
}
