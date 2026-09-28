from __future__ import annotations

from ..models import ParsedPerson
from ..parsers.cityu import (
    cityu_academic_candidate_count,
    cityu_federated_candidate_count,
    cityu_multi_person_profile_candidate_count,
    parse_cityu_academic_directory,
    parse_cityu_federated_directory,
    parse_cityu_multi_person_profile,
    parse_cityu_person_profile,
)
from ..parsers.faculty_directory import faculty_directory_candidate_block_count, parse_faculty_directory
from ..parsers.generic_html import parse_profile_page
from ..parsers.hkust import hkust_faculty_candidate_count, parse_hkust_faculty_directory
from ..parsers.hku import hku_directory_candidate_count, parse_hku_directory, parse_hku_profile
from ..parsers.jsonld import parse_jsonld_people
from ..parsers.mailto import extract_emails_from_html
from ..parsers.polyu import parse_polyu_academic_directory, polyu_academic_candidate_count
from ..parsers.pure import parse_pure_person, pure_person_candidate_count
from ..parsers.xjtu import parse_xjtu_teacher_profile


class JSONLDPersonAdapter:
    name = "jsonld_person"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        return parse_jsonld_people(html_text, source_url)


class FacultyDirectoryAdapter:
    name = "faculty_directory"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        return parse_faculty_directory(html_text, source_url)

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
        person = parse_profile_page(html_text, source_url)
        return [person] if person else []


class GenericHTMLAdapter:
    name = "generic_html"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        person = parse_profile_page(html_text, source_url)
        return [person] if person else []


class XJTUTeacherProfileAdapter:
    name = "xjtu_teacher_profile"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        return parse_xjtu_teacher_profile(html_text, source_url, config)


class ElsevierPurePersonAdapter:
    name = "elsevier_pure_person"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        return parse_pure_person(html_text, source_url, config)

    def stats(self, html_text: str, people: list[ParsedPerson]) -> dict[str, int]:
        candidates = pure_person_candidate_count(html_text)
        return {
            "candidate_blocks": candidates,
            "people_extracted": len(people),
            "filtered_blocks": max(0, candidates - len(people)),
        }


class HKUSTFacultyDirectoryAdapter:
    name = "hkust_faculty_directory"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        return parse_hkust_faculty_directory(html_text, source_url, config)

    def stats(self, html_text: str, people: list[ParsedPerson]) -> dict[str, int]:
        candidates = hkust_faculty_candidate_count(html_text)
        return {
            "candidate_blocks": candidates,
            "people_extracted": len(people),
            "filtered_blocks": max(0, candidates - len(people)),
        }


class PolyUAcademicDirectoryAdapter:
    name = "polyu_academic_directory"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        return parse_polyu_academic_directory(html_text, source_url, config)

    def stats(self, html_text: str, people: list[ParsedPerson]) -> dict[str, int]:
        candidates = polyu_academic_candidate_count(html_text)
        return {
            "candidate_blocks": candidates,
            "people_extracted": len(people),
            "filtered_blocks": max(0, candidates - len(people)),
        }


class CityUAcademicDirectoryAdapter:
    name = "cityu_academic_directory"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        return parse_cityu_academic_directory(html_text, source_url, config)

    def stats(self, html_text: str, people: list[ParsedPerson]) -> dict[str, int]:
        candidates = cityu_academic_candidate_count(html_text)
        return {
            "candidate_blocks": candidates,
            "people_extracted": len(people),
            "filtered_blocks": max(0, candidates - len(people)),
        }


class CityUFederatedDirectoryAdapter:
    name = "cityu_federated_directory"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        return parse_cityu_federated_directory(html_text, source_url, config)

    def stats(self, html_text: str, people: list[ParsedPerson]) -> dict[str, int]:
        candidates = cityu_federated_candidate_count(html_text)
        return {
            "candidate_blocks": candidates,
            "people_extracted": len(people),
            "filtered_blocks": max(0, candidates - len(people)),
        }


class CityUPersonProfileAdapter:
    name = "cityu_person_profile"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        person = parse_cityu_person_profile(html_text, source_url, config)
        return [person] if person else []


class CityUMultiPersonProfileAdapter:
    name = "cityu_multi_person_profile"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        return parse_cityu_multi_person_profile(html_text, source_url, config)

    def stats(self, html_text: str, people: list[ParsedPerson]) -> dict[str, int]:
        candidates = cityu_multi_person_profile_candidate_count(html_text)
        return {
            "candidate_blocks": candidates,
            "people_extracted": len(people),
            "filtered_blocks": max(0, candidates - len(people)),
        }


class HKUMultiTemplateDirectoryAdapter:
    name = "hku_multi_template_directory"

    def __init__(self):
        self._candidate_count = 0

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        self._candidate_count = hku_directory_candidate_count(html_text, source_url, config)
        return parse_hku_directory(html_text, source_url, config)

    def stats(self, html_text: str, people: list[ParsedPerson]) -> dict[str, int]:
        candidates = self._candidate_count
        return {
            "candidate_blocks": candidates,
            "people_extracted": len(people),
            "filtered_blocks": max(0, candidates - len(people)),
        }


class HKUFederatedProfileAdapter:
    name = "hku_federated_profile"

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        person = parse_hku_profile(html_text, source_url, config)
        return [person] if person else []


ADAPTERS = {
    adapter.name: adapter
    for adapter in [
        XJTUTeacherProfileAdapter(),
        ElsevierPurePersonAdapter(),
        HKUSTFacultyDirectoryAdapter(),
        PolyUAcademicDirectoryAdapter(),
        CityUAcademicDirectoryAdapter(),
        CityUFederatedDirectoryAdapter(),
        CityUMultiPersonProfileAdapter(),
        CityUPersonProfileAdapter(),
        HKUMultiTemplateDirectoryAdapter(),
        HKUFederatedProfileAdapter(),
        JSONLDPersonAdapter(),
        FacultyDirectoryAdapter(),
        MailtoProfileAdapter(),
        GenericHTMLAdapter(),
    ]
}
