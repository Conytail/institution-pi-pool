from __future__ import annotations

from typing import Protocol

from ..models import ParsedPerson


class SourceAdapter(Protocol):
    name: str

    def parse(self, html_text: str, source_url: str, config: dict) -> list[ParsedPerson]:
        ...
