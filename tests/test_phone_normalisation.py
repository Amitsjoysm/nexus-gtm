# tests/test_phone_normalisation.py
"""Canonical phone storage.

Phone numbers arrive from a CRM import, an Apify actor, a manual edit and an enrichment provider,
all disagreeing on format. Stored raw, the same person is four contacts and a dialler is handed a
string it cannot call. E.164 is the only representation where string equality means "same number".
"""
from __future__ import annotations

from nexus.contacts.phone import normalise_phone, region_for, resolve_region


# ---- the region cascade -------------------------------------------------------------------------

def test_the_person_country_wins_over_the_account_country():
    """A person in London working for a US-headquartered account has a UK number."""
    got = normalise_phone("020 7946 0958", country="United Kingdom", account_country="US")
    assert got.e164 == "+442079460958"
    assert got.region == "GB"


def test_the_account_country_is_used_when_the_person_has_none():
    got = normalise_phone("020 7946 0958", country=None, account_country="GB")
    assert got.e164 == "+442079460958"


def test_us_is_the_fallback_when_neither_is_known():
    got = normalise_phone("(415) 555-2671")
    assert got.e164 == "+14155552671"
    assert got.region == "US"


def test_an_explicit_region_overrides_the_cascade():
    got = normalise_phone("98765 43210", region="IN", country="US", account_country="US")
    assert got.e164 == "+919876543210"


def test_country_names_and_iso_codes_both_resolve():
    assert region_for("india") == "IN"
    assert region_for("IN") == "IN"
    assert region_for("United Kingdom") == "GB"
    assert region_for("Nowhereistan") == "", "an unknown country must not resolve to a guess"


def test_the_cascade_skips_blanks():
    assert resolve_region(None, "", "  ", "India") == "IN"
    assert resolve_region(None, "") == "US"


# ---- formats that actually turn up ---------------------------------------------------------------

def test_an_already_international_number_keeps_its_own_country_code():
    """The cascade must not overwrite a country the number itself declares."""
    got = normalise_phone("+44 20 7946 0958", country="US")
    assert got.e164 == "+442079460958"


def test_the_double_zero_international_prefix_is_understood():
    assert normalise_phone("00 44 20 7946 0958", country="US").e164 == "+442079460958"


def test_the_nanp_011_prefix_is_understood():
    assert normalise_phone("011 44 20 7946 0958", country="US").e164 == "+442079460958"


def test_punctuation_and_spacing_are_irrelevant():
    forms = ["(415) 555-2671", "415.555.2671", "415 555 2671", "4155552671", "+1 415-555-2671"]
    assert {normalise_phone(f).e164 for f in forms} == {"+14155552671"}


def test_an_extension_is_not_part_of_the_number():
    """E.164 has no room for one, and a dialler must not try to call it."""
    for form in ("415-555-2671 x123", "415-555-2671 ext. 99", "(415) 555-2671 extension 4"):
        assert normalise_phone(form).e164 == "+14155552671", form


def test_a_national_number_carrying_its_own_country_code_is_not_doubled():
    assert normalise_phone("1 415 555 2671").e164 == "+14155552671"


def test_a_ten_digit_us_number_starting_with_one_keeps_its_leading_digit():
    """The regression this guards: stripping a leading "1" as a country code would silently
    corrupt every US number in the 1xx area codes."""
    got = normalise_phone("1234567890")
    assert got.e164 == "+11234567890"


def test_the_national_trunk_zero_is_dropped_outside_nanp():
    assert normalise_phone("020 7946 0958", country="GB").e164 == "+442079460958"


# ---- never lose the input ------------------------------------------------------------------------

def test_an_unparseable_number_is_kept_raw_not_discarded():
    """Losing a contact's only phone number to keep a column tidy is the worse outcome."""
    got = normalise_phone("call me maybe", country="US")
    assert got.e164 == ""
    assert got.raw == "call me maybe"
    assert got.ok is False


def test_a_wrong_length_us_number_is_kept_raw():
    got = normalise_phone("555-2671")
    assert got.e164 == ""
    assert got.raw == "555-2671"


def test_blank_input_is_simply_empty():
    assert normalise_phone(None).raw == ""
    assert normalise_phone("   ").raw == ""


def test_normalising_is_idempotent():
    once = normalise_phone("(415) 555-2671").e164
    assert normalise_phone(once).e164 == once
