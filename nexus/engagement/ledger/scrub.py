"""Remove personal data from free text before it reaches the training store (spec §18.3, §16).

Three passes, in this order:

1. **Addresses and links.** Every email address and link — the ones the event's records know first,
   then anything that looks like one — becomes a placeholder. These go first because a company domain
   replaced earlier would split "priya@acme.io" and "www.acme.io/careers" into halves no pattern can
   recognise.
2. **Known people and companies.** Every name and company the event is about, taken from the records
   it references (contact, account, mailbox, the SDR), becomes a consistent placeholder for the whole
   example: "Jane" in the email and "Jane Buyer" in the signature are the same ``[PERSON_1]``, which
   keeps the text learnable.
3. **Phone numbers.** Eight to fifteen digits, whatever the punctuation.

Names the records do not know and no pattern can see (a colleague named in passing) are the known
limit; ``SCRUBBER_VERSION`` is stored on every row so an improved scrubber can re-scrub from the
archive.

Matching rules: full names and company names are matched case-insensitively; a single name part
("Mark", "Will") only with the capitalisation it was given, so ordinary words survive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SCRUBBER_VERSION = 1

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<![\w/])\+?\(?\d[\d\s().-]{6,}\d(?![\w/])")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_BOUNDARY = r"\b"


@dataclass(slots=True)
class Known:
    people: list[str] = field(default_factory=list)       # full names
    companies: list[str] = field(default_factory=list)    # names and domains
    emails: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _word(literal: str, flags: int) -> re.Pattern:
    return re.compile(_BOUNDARY + re.escape(literal) + _BOUNDARY, flags)


class Scrubber:
    """One per example, so placeholders are consistent across its texts."""

    def __init__(self, known: Known):
        self._placeholders: dict[tuple[str, str], str] = {}
        self._counts: dict[str, int] = {}

        literals: list[tuple[re.Pattern, str, str]] = []
        for email in known.emails:
            if email:
                literals.append((re.compile(re.escape(email), re.IGNORECASE), "EMAIL",
                                 email.lower()))
        for url in known.urls:
            if url:
                literals.append((re.compile(re.escape(url), re.IGNORECASE), "URL", url.lower()))
        # Known addresses and links first, so they number before strangers found by pattern.
        self._literals = sorted(literals, key=lambda r: -len(r[0].pattern))

        names: list[tuple[re.Pattern, str, str]] = []
        people: set[tuple[str, bool, str]] = set()
        for full in known.people:
            name = " ".join((full or "").split())
            if not name:
                continue
            identity = name.lower()
            people.add((name, True, identity))
            for part in name.split(" "):
                # A first or last name on its own shares the full name's placeholder, and matches
                # only with the capitalisation it was given, so "will" and "mark" survive as words.
                if len(part) >= 3 and part[0].isupper():
                    people.add((part, False, identity))
        for name, insensitive, identity in people:
            names.append((_word(name, re.IGNORECASE if insensitive else 0), "PERSON", identity))
        for company in {c for c in known.companies if c}:
            names.append((_word(company, re.IGNORECASE), "COMPANY", company.lower()))
        # Longest literal first, so "Jane Buyer" wins over "Jane" and a domain over its label.
        self._names = sorted(names, key=lambda r: -len(r[0].pattern))

    def _placeholder(self, kind: str, identity: str) -> str:
        slot = (kind, identity)
        if slot not in self._placeholders:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._placeholders[slot] = f"[{kind}_{self._counts[kind]}]"
        return self._placeholders[slot]

    def text(self, value: str | None) -> str:
        text = value or ""
        for pattern, kind, identity in self._literals:
            text = pattern.sub(lambda _m, k=kind, i=identity: self._placeholder(k, i), text)
        text = EMAIL_RE.sub(lambda m: self._placeholder("EMAIL", m.group(0).lower()), text)
        text = URL_RE.sub(lambda m: self._placeholder("URL", m.group(0).lower()), text)
        for pattern, kind, identity in self._names:
            text = pattern.sub(lambda _m, k=kind, i=identity: self._placeholder(k, i), text)

        def _phone(match: re.Match) -> str:
            digits = _digits(match.group(0))
            # An ISO date has eight digits too, and a resolved "call me on 2026-10-05" is a label.
            if _ISO_DATE.fullmatch(match.group(0)):
                return match.group(0)
            if len(digits) < 8 or len(digits) > 15:
                return match.group(0)
            return self._placeholder("PHONE", digits)

        return PHONE_RE.sub(_phone, text)

    def value(self, obj):
        """Scrub every string inside a JSON-like value."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {k: self.value(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.value(v) for v in obj]
        return obj
