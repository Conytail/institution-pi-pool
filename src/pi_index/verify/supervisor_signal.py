from __future__ import annotations

import re

NEGATIVE_ROLE_PATTERNS = [
    "student",
    "phd candidate",
    "doctoral candidate",
    "postdoctoral researcher",
    "postdoc",
    "research assistant",
    "teaching assistant",
    "administrator",
    "admin",
    "staff",
    "coordinator",
    "emeritus",
    "retired",
    "former",
    "alumni",
    "visitor",
]

REVIEW_ROLE_PATTERNS = [
    "Clinical Professor",
    "Adjunct",
    "Visiting",
    "Affiliate",
    "Research Fellow",
    "Senior Lecturer",
    "Lecturer",
]

STRONG_SUPERVISOR_PATTERNS = [
    "博士生导师",
    "博士研究生导师",
    "博士导师",
    "教授",
    "Associate Professor",
    "Assistant Professor",
    "Full Professor",
    "Professor",
    "Reader",
    "Group Leader",
    "Principal Investigator",
    "Lab Director",
    "Lab Head",
    "Faculty",
]

MEDIUM_SUPERVISOR_PATTERNS = [
    "Senior Lecturer",
    "Lecturer",
    "Professor of Teaching",
    "Associate Professor of Teaching",
    "Assistant Professor of Teaching",
]


def _pattern_matches(text: str, pattern: str) -> bool:
    if not text or not pattern:
        return False
    if pattern.isascii():
        return re.search(rf"\b{re.escape(pattern)}\b", text, re.I) is not None
    return pattern.lower() in text.lower()


def has_negative_title(title: str | None, negative_patterns: list[str]) -> str | None:
    text = title or ""
    for pattern in list(negative_patterns) + NEGATIVE_ROLE_PATTERNS:
        if _pattern_matches(text, pattern):
            if pattern.lower() == "alumni" and re.search(r"\b(dean|professor)\b", text, re.I):
                continue
            return pattern
    return None


def supervisor_signals(title: str | None, research_areas: list[str], positive_patterns: list[str]) -> list[str]:
    signals: list[str] = []
    text = title or ""
    for pattern in positive_patterns:
        if _pattern_matches(text, pattern):
            signals.append(f"title:{pattern}")
            break
    if research_areas:
        signals.append("research_areas_present")
    return signals


def supervisor_confidence(title: str | None, evidence_text: str, negative_patterns: list[str]) -> tuple[str, str, list[str]]:
    text = " ".join(part for part in [title or "", evidence_text or ""] if part)
    negative = has_negative_title(text, negative_patterns)
    if negative:
        return "low", "false", [f"negative role indicator: {negative}"]

    if re.search(r"\b(programme|program)\s+leader\b.*\b(doctor of philosophy|phd)\b", text, re.I):
        return "high", "true", ["explicit PhD programme leadership evidence"]
    if any(pattern in text for pattern in ["博士生导师", "博士研究生导师", "博士导师"]):
        return "high", "true", ["explicit doctoral supervisor evidence"]

    for pattern in REVIEW_ROLE_PATTERNS:
        if _pattern_matches(text, pattern):
            return "medium", "unknown", [f"review-queue role indicator: {pattern}"]

    reasons: list[str] = []
    for pattern in STRONG_SUPERVISOR_PATTERNS:
        if _pattern_matches(text, pattern):
            if re.search(r"\b(teaching|instructional)\b", text, re.I) and "Professor" in pattern:
                return "medium", "unknown", ["teaching-focused professor title is plausible but not sufficient for PhD supervision"]
            reasons.append(f"strong supervisor title: {pattern}")
            return "high", "true", reasons
    for pattern in MEDIUM_SUPERVISOR_PATTERNS:
        if _pattern_matches(text, pattern):
            return "medium", "unknown", [f"plausible but system-dependent supervisor title: {pattern}"]
    if re.search(r"\b(programme|program)\s+leader\b", text, re.I):
        return "medium", "unknown", ["programme leader role lacks explicit PhD supervision evidence"]
    if re.search(r"\b(supervisor|supervision|phd advisor|doctoral advisor)\b", text, re.I):
        return "high", "true", ["explicit supervision evidence"]
    return "unknown", "unknown", ["no strong supervisor title or explicit supervision evidence"]
