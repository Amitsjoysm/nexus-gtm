"""Deterministic job-level and keyword title matching.

Exact-title matching is why a tester's contact search returned nothing across three campaigns. They
asked for ``Facilities Director``; the index held ``Director of Facilities``, ``Head of Facilities``
and ``Facilities Head``. A person's title is written five ways by five companies, and the one a rep
types is never the one the data holds.

**No LLM here, deliberately.** This decides which humans a rep contacts. "The model thought so" is
not an answer to "why did this person match?", and the scoring path in this codebase does not make
network calls — it has to work when the provider is down, which is precisely the condition that
produced the original bug report.

Three independent gates, and **all of them default to open**:

* ``job_levels``             — seniority, normalised from the title
* ``title_keywords``         — function words that must appear somewhere in the title
* ``exclude_title_keywords`` — words that disqualify regardless of the other two

An empty spec matches everything, which is what makes this additive: every existing workspace has
none of these keys and keeps exactly the results it has today.
"""
from __future__ import annotations

import re

# Ordered MOST SENIOR FIRST, and the order is load-bearing: a title containing both "VP" and
# "Manager" ("VP, Engineering Manager") is a VP. Ascending order would call it a manager and drop it
# from a VP-and-above search.
_LEVEL_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("c_level", (
        r"\bc[teofmirsp]o\b",            # cto ceo cfo coo cmo cio cro cso cpo
        r"\bchief\b",
        r"\bfounder\b", r"\bco founder\b", r"\bcofounder\b",
        r"\bowner\b",
        # NOT a bare `\bpresident\b`: "Senior Vice President" contains "president" as a whole word,
        # and c_level is tested first, so every VP in the estate was reading as C-level — which
        # silently breaks a "VP and above" search by putting VPs in the wrong bucket entirely.
        r"(?<!vice )\bpresident\b",
        r"\bmanaging director\b",        # in most markets this IS the top job, not a director
    )),
    ("vp", (
        r"\bvp\b", r"\bv p\b",
        r"\bvice president\b",
        r"\bsvp\b", r"\bevp\b", r"\bavp\b",
    )),
    ("head", (r"\bhead\b",)),
    ("director", (r"\bdirector\b", r"\bdir\b")),
    ("manager", (r"\bmanager\b", r"\bmgr\b", r"\blead\b", r"\bsupervisor\b")),
)

LEVELS: tuple[str, ...] = ("c_level", "vp", "head", "director", "manager", "ic")

# The phrase shapes a job title actually takes. Taken from the tester's own list, which is exactly
# the set a web index holds.
_PHRASE_FORMS = ("{level} of {kw}", "{kw} {level}", "{level}, {kw}", "{level} {kw}")

_LEVEL_WORDS = {
    "c_level": "Chief", "vp": "VP", "head": "Head", "director": "Director", "manager": "Manager",
}


def _normalise(title: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed.

    Punctuation becomes a SPACE rather than being deleted: ``Director,Facilities`` must not collapse
    to ``directorfacilities``, and the ``\\b`` word boundaries above need real separators to fire at
    all. That is also what makes ``Co-Founder`` reach the ``co founder`` pattern.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (title or "").lower())).strip()


def level_of(title: str) -> str:
    """Normalise a free-text title to one of :data:`LEVELS`. Unrecognised titles are ``ic``."""
    norm = _normalise(title)
    if not norm:
        return "ic"
    for level, patterns in _LEVEL_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, norm):
                return level
    return "ic"


def matches_title(title: str, spec: dict | None) -> bool:
    """Does ``title`` satisfy the ICP's level/keyword spec?

    An empty or absent spec returns True. That is the compatibility line: a workspace that has never
    set these keys keeps the contacts it sees today.
    """
    spec = spec or {}
    levels = [str(x).lower() for x in (spec.get("job_levels") or []) if x]
    keywords = [str(x) for x in (spec.get("title_keywords") or []) if x]
    excluded = [str(x) for x in (spec.get("exclude_title_keywords") or []) if x]

    if not levels and not keywords and not excluded:
        return True

    norm = _normalise(title)

    # Exclusions are checked FIRST and are absolute. "Assistant Director of Facilities" satisfies
    # both other gates and is not the person the rep meant.
    if any(_normalise(x) in norm for x in excluded if _normalise(x)):
        return False
    if levels and level_of(title) not in levels:
        return False
    if keywords and not any(_normalise(k) in norm for k in keywords if _normalise(k)):
        return False
    return True


def expand_titles(spec: dict | None, *, limit: int = 12) -> list[str]:
    """Turn a level/keyword spec into searchable title phrases.

    The contact-search provider queries a web index, which needs strings rather than a predicate.
    :func:`matches_title` then re-filters whatever comes back, so a phrase that over-matches costs a
    little recall noise and never a wrong contact — the right way round for this trade.
    """
    spec = spec or {}
    levels = [str(x).lower() for x in (spec.get("job_levels") or []) if x]
    keywords = [str(x).strip() for x in (spec.get("title_keywords") or []) if str(x).strip()]

    if not keywords:
        return [_LEVEL_WORDS[lv] for lv in levels if lv in _LEVEL_WORDS][:limit]

    out: list[str] = []
    seen: set[str] = set()
    # No levels named means "leadership of this function"; director is the most common rung a GTM
    # team means by that, and `matches_title` is not narrowed by this guess — only the query is.
    for keyword in keywords:
        for level in levels or ["director", "head"]:
            word = _LEVEL_WORDS.get(level)
            if not word:
                continue
            for form in _PHRASE_FORMS:
                phrase = form.format(level=word, kw=keyword).strip()
                key = phrase.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(phrase)
                if len(out) >= limit:
                    return out
    return out


def spec_from_icp(icp: dict | None) -> dict:
    """Pull just the title-matching keys out of an ICP, so callers share one shape.

    Returns the three keys unconditionally — an all-empty spec is the "match everything" case that
    every caller needs to be able to pass around without special-casing None.
    """
    icp = icp or {}
    return {
        "job_levels": list(icp.get("job_levels") or []),
        "title_keywords": list(icp.get("title_keywords") or []),
        "exclude_title_keywords": list(icp.get("exclude_title_keywords") or []),
    }
