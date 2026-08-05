# nexus/core/keys.py
"""Matching third-party JSON key names by meaning rather than by substring.

Both places that read Apify actor output — the phone extractor and the personalization parser —
have to answer "does this key name hold the thing I want?" against JSON nobody controls. The
obvious implementation is a substring regex, and it is quietly wrong:

    _EXCLUDE = re.compile(r"...|date|...")     # meant to skip `lastPostDate`
    "updates"                                  # ...also matches. up-DATE-s

That cost a real bug: `updates`, one of the most common key names for a LinkedIn activity feed,
was silently dropped because it contains "date". The same class of collision is waiting in every
substring list — "state" inside "estate", "id" inside "video", "code" inside "postcode".

Splitting the key into its **segments** first makes the match mean what it looks like it means.
`lastPostDate` -> {last, post, date} excludes on `date`; `updates` -> {updates} does not.
"""
from __future__ import annotations

import re

# camelCase, snake_case, kebab-case and digit boundaries all delimit a segment.
_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])")


def key_segments(key: str) -> set[str]:
    """The lowercase word-parts of a JSON key.

    ``first_mobile_number`` -> ``{first, mobile, number}``
    ``lastPostDate``        -> ``{last, post, date}``
    ``phone-2``             -> ``{phone, 2}``
    """
    if not key:
        return set()
    return {part.lower() for part in _SPLIT.split(key) if part}


def _singular(segment: str) -> set[str]:
    """Candidate singular forms of one segment.

    Only the two rules that actually occur in JSON key names. `activities` -> `activity` needs the
    -ies rule specifically: stripping a trailing "s" yields `activitie`, which matches nothing, so
    a key named ``activities`` — one of the most common names for a LinkedIn feed — would be
    skipped while ``activity`` worked. That is the same silent-miss failure as the phone actor,
    one layer down.
    """
    forms = set()
    if len(segment) > 4 and segment.endswith("ies"):
        forms.add(segment[:-3] + "y")
    if len(segment) > 3 and segment.endswith("s") and not segment.endswith("ss"):
        forms.add(segment[:-1])
    return forms


def key_matches(key: str, *, wanted: frozenset[str], unwanted: frozenset[str]) -> bool:
    """Whether ``key`` names something in ``wanted`` and nothing in ``unwanted``.

    Plural segments are singularised too, so a list named ``posts`` or ``activities`` matches the
    singular concept without both spellings having to be listed. ``unwanted`` is checked first: a
    key that names both (``postCount``) is excluded.
    """
    segments = key_segments(key)
    all_forms = set(segments)
    for segment in segments:
        all_forms |= _singular(segment)
    if all_forms & unwanted:
        return False
    return bool(all_forms & wanted)
