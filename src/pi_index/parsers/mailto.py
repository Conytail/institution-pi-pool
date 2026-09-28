from __future__ import annotations

import base64
import binascii
import html
import re
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, Tag

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])")
BAD_TLDS = {"jpg", "jpeg", "png", "gif", "webp", "svg", "css", "js"}
ROLE_LOCAL_PARTS = {
    "admin",
    "admissions",
    "anaes",
    "contact",
    "enquiries",
    "enquiry",
    "english",
    "faculty",
    "finance",
    "help",
    "info",
    "office",
    "ortho",
    "radiology",
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


_PERSON_SCOPE_MARKERS = {
    "card",
    "contact",
    "faculty",
    "member",
    "people",
    "person",
    "profile",
    "researcher",
    "staff",
}


def _local_person_scopes(link: Tag) -> list[Tag]:
    """Return progressively wider containers that plausibly represent one person."""

    scopes: list[Tag] = []
    cell_scope: Tag | None = None
    for parent in link.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name in {"body", "html"}:
            break
        if parent.name == "td" and cell_scope is None:
            cell_scope = parent
        classes = " ".join(parent.get("class") or []).casefold()
        identity = str(parent.get("id") or "").casefold()
        if (
            parent.name in {"tr", "article", "li"}
            or any(marker in classes or marker in identity for marker in _PERSON_SCOPE_MARKERS)
        ):
            scopes.append(parent)
    if cell_scope is not None and cell_scope not in scopes:
        scopes.insert(0, cell_scope)
    return scopes


def _conflicts_with_local_email_evidence(link: Tag, email: str) -> bool:
    """Whether an empty mailto conflicts with stronger evidence in one person block."""

    normalized = normalize_email(email)
    for scope in _local_person_scopes(link):
        corroborated = set(extract_visible_emails(scope.get_text(" ", strip=True)))
        corroborated.update(extract_data_md5_emails(scope))
        if corroborated:
            return normalized not in corroborated
    return False


def extract_mailto_links(soup: BeautifulSoup) -> list[str]:
    emails: set[str] = set()
    for link in soup.find_all(
        "a",
        href=lambda value: bool(
            value and re.match(r"^mailt(?:o)?:", str(value), flags=re.I)
        ),
    ):
        href = link.get("href") or ""
        parsed = urlparse(href)
        address = unquote(
            parsed.path or re.sub(r"^mailt(?:o)?:", "", href, count=1, flags=re.I)
        )
        address = address.split("?", 1)[0]
        extracted = extract_visible_emails(address)
        visible_anchor_emails = set(
            extract_visible_emails(link.get_text(" ", strip=True))
        )
        if visible_anchor_emails:
            # A small number of legacy HKU pages publish a stale ``mailto``
            # href while rendering the current person's address as link text.
            # The visible value is the person-local evidence a human sees; a
            # conflicting href must not silently attach somebody else's email.
            emails.update(visible_anchor_emails)
            extracted = [email for email in extracted if email in visible_anchor_emails]
        is_empty_anchor = (
            not link.get_text(" ", strip=True)
            and link.find(True) is None
            and not any(link.get(attribute) for attribute in ("aria-label", "title"))
        )
        # Empty anchors are often legitimate CSS icon links.  Reject one only
        # when a local person card/row contains a different visible or Pure
        # data-md5 address, which is the stale-neighbour pattern observed at
        # HKU Psychology.
        if is_empty_anchor and any(
            _conflicts_with_local_email_evidence(link, email)
            for email in extracted
        ):
            continue
        for email in extracted:
            emails.add(email)
    return sorted(emails)


def extract_conflicting_mailto_emails(soup: BeautifulSoup) -> list[str]:
    """Return stale mailto targets contradicted by a visible email label.

    These addresses are useful as provenance/ambiguity evidence for adapters
    that retain it, but must not be promoted to the person's primary email.
    """

    conflicting: set[str] = set()
    for link in soup.find_all(
        "a",
        href=lambda value: bool(
            value and re.match(r"^mailt(?:o)?:", str(value), flags=re.I)
        ),
    ):
        visible = set(extract_visible_emails(link.get_text(" ", strip=True)))
        if not visible:
            continue
        href = link.get("href") or ""
        parsed = urlparse(href)
        address = unquote(
            parsed.path or re.sub(r"^mailt(?:o)?:", "", href, count=1, flags=re.I)
        )
        address = address.split("?", 1)[0]
        conflicting.update(set(extract_visible_emails(address)) - visible)
    return sorted(conflicting)


def extract_data_md5_emails(soup: BeautifulSoup) -> list[str]:
    """Decode Elsevier Pure's misleadingly named Base64 email attribute.

    Pure profile pages, including CityU Scholars, place ``mailto:...`` in a
    Base64 encoded ``data-md5`` attribute.  This belongs in the common email
    extractor because both the Pure-specific and generic profile adapters can
    encounter the same markup.
    """

    emails: set[str] = set()
    for node in soup.select("[data-md5]"):
        encoded = (node.get("data-md5") or "").strip()
        if not encoded:
            continue
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if decoded.lower().startswith("mailto:"):
            decoded = decoded[7:]
        emails.update(extract_visible_emails(decoded))
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
    emails.update(extract_data_md5_emails(soup))
    emails.update(extract_cloudflare_protected_emails(soup))
    emails.update(extract_visible_emails(soup.get_text(" ", strip=True)))
    return sorted(emails)
