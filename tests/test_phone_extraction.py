# tests/test_phone_extraction.py
"""What may become a contact's phone number, and what may not.

Two defects this pins, both of which shipped:

**Junk was stored as a phone number.** `extract_phone` returned the first non-empty string it
found under any phone-ish key. A scraper that writes "Premium feature" or "N/A" into that field
therefore produced a *successful* lookup, which was then written into the SHARED person record and
suppressed re-lookup for every tenant until the TTL expired. A miss has to look like a miss.

**A stranger's number could be attached to a contact.** The actor is called with a LIST of profile
URLs and returns a dataset; taking the first row's phone means any extra row — a partial match, a
suggested profile, a stale row from a batched run — supplies the number. That is the same
wrong-attribution class as the six bugs in `nexus/companies/`, and worse: a rep phones a real
person believing they are someone else.
"""
from __future__ import annotations

import pytest

from nexus.contacts.phone import looks_like_phone
from nexus.people.enrich import extract_phone

URL = "https://www.linkedin.com/in/derek-hall"
OTHER = "https://www.linkedin.com/in/someone-else"


# ---- the shape gate --------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "+1 (415) 555-2671", "+14155552671", "415-555-2671", "00 44 20 7946 0958",
    "+91-98765-43210", "020 7946 0958", "+1 415 555 2671 x123", "(415) 555.2671",
    "+61 2 9374 4000", "+353 1 234 5678",
])
def test_real_numbers_are_accepted(value):
    assert looks_like_phone(value), value


@pytest.mark.parametrize("value", [
    "", "   ", None,
    # Sentinels a scraper writes when it has nothing. Every one of these was previously stored as
    # a phone number and cached against the shared person record.
    "N/A", "n/a", "NA", "none", "None", "null", "-", "--", "unknown", "Not available",
    "not found", "no phone", "unavailable", "hidden", "private", "redacted",
    "Premium", "Premium feature", "upgrade", "locked", "restricted", "undefined", "false",
    # Prose that happens to sit in a phone column.
    "call the main office", "see LinkedIn", "email only",
    # Too short to route, or placeholder digits.
    "0", "00", "123", "12345", "0000000000", "1111111111",
    # Too long for E.164 (max 15 digits).
    "+1234567890123456789",
    # Vanity numbers are not dialable as E.164.
    "1-800-FLOWERS",
])
def test_junk_is_rejected(value):
    assert not looks_like_phone(value), value


def test_a_rejected_value_never_reaches_extraction():
    assert extract_phone([{"phone": "Premium feature"}]) == ""
    assert extract_phone([{"phone": "N/A"}]) == ""
    assert extract_phone([{"phone": "0000000000"}]) == ""


# ---- shape tolerance (actor output is not a contract) ----------------------------------------

@pytest.mark.parametrize("item", [
    {"phone": "+14155552671"},
    {"phone_number": "+14155552671"},
    {"phoneNumber": "+14155552671"},
    {"mobile": "+14155552671"},
    {"telephone": "+14155552671"},
    {"phoneNumbers": ["+14155552671"]},
    {"phones": [{"phone": "+14155552671"}]},
    {"contact_numbers": ["+14155552671"]},
    {"contact": {"phone": "+14155552671"}},
])
def test_every_known_output_shape_is_read(item):
    """Reading one hard-coded key would make an upstream rename look like 'no phone number'."""
    assert extract_phone([item]) == "+14155552671"


def test_a_junk_first_entry_does_not_hide_a_real_one():
    """The list is scanned for something usable, not truncated at the first element."""
    assert extract_phone([{"phoneNumbers": ["N/A", "+14155552671"]}]) == "+14155552671"


def test_a_junk_key_does_not_hide_a_real_one_on_the_same_row():
    assert extract_phone([{"phone": "not available", "mobile": "+14155552671"}]) == "+14155552671"


# ---- identity: the row must be about the person we asked for ---------------------------------

def test_a_row_for_a_different_profile_never_supplies_the_number():
    """THE bug. Without this, a rep phones a stranger with someone else's context."""
    items = [
        {"linkedin_url": OTHER, "phone": "+14155550000"},
        {"linkedin_url": URL, "phone": "+14155552671"},
    ]
    assert extract_phone(items, expect_linkedin_url=URL) == "+14155552671"


def test_only_a_foreign_row_yields_nothing_rather_than_a_wrong_number():
    items = [{"linkedin_url": OTHER, "phone": "+14155550000"}]
    assert extract_phone(items, expect_linkedin_url=URL) == ""


def test_url_spelling_differences_still_match():
    """The same profile arrives as .../in/x, www..., uk..., and with a trailing slash."""
    for spelling in (
        "linkedin.com/in/derek-hall",
        "http://uk.linkedin.com/in/derek-hall/",
        "https://www.linkedin.com/in/derek-hall?trk=abc",
    ):
        items = [{"profileUrl": spelling, "phone": "+14155552671"}]
        assert extract_phone(items, expect_linkedin_url=URL) == "+14155552671", spelling


@pytest.mark.parametrize("key", [
    "linkedin_url", "linkedinUrl", "profile_url", "profileUrl", "linkedin", "url", "inputUrl",
])
def test_the_profile_is_recognised_under_any_key_spelling(key):
    assert extract_phone([{key: URL, "phone": "+14155552671"}], expect_linkedin_url=URL) \
        == "+14155552671"


def test_a_row_naming_no_profile_is_used_as_a_fallback():
    """A single-result actor that does not echo the input back is the common, benign case."""
    assert extract_phone([{"phone": "+14155552671"}], expect_linkedin_url=URL) == "+14155552671"


def test_a_foreign_row_is_not_a_fallback_even_when_it_is_the_only_one_with_a_number():
    """'Some number is better than none' is exactly the reasoning that produces a wrong call."""
    items = [
        {"linkedin_url": OTHER, "phone": "+14155550000"},
        {"name": "no profile here"},
    ]
    assert extract_phone(items, expect_linkedin_url=URL) == ""


def test_without_an_expected_profile_the_old_permissive_behaviour_stands():
    """Callers that genuinely have no profile to match on (a name-only actor) must still work."""
    assert extract_phone([{"phone": "+14155552671"}]) == "+14155552671"


def test_empty_and_malformed_input():
    assert extract_phone([]) == ""
    assert extract_phone([{"name": "Derek"}]) == ""
    assert extract_phone([{"phone": ""}]) == ""
    assert extract_phone(["not a dict", None, 42]) == ""  # type: ignore[list-item]
