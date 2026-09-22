"""Date phrases from real replies resolve to the date a person means, or to nothing (spec §6)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from nexus.engagement.dates import resolve_date

# Thursday 17 September 2026, 10:00 UTC.
RECEIVED = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
UTC = timezone.utc

RESOLVED = [
    # month names: first business day of the next occurrence
    ("try me in June", date(2027, 6, 1), "month"),
    ("next June", date(2027, 6, 1), "month"),
    ("reach out in October", date(2026, 10, 1), "month"),
    ("next September", date(2027, 9, 1), "month"),
    ("in May", date(2027, 5, 3), "month"),               # 1 May 2027 is a Saturday
    ("mid-November", date(2026, 11, 16), "month"),       # 15 Nov is a Sunday
    ("late January", date(2027, 1, 22), "month"),
    ("early December 2026", date(2026, 12, 1), "month"),
    # quarters, halves, years
    ("after Q3", date(2026, 10, 1), "quarter"),
    ("in Q1", date(2027, 1, 1), "quarter"),
    ("second quarter", date(2027, 4, 1), "quarter"),
    ("first half of 2027", date(2027, 1, 1), "half"),
    ("in 2027", date(2027, 1, 1), "year"),
    ("next quarter", date(2026, 10, 1), "next_period"),
    ("next month", date(2026, 10, 1), "next_period"),
    ("next year", date(2027, 1, 1), "next_period"),
    ("next week", date(2026, 9, 21), "next_period"),
    ("end of the month", date(2026, 9, 25), "end_of_period"),
    ("end of the year", date(2026, 12, 1), "end_of_period"),
    # offsets: from the received date, moved to a business day
    ("in two weeks", date(2026, 10, 1), "offset"),
    ("in 2 weeks' time", date(2026, 10, 1), "offset"),
    ("a couple of weeks", date(2026, 10, 1), "offset"),
    ("in 3 months", date(2026, 12, 17), "offset"),
    ("you may reach me in two weeks", date(2026, 10, 1), "offset"),
    ("tomorrow", date(2026, 9, 18), "tomorrow"),
    # specific days: returned as written
    ("back on the 24th", date(2026, 9, 24), "day_of_month"),
    ("back on the 5th", date(2026, 10, 5), "day_of_month"),
    ("returning Tuesday, 29th September", date(2026, 9, 29), "day_month"),
    ("out until October 3", date(2026, 10, 3), "day_month"),
    ("3 October", date(2026, 10, 3), "day_month"),
    ("2026-10-12", date(2026, 10, 12), "iso_date"),
    ("24/09", date(2026, 9, 24), "numeric_date"),
    ("back Monday", date(2026, 9, 21), "weekday"),
]

UNCLEAR = [
    "September",                              # the current month: this one or next year's?
    "Q3",                                     # the current quarter
    "in a few weeks",                         # no number
    "03/04",                                  # March 4th or 3rd April
    "returning Monday 29th September",        # 29 September 2026 is a Tuesday
    "between June and July",                  # two dates
    "H2 next year",                           # H2 read as the current half, then "next year"
    "end of the quarter",                     # already past the quarter's end window
    "not now",
    "",
]


@pytest.mark.parametrize("phrase,expected,rule", RESOLVED)
def test_a_phrase_resolves_to_the_date_it_names(phrase, expected, rule):
    result = resolve_date(phrase, RECEIVED, UTC)
    assert result is not None, f"{phrase!r} did not resolve"
    assert (result.date, result.rule) == (expected, rule)


@pytest.mark.parametrize("phrase", UNCLEAR)
def test_an_ambiguous_phrase_resolves_to_nothing(phrase):
    assert resolve_date(phrase, RECEIVED, UTC) is None


def test_tomorrow_is_read_in_the_contacts_timezone():
    # 23:30 on the 17th in UTC is already the 18th in Tokyo, so "tomorrow" there is the 19th.
    late = datetime(2026, 9, 17, 23, 30, tzinfo=timezone.utc)
    assert resolve_date("tomorrow", late, UTC).date == date(2026, 9, 18)
    assert resolve_date("tomorrow", late, ZoneInfo("Asia/Tokyo")).date == date(2026, 9, 19)


def test_a_date_already_past_is_not_a_reengagement_date():
    assert resolve_date("2026-09-01", RECEIVED, UTC) is None
