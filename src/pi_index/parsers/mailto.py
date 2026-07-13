from __future__ import annotations

import html
import re
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])")
BAD_TLDS = {"jpg", "jpeg", "png", "gif", "webp", "svg", "css", "js"}
ROLE_LOCAL_PARTS = {
    "admin",
    "admissions",
    "contact",
    "enquiries",
    "enquiry",
    "faculty",
    "finance",
    "help",
    "info",
    "office",
    "support",
    "webmaster",
}


def normalize_email(email: str) -> str:
    return email.strip().strip(".,;:()[]<>").lower()


def is_probable_email(email: str) -> bool:
    email = normalize_email(email)
    if "@" not in email:
        return False
    local, domain = email.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        return False
    tld = domain.rsplit(".", 1)[-1].lower()
    if tld in BAD_TLDS:
        return False
    if domain.startswith("2x."):
        return False
    return True


def is_role_email(email: str) -> bool:
    local = normalize_email(email).split("@", 1)[0]
    normalized = re.sub(r"[^a-z0-9]+", "", local.lower())
    if normalized in ROLE_LOCAL_PARTS:
        return True
    return any(normalized.startswith(part) and len(normalized) <= len(part) + 4 for part in ROLE_LOCAL_PARTS)


def split_person_and_ambiguous_emails(emails: list[str]) -> tuple[list[str], list[str]]:
    person: list[str] = []
    ambiguous: list[str] = []
    for email in sorted(set(normalize_email(e) for e in emails if is_probable_email(e))):
        if is_role_email(email):
            ambiguous.append(email)
        else:
            person.append(email)
    return person, ambiguous


def extract_visible_emails(text: str) -> list[str]:
    text = html.unescape(text or "")
    found = {normalize_email(match.group(1)) for match in EMAIL_RE.finditer(text)}
    return sorted(email for email in found if is_probable_email(email))


def extract_mailto_links(soup: BeautifulSoup) -> list[str]:
    emails: set[str] = set()
    for link in soup.select("a[href^=mailto]"):
        href = link.get("href") or ""
        parsed = urlparse(href)
        address = unquote(parsed.path or href.replace("mailto:", "", 1))
        address = address.split("?", 1)[0]
        for email in extract_visible_emails(address):
            emails.add(email)
    return sorted(emails)


def decode_cloudflare_email(encoded: str) -> str | None:
    encoded = (encoded or "").strip()
    if len(encoded) < 4 or len(encoded) % 2:
        return None
    try:
        key = int(encoded[:2], 16)
        decoded = bytes(int(encoded[i : i + 2], 16) ^ key for i in range(2, len(encoded), 2))
        return decoded.decode("utf-8", errors="ignore")
    except ValueError:
        return None


def extract_cloudflare_protected_emails(soup: BeautifulSoup) -> list[str]:
    emails: set[str] = set()
    for node in soup.select("[data-cfemail]"):
        decoded = decode_cloudflare_email(node.get("data-cfemail") or "")
        if decoded and decoded.count("@") == 1:
            emails.update(extract_visible_emails(decoded))
    for link in soup.find_all("a", href=True):
        href = link.get("href") or ""
        if "/cdn-cgi/l/email-protection#" not in href:
            continue
        encoded = href.rsplit("#", 1)[-1]
        decoded = decode_cloudflare_email(encoded)
        if decoded and decoded.count("@") == 1:
            emails.update(extract_visible_emails(decoded))
    return sorted(emails)


def extract_emails_from_html(html_text: str) -> list[str]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    emails = set(extract_mailto_links(soup))
    emails.update(extract_cloudflare_protected_emails(soup))
    emails.update(extract_visible_emails(soup.get_text(" ", strip=True)))
    return sorted(emails)
