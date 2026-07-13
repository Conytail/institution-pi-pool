from __future__ import annotations

from typing import Protocol

from ..models import ParsedPerson


class LLMFallbackParser(Protocol):
    """Optional strict-JSON fallback interface.

    The MVP does not depend on an LLM. Implementations must never invent missing
    emails and must return validated ParsedPerson-compatible records.
    """

    def parse_people(self, html_text: str, source_url: str) -> list[ParsedPerson]:
        ...
