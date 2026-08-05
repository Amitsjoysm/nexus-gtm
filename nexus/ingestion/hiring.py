# nexus/ingestion/hiring.py
"""What KIND of hiring this is — surge, a new function, or a seniority shift.

M17. "Acme has 12 open roles" tells a rep almost nothing: every growing company has open roles.
What is actionable is the **change**:

* ``surge`` — headcount plans stepped up sharply. They are scaling.
* ``new_function`` — they are hiring into a department they were not hiring into at all. A new
  function means new budget and, usually, a new buyer.
* ``seniority_shift`` — they are recruiting leadership. A new VP arrives with a mandate and
  rebuilds their stack, which is the narrowest window a seller ever gets.

**These are subtypes of ``hiring``, not new signal kinds** (decision, 2026-08-05). Every existing
`INTENT_WEIGHTS["hiring"]`, play condition and alert rule keeps working untouched, and anything
that wants the detail opts into it. Promoting a subtype to a first-class kind later is easy;
demoting one is a data migration, so the reversible direction is the one to start in.

**All three are deltas, so none can be computed from a single crawl.** The comparison is against
the previous month's snapshot, cached on ``Account.custom_fields['ats_hiring']`` — the same place
the ATS board token already lives. The first hiring signal for an account therefore has **no**
subtype, which is honest: we have nothing to compare against and inventing one would be a guess.

The thresholds below are deliberately blunt. A subtype that fires on noise is worse than no
subtype: it puts a specific claim ("they're building a new team") in front of a rep who will repeat
it to a prospect, and being confidently wrong in a first sentence loses the conversation. Every
threshold requires both a *relative* and an *absolute* change, so a company going from 1 to 2 open
roles never trips anything.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("nexus.ingestion.hiring")

# Stored on the signal; `None` means "we could not tell", not "nothing happened".
SURGE = "surge"
NEW_FUNCTION = "new_function"
SENIORITY_SHIFT = "seniority_shift"
HIRING_SUBTYPES = (SURGE, NEW_FUNCTION, SENIORITY_SHIFT)

# Where the previous snapshot lives on the account.
SNAPSHOT_FIELD = "ats_hiring"

# Seniority read from the requisition title. Ordered most senior first — the first match wins, so
# "VP of Engineering" is exec-adjacent leadership rather than an engineer.
_SENIORITY_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    # `president` needs the lookbehind: "Vice President" CONTAINS "President", so a plain
    # alternation graded every VP as an exec. Same substring-collision class as `updates`
    # containing "date" (nexus/core/keys.py) — a title match that looks obviously correct and
    # silently mis-grades one of the commonest senior titles there is.
    ("exec", re.compile(
        r"\b(chief|c[etofmr]o|founder|partner)\b|(?<!vice )(?<!vice-)\bpresident\b", re.I)),
    ("vp", re.compile(r"\b(vp|svp|evp|vice[\s-]?president)\b", re.I)),
    ("director", re.compile(r"\b(director|head of|head,)\b", re.I)),
    ("manager", re.compile(r"\b(manager|lead|principal|staff)\b", re.I)),
)
# Levels that count as "leadership" for the seniority-shift test.
_SENIOR_LEVELS = frozenset({"exec", "vp", "director"})

# --- thresholds. Both halves of each pair must hold. ---
# A surge is a step change, not growth. 1.5x catches a genuine ramp; +5 stops a 2->3 move counting.
_SURGE_RATIO, _SURGE_ABSOLUTE = 1.5, 5
# One req in a new department is a backfill or an experiment. Two is an intent to build.
_NEW_FUNCTION_MIN = 2
# A 15-point swing in the leadership share, and at least two actual leadership roles — one VP
# opening is a replacement far more often than it is a reorganisation.
_SENIORITY_POINT_SHIFT, _SENIORITY_MIN_ROLES = 15.0, 2
# Below this, percentages are meaningless. A company with 3 reqs has no "mix".
_MIN_BASELINE = 3


def seniority_of(title: str) -> str:
    """Coarse level for one requisition title. Unmatched titles are individual contributors."""
    text = (title or "").strip()
    for level, pattern in _SENIORITY_PATTERNS:
        if pattern.search(text):
            return level
    return "ic"


def snapshot(postings) -> dict:
    """A comparable summary of this month's open requisitions.

    Counts only, never titles or URLs: this is cached on the account indefinitely, and storing a
    company's full requisition list there would be a second copy of data that already lives in the
    signal, ageing independently of it.
    """
    departments: dict[str, int] = {}
    seniority: dict[str, int] = {}
    for posting in postings:
        dept = (getattr(posting, "department", "") or "").strip().lower()
        if dept:
            departments[dept] = departments.get(dept, 0) + 1
        level = seniority_of(getattr(posting, "title", "") or "")
        seniority[level] = seniority.get(level, 0) + 1
    return {
        "count": len(postings),
        "departments": departments,
        "seniority": seniority,
    }


def _senior_share(snap: dict) -> float:
    total = int(snap.get("count") or 0)
    if total <= 0:
        return 0.0
    seniority = snap.get("seniority") or {}
    senior = sum(int(n) for level, n in seniority.items() if level in _SENIOR_LEVELS)
    return 100.0 * senior / total


def _senior_count(snap: dict) -> int:
    seniority = snap.get("seniority") or {}
    return sum(int(n) for level, n in seniority.items() if level in _SENIOR_LEVELS)


def classify(current: dict, previous: dict | None) -> tuple[str | None, str]:
    """The subtype for this month, and a human sentence explaining it.

    Returns ``(None, "")`` when nothing stands out or there is no baseline to compare against.

    Priority when several apply — most specific commercial implication first:

    1. ``new_function``   — a department appearing from nothing names a new initiative, and names
       the team a seller should be talking to.
    2. ``seniority_shift`` — a leader arriving means a mandate and a rebuild, but says nothing about
       which area until they start.
    3. ``surge``          — real, and the least specific: everything is growing.

    Never raises. A classification failure yields no subtype, which degrades to exactly the
    pre-M17 behaviour.
    """
    try:
        if not previous or int(previous.get("count") or 0) < _MIN_BASELINE:
            # No usable baseline. Silence is the honest answer.
            return None, ""

        now_count = int(current.get("count") or 0)
        was_count = int(previous.get("count") or 0)
        now_depts = {d: int(n) for d, n in (current.get("departments") or {}).items()}
        was_depts = {d: int(n) for d, n in (previous.get("departments") or {}).items()}

        # 1. New function.
        fresh = {
            dept: n for dept, n in now_depts.items()
            if n >= _NEW_FUNCTION_MIN and dept not in was_depts
        }
        if fresh and was_depts:
            top = max(fresh.items(), key=lambda kv: kv[1])
            return NEW_FUNCTION, (
                f"first {top[0].title()} roles — {top[1]} open, none last month"
            )

        # 2. Seniority shift.
        senior_now, senior_was = _senior_share(current), _senior_share(previous)
        if (senior_now - senior_was) >= _SENIORITY_POINT_SHIFT \
                and _senior_count(current) >= _SENIORITY_MIN_ROLES:
            return SENIORITY_SHIFT, (
                f"leadership hiring up — {_senior_count(current)} senior roles, "
                f"{senior_now:.0f}% of openings vs {senior_was:.0f}% last month"
            )

        # 3. Surge.
        if was_count > 0 and now_count >= was_count * _SURGE_RATIO \
                and (now_count - was_count) >= _SURGE_ABSOLUTE:
            return SURGE, f"open roles up from {was_count} to {now_count} in a month"

        return None, ""
    except Exception:
        logger.warning("hiring subtype classification failed", exc_info=True)
        return None, ""


def read_snapshot(account) -> dict | None:
    value = (getattr(account, "custom_fields", None) or {}).get(SNAPSHOT_FIELD)
    return value if isinstance(value, dict) else None


def write_snapshot(account, snap: dict) -> None:
    """Cache this month's snapshot for next month's comparison.

    Replaces rather than accumulates: the comparison is always month-over-month, and a growing
    history on `custom_fields` would be a table pretending to be a column.
    """
    fields = dict(getattr(account, "custom_fields", None) or {})
    fields[SNAPSHOT_FIELD] = snap
    account.custom_fields = fields
