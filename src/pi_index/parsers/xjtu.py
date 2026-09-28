from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import ParsedPerson
from .generic_html import clean_text
from .mailto import extract_emails_from_html, split_person_and_ambiguous_emails
from .publications import extract_publication_fingerprints


CHINESE_NAME_RE = re.compile(r"^[\u4e00-\u9fff·]{2,6}$")
URL_RE = re.compile(r"https?://[^\s<>\"']+")

RESEARCH_LABELS = [
    "研究领域（方向）",
    "研究领域(方向)",
    "研究方向",
    "研究领域",
    "研究兴趣",
]

SECTION_STOP_LABELS = [
    "个人及工作简历",
    "科研项目",
    "学术及科研成果、专利、论文",
    "学术及科研成果",
    "代表性成果",
    "联系方式",
    "电子邮箱",
    "联系电话",
    "个人主页",
    "联系地址",
    "更新日期",
]

NON_NAME_LINES = {
    "教师内容页",
    "博士生导师",
    "师资队伍",
    "联系方式",
}


def _content_root(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    for selector in [".jiaoshi", ".text.zzjg_list", ".content"]:
        node = soup.select_one(selector)
        if node:
            return node
    return soup.body or soup


def _lines(root: Tag | BeautifulSoup) -> list[str]:
    for tag in root(["script", "style", "noscript"]):
        tag.decompose()
    return [clean_text(line) for line in root.get_text("\n", strip=True).splitlines() if clean_text(line)]


def _extract_name(lines: list[str]) -> str | None:
    for line in lines[:12]:
        if line in NON_NAME_LINES:
            continue
        if CHINESE_NAME_RE.match(line):
            return line
    return None


def _strip_label_value(line: str, label: str) -> str:
    remainder = line[len(label) :].strip()
    return remainder.lstrip(":：").strip()


def _is_stop_line(line: str) -> bool:
    return any(line == label or line.startswith(f"{label}：") or line.startswith(f"{label}:") for label in SECTION_STOP_LABELS)


def _extract_section(lines: list[str], labels: list[str]) -> list[str]:
    chunks: list[str] = []
    collecting = False
    for line in lines:
        if collecting:
            if _is_stop_line(line):
                break
            chunks.append(line)
            continue
        for label in labels:
            if line == label or line.startswith(f"{label}：") or line.startswith(f"{label}:"):
                value = _strip_label_value(line, label)
                if value:
                    chunks.append(value)
                collecting = True
                break
    return chunks


def _split_research_areas(lines: list[str]) -> list[str]:
    areas: list[str] = []
    for line in lines:
        parts = re.split(r"[、,，;；]\s*", line)
        for part in parts:
            value = clean_text(re.sub(r"^[（(]?\d+[）).、]\s*", "", part))
            for label in RESEARCH_LABELS:
                if value.startswith(label):
                    value = _strip_label_value(value, label)
                    break
            value = value.strip(" .。;；,:，：")
            if value and value not in areas and len(value) <= 220:
                areas.append(value)
    return areas


def _extract_homepage(root: Tag | BeautifulSoup, lines: list[str], source_url: str) -> str | None:
    homepage_lines = _extract_section(lines, ["个人主页"])
    for line in homepage_lines:
        match = URL_RE.search(line)
        if match:
            return match.group(0).rstrip("。；;，,")
    for link in root.find_all("a", href=True):
        text = clean_text(link.get_text(" ", strip=True))
        href = link.get("href") or ""
        if "个人主页" in text or "gr.xjtu.edu.cn" in href or "gr.xjtu.edu.cn" in text:
            return urljoin(source_url, href)
    text = "\n".join(lines)
    marker = text.find("个人主页")
    if marker >= 0:
        match = URL_RE.search(text[marker : marker + 300])
        if match:
            return match.group(0).rstrip("。；;，,")
    return None


def parse_xjtu_teacher_profile(html_text: str, source_url: str, config: dict | None = None) -> list[ParsedPerson]:
    if "jsnr.jsp" not in source_url:
        return []
    soup = BeautifulSoup(html_text or "", "html.parser")
    root = _content_root(soup)
    lines = _lines(root)
    name = _extract_name(lines)
    if not name:
        return []

    research_lines = _extract_section(lines, RESEARCH_LABELS)
    research_areas = _split_research_areas(research_lines)
    emails, ambiguous_emails = split_person_and_ambiguous_emails(extract_emails_from_html(str(root)))
    parsing_config = (config or {}).get("parsing", {})
    title = parsing_config.get("default_title") or "博士生导师"
    department = parsing_config.get("default_department") or "人工智能学院"
    evidence_text = clean_text(root.get_text(" ", strip=True))[:1000]

    return [
        ParsedPerson(
            name=name,
            title=title,
            department=department,
            profile_url=source_url,
            lab_url=_extract_homepage(root, lines, source_url),
            emails=emails,
            ambiguous_emails=ambiguous_emails,
            research_areas=research_areas,
            publication_fingerprints=extract_publication_fingerprints(html_text, source_url),
            source_url=source_url,
            source_type="official_profile",
            extraction_method="xjtu_teacher_profile",
            evidence_text=evidence_text,
            confidence=0.9,
            email_association="person_local",
        )
    ]
