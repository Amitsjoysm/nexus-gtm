"""Partial masks (decision D7): enough to judge a row, never enough to use it unpaid.

The browser sees at most the first character of an address's local part and of its domain label,
the top-level domain, the last four digits of a phone of eight digits or more, and the first
character of a LinkedIn slug. A part too short to hide behind its first character shows nothing.
Everything else is the same placeholder (``•••``), so a mask carries no length either.
"""
from __future__ import annotations

import re

DOTS = "•••"
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_SLUG = re.compile(r"/(company|in|showcase|school)/([^/?#]+)", re.I)


def _lead(part: str) -> str:
    """The first character of a part long enough to hide behind it, else nothing."""
    return part[0].lower() if len(part) > 1 else ""


def _host(value: str) -> str:
    host = _SCHEME.sub("", (value or "").strip()).split("/", 1)[0].split("?", 1)[0]
    host = host.split("@")[-1].split(":", 1)[0].lower()
    return host[4:] if host.startswith("www.") else host


def _mask_host(host: str) -> str:
    labels = [label for label in host.split(".") if label]
    if not labels:
        return ""
    tld = f".{labels[-1]}" if len(labels) > 1 else ""
    return f"{_lead(labels[0])}{DOTS}{tld}"


def mask_email(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "@" not in value:
        return DOTS
    local, domain = value.rsplit("@", 1)
    return f"{_lead(local)}{DOTS}@{_mask_host(domain.lower())}"


def mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return ""
    if len(digits) >= 8:
        return f"{DOTS}-{DOTS}-{digits[-4:]}"
    return f"{DOTS}-{DOTS}-••••"


def mask_domain(value: str) -> str:
    return _mask_host(_host(value)) if (value or "").strip() else ""


def mask_linkedin(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    match = _SLUG.search(value)
    if not match:
        return f"linkedin.com/{DOTS}"
    kind, slug = match.group(1).lower(), match.group(2)
    return f"linkedin.com/{kind}/{_lead(slug)}{DOTS}"
