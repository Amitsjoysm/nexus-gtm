"""When did it happen? Read a date the way our sources actually write one.

Every signal used to be dated the moment we collected it, because nothing read a source's own date.
RSS `pubDate`, SEC `filed_at`, a Hacker News `created_at`, a search provider's "3 days ago" and a
news CMS's `/2025/03/04/` path each say when the event happened, and the day filter compared
against collection time instead. Measured on the local database before this existed: 1,099 of
1,112 signals dated within ten minutes of collection.

One reader for all of them, standard library only. **Anything it cannot read is ``None``, never a
guess**: an unknown date is recorded as "found on", which is honest, while a wrong date silently
moves a signal into or out of every window.

Lives in ``core`` rather than ``ingestion`` because the search engines fill ``SearchHit`` dates
too, and ``integrations`` must not depend on ``ingestion``.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

#: Before this, a parsed date is an unset epoch or a typo, not when a company raised money.
FLOOR = datetime(1995, 1, 1, tzinfo=timezone.utc)

_RELATIVE = re.compile(
    r"^\s*(?:(an?|\d+)\s+(minute|hour|day|week|month|year)s?\s+ago|(yesterday|today))\s*$",
    re.IGNORECASE,
)
_UNIT = {
    "minute": timedelta(minutes=1), "hour": timedelta(hours=1), "day": timedelta(days=1),
    "week": timedelta(weeks=1), "month": timedelta(days=30), "year": timedelta(days=365),
}
# Human forms the SERP providers use: "Mar 4, 2025", "March 4, 2025", "4 Mar 2025".
_HUMAN = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y")

# News CMSs put the date in the path. Year-alone is deliberately absent: it would date everything
# to 1 January, which is a guess, not a reading.
_URL_YMD = re.compile(r"/((?:19|20)\d{2})/([01]?\d)/([0-3]?\d)(?:/|$|[-_])")
_URL_YM = re.compile(r"/((?:19|20)\d{2})/([01]?\d)(?:/|$)")
_URL_ISO = re.compile(r"(?<!\d)((?:19|20)\d{2})-([01]\d)-([0-3]\d)(?!\d)")


def _plausible(dt: datetime | None, now: datetime) -> datetime | None:
    """Clamp a future date to now; refuse anything before ``FLOOR``."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt < FLOOR:
        return None
    # A feed with a wrong timezone, or a scheduled post, must not float above every real signal.
    return min(dt, now)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def parse_when(value, *, now: datetime | None = None) -> datetime | None:
    """A timezone-aware UTC datetime, or ``None`` when the value says nothing we can trust."""
    now = _now(now)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return _plausible(value, now)
    if not isinstance(value, str):
        return None  # a bare number is an id as often as it is an epoch
    text = value.strip()
    if not text:
        return None

    rel = _RELATIVE.match(text)
    if rel:
        if rel.group(3):
            return _plausible(now - timedelta(days=0 if rel.group(3).lower() == "today" else 1), now)
        count = 1 if rel.group(1).lower() in ("a", "an") else int(rel.group(1))
        return _plausible(now - _UNIT[rel.group(2).lower()] * count, now)

    iso = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        return _plausible(datetime.fromisoformat(iso), now)
    except ValueError:
        pass
    try:
        return _plausible(parsedate_to_datetime(text), now)
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in _HUMAN:
        try:
            return _plausible(datetime.strptime(text, fmt), now)
        except ValueError:
            continue
    return None


def _ymd(year: str, month: str, day: str, now: datetime) -> datetime | None:
    try:
        return _plausible(datetime(int(year), int(month), int(day), tzinfo=timezone.utc), now)
    except ValueError:
        return None


def date_from_url(url: str | None, *, now: datetime | None = None) -> datetime | None:
    """The publication date a news URL carries in its path, or ``None``."""
    now = _now(now)
    if not url:
        return None
    path = url.split("?", 1)[0]
    m = _URL_YMD.search(path)
    if m:
        return _ymd(*m.groups(), now)
    m = _URL_ISO.search(path)
    if m:
        return _ymd(*m.groups(), now)
    m = _URL_YM.search(path)
    if m:
        return _ymd(m.group(1), m.group(2), "1", now)
    return None
