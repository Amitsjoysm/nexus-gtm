"""Turn a reply's date phrase into a calendar date — in code, never in the model (D15, spec §6).

The classifier returns only the words the person wrote ("next June", "after Q3", "back on the
24th", "in two weeks"). This module interprets them against the moment the reply was RECEIVED, in
the CONTACT's timezone, because "tomorrow" in a reply read three days later still means the day
after it was written.

**Ambiguity is an answer.** Anything that resolves more than one way, or not at all, returns
``None``, and the caller files the reply as ``unclear`` so a person decides (D8). Guessing wrong
here means emailing someone before they asked, which is exactly the outcome "later" replies exist to
prevent. Concretely ambiguous:

* ``03/04`` — March 4th or 3rd April, depending on who wrote it.
* ``June`` written in June — this June's remaining weeks, or next year's.
* ``a few weeks`` — no number to add.
* two different dates in one phrase — "between June and July".

Period phrases land on the FIRST BUSINESS DAY of the period (spec §6). A specific day ("the 24th",
"September 30") is returned as written; the scheduler decides what to do when it falls on a weekend.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from nexus.engagement.timekeeping import roll_to_business_day, to_local

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12,
    "dec": 12,
}
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thurs": 3, "thur": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5,
    "sat": 5, "sunday": 6, "sun": 6,
}
_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "couple": 2, "a couple": 2, "a couple of": 2,
    "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}
_ORDINAL_QUARTERS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
                     "fourth": 4, "4th": 4, "last": 4}

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_ALT = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_NUMBER_ALT = r"\d{1,3}|" + "|".join(sorted(_NUMBERS, key=len, reverse=True))


@dataclass(frozen=True, slots=True)
class Resolution:
    date: date
    rule: str


class _Ambiguous(Exception):
    """A rule recognised the phrase and found more than one reading."""


def _clean(phrase: str) -> str:
    text = (phrase or "").lower()
    text = text.replace("’", "'").replace("'s", "").replace("'", "")
    text = re.sub(r"[,;!?()\"]", " ", text)
    text = re.sub(r"\.(?!\d)", " ", text)
    return " ".join(text.split())


def _first_business_day(year: int, month: int) -> date:
    return roll_to_business_day(date(year, month, 1))


def _add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _number(token: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _NUMBERS.get(token)


def _year_for_month(month: int, today: date, *, year: int | None, explicit_next: bool) -> int:
    if year is not None:
        return year
    if month > today.month:
        return today.year
    if month < today.month or explicit_next:
        return today.year + 1
    raise _Ambiguous("the named month is the current month")


def _check_weekday(day: date, weekday_word: str | None) -> None:
    if weekday_word and _WEEKDAYS[weekday_word] != day.weekday():
        raise _Ambiguous("the weekday named does not match the date")


# Each rule: (name, compiled pattern, handler(match, today) -> date). Ordered most specific first;
# a rule's match is removed from the text before the next rule runs, so "Monday 29th September"
# is read once as a date rather than again as "next Monday".
def _iso(m: dict, today: date) -> date:
    return date(int(m["y"]), int(m["m"]), int(m["d"]))


def _numeric(m: dict, today: date) -> date:
    a, b = int(m["a"]), int(m["b"])
    if a <= 12 and b <= 12 and a != b:
        raise _Ambiguous("day and month order cannot be told apart")
    day, month = (a, b) if a > 12 else (b, a)
    year = int(m["y"]) if m["y"] else None
    if year is not None and year < 100:
        year += 2000
    target_year = year if year is not None else today.year
    result = date(target_year, month, day)
    if year is None and result < today:
        result = date(today.year + 1, month, day)
    return result


def _day_month(m: dict, today: date) -> date:
    month = _MONTHS[m["month"]]
    day = int(m["day"])
    year = int(m["year"]) if m["year"] else None
    result = date(year or today.year, month, day)
    if year is None and result < today:
        result = date(today.year + 1, month, day)
    _check_weekday(result, m["wd"])
    return result


def _tomorrow(m: dict, today: date) -> date:
    return today + timedelta(days=2 if m["after"] else 1)


def _offset(m: dict, today: date) -> date:
    count = _number(m["n"])
    if count is None or count <= 0:
        raise _Ambiguous("no usable number")
    unit = m["unit"]
    if unit.startswith("day"):
        result = today + timedelta(days=count)
    elif unit.startswith("week"):
        result = today + timedelta(weeks=count)
    else:
        result = _add_months(today, count)
    return roll_to_business_day(result)


def _vague_offset(m: dict, today: date) -> date:
    raise _Ambiguous("'a few' / 'several' has no number")


def _next_period(m: dict, today: date) -> date:
    period = m["period"]
    if period == "week":
        return today + timedelta(days=7 - today.weekday())
    if period == "month":
        nxt = _add_months(today.replace(day=1), 1)
        return _first_business_day(nxt.year, nxt.month)
    if period == "quarter":
        quarter_start = (today.month - 1) // 3 * 3 + 1
        nxt = _add_months(date(today.year, quarter_start, 1), 3)
        return _first_business_day(nxt.year, nxt.month)
    return _first_business_day(today.year + 1, 1)


def _end_of_period(m: dict, today: date) -> date:
    period = m["period"]
    if period == "month":
        result = roll_to_business_day(date(today.year, today.month, 25))
    elif period == "quarter":
        last_month = ((today.month - 1) // 3 + 1) * 3
        result = roll_to_business_day(date(today.year, last_month, 15))
    else:
        result = roll_to_business_day(date(today.year, 12, 1))
    if result < today:
        raise _Ambiguous("that period's end has already passed")
    return result


def _quarter_number(m: dict) -> int:
    word = m["q"] or m["qword"]
    return int(word) if word and word.isdigit() else _ORDINAL_QUARTERS[word]


def _quarter(m: dict, today: date) -> date:
    q = _quarter_number(m)
    after = bool(m["after"])
    year = int(m["year"]) if m["year"] else None
    if after:
        q += 1
        if q == 5:
            q, year = 1, (year + 1 if year else None)
            if year is None:
                return _first_business_day(today.year + 1, 1)
    current_q = (today.month - 1) // 3 + 1
    if year is None:
        if q > current_q:
            year = today.year
        elif q < current_q or m["next"]:
            year = today.year + 1
        elif after:
            year = today.year
        else:
            raise _Ambiguous("the named quarter is the current quarter")
    return _first_business_day(year, (q - 1) * 3 + 1)


def _half(m: dict, today: date) -> date:
    half = 1 if (m["h"] == "1" or m["hword"] == "first") else 2
    current = 1 if today.month <= 6 else 2
    if m["year"]:
        year = int(m["year"])
    elif half > current:
        year = today.year
    elif half < current or m["next"]:
        year = today.year + 1
    else:
        raise _Ambiguous("the named half is the current half")
    return _first_business_day(year, 1 if half == 1 else 7)


def _month(m: dict, today: date) -> date:
    word = m["month"]
    if word == "may" and not (m["qual"] or m["next"] or m["year"] or m["lead"]):
        raise _NotADate()
    month = _MONTHS[word]
    year = _year_for_month(
        month, today, year=int(m["year"]) if m["year"] else None, explicit_next=bool(m["next"])
    )
    qual = (m["qual"] or "").strip()
    day = 1
    if qual in ("mid", "middle of", "mid-"):
        day = 15
    elif qual in ("late", "end of", "the end of"):
        day = 22
    return roll_to_business_day(date(year, month, day))


def _year(m: dict, today: date) -> date:
    year = int(m["year"])
    if year <= today.year:
        raise _Ambiguous("a year that has already started")
    return _first_business_day(year, 1)


def _weekday(m: dict, today: date) -> date:
    target = _WEEKDAYS[m["wd"]]
    delta = (target - today.weekday()) % 7 or 7
    return today + timedelta(days=delta)


def _day_of_month(m: dict, today: date) -> date:
    day = int(m["day"] or m["day2"])
    weekday_word = m["wd"] or m["wd2"]
    if not 1 <= day <= 31:
        raise _Ambiguous("not a day of the month")
    year, month = today.year, today.month
    for _ in range(13):
        if day <= calendar.monthrange(year, month)[1]:
            candidate = date(year, month, day)
            if candidate >= today:
                _check_weekday(candidate, weekday_word)
                return candidate
        month += 1
        if month == 13:
            year, month = year + 1, 1
    raise _Ambiguous("no such day")


class _NotADate(Exception):
    """The words matched a pattern but are not a date here ("you may")."""


_RULES: tuple[tuple[str, re.Pattern, object], ...] = (
    ("iso_date", re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b"), _iso),
    ("numeric_date",
     re.compile(r"\b(?P<a>\d{1,2})[/.](?P<b>\d{1,2})(?:[/.](?P<y>\d{2,4}))?\b"), _numeric),
    ("day_month", re.compile(
        rf"\b(?:(?P<wd>{_WEEKDAY_ALT}) )?(?:the )?(?P<day>\d{{1,2}})(?:st|nd|rd|th)? (?:of )?"
        rf"(?P<month>{_MONTH_ALT})(?: (?P<year>\d{{4}}))?\b"), _day_month),
    ("day_month", re.compile(
        rf"\b(?:(?P<wd>{_WEEKDAY_ALT}) )?(?P<month>{_MONTH_ALT}) (?:the )?(?P<day>\d{{1,2}})"
        rf"(?:st|nd|rd|th)?(?: (?P<year>\d{{4}}))?\b"), _day_month),
    ("tomorrow", re.compile(r"\b(?P<after>(?:the )?day after )?tomorrow\b"), _tomorrow),
    ("vague_offset", re.compile(r"\b(?:a few|few|several|some) (?:days?|weeks?|months?)\b"),
     _vague_offset),
    ("offset", re.compile(
        rf"\b(?:in |after |within )?(?P<n>{_NUMBER_ALT}) (?:of )?(?P<unit>days?|weeks?|months?)"
        rf"(?: time| from now)?\b"), _offset),
    ("end_of_period", re.compile(
        r"\b(?:the )?end of (?:the |this )?(?P<period>month|quarter|year)\b"), _end_of_period),
    ("next_period", re.compile(
        r"\b(?:next|following|(?:start|beginning) of next|early next) (?P<period>week|month|quarter|year)\b"),
     _next_period),
    ("quarter", re.compile(
        r"\b(?P<after>after |post |once )?(?:the )?(?P<next>next )?(?:(?:q(?P<q>[1-4]))|"
        r"(?P<qword>first|second|third|fourth|1st|2nd|3rd|4th|last) quarter)"
        r"(?: (?:of )?(?P<year>\d{4}))?\b"), _quarter),
    ("half", re.compile(
        r"\b(?P<next>next )?(?:h(?P<h>[12])|(?P<hword>first|second) half(?: of the year)?)"
        r"(?: (?P<year>\d{4}))?\b"), _half),
    ("month", re.compile(
        rf"\b(?P<lead>in |until |after |by |around |from )?(?P<qual>early |beginning of |start of |"
        rf"mid-|mid |middle of |late |end of |the end of )?(?P<next>next )?"
        rf"(?P<month>{_MONTH_ALT})(?: (?P<year>\d{{4}}))?\b"), _month),
    ("year", re.compile(r"\b(?:in |during |until )(?P<year>20\d{2})\b"), _year),
    ("day_of_month", re.compile(
        rf"\b(?:(?P<wd>{_WEEKDAY_ALT}) )?(?:on )?the (?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b|"
        rf"\b(?:(?P<wd2>{_WEEKDAY_ALT}) )?(?P<day2>\d{{1,2}})(?:st|nd|rd|th)\b"), _day_of_month),
    ("weekday", re.compile(
        rf"\b(?:next |this |on |by |until )?(?P<wd>{_WEEKDAY_ALT})\b"), _weekday),
)


def resolve_date(phrase: str | None, received_at: datetime, zone: tzinfo) -> Resolution | None:
    """The date ``phrase`` names, read at ``received_at`` in ``zone``; ``None`` when unclear."""
    text = _clean(phrase or "")
    if not text:
        return None
    today = to_local(received_at, zone).date()
    found: list[Resolution] = []
    for name, pattern, handler in _RULES:
        while True:
            match = pattern.search(text)
            if match is None:
                break
            text = f"{text[:match.start()]} | {text[match.end():]}"
            try:
                found.append(Resolution(handler(match.groupdict(), today), name))
            except _NotADate:
                continue
            except (_Ambiguous, ValueError):
                return None
    dates = {r.date for r in found}
    if len(dates) != 1:
        return None
    result = found[0]
    if result.date < today:
        return None
    return result
