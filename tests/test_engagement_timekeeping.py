"""Business days, recipient timezones and follow-up times (spec §8)."""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from nexus.engagement import timekeeping as tk

UTC = timezone.utc


def test_the_recipient_zone_resolves_contact_then_account_then_sdr():
    explicit = tk.resolve_zone(contact_tz="Europe/Berlin", account_country="United States",
                               account_region="TX", sdr_tz="Asia/Kolkata")
    assert (explicit.name, explicit.source) == ("Europe/Berlin", "contact")

    by_region = tk.resolve_zone(contact_tz=None, account_country="United States",
                                account_region="TX", sdr_tz="Asia/Kolkata")
    assert (by_region.name, by_region.source) == ("America/Chicago", "account")

    by_country = tk.resolve_zone(contact_tz="", account_country="DE", account_region=None,
                                 sdr_tz="Asia/Kolkata")
    assert (by_country.name, by_country.source) == ("Europe/Berlin", "account")

    unknown = tk.resolve_zone(contact_tz="Mars/Base", account_country="Atlantis",
                              account_region=None, sdr_tz="Asia/Kolkata")
    assert (unknown.name, unknown.source) == ("Asia/Kolkata", "sdr")

    nothing = tk.resolve_zone(contact_tz=None, account_country=None, account_region=None,
                              sdr_tz=None)
    assert (nothing.name, nothing.source) == ("UTC", "utc")


def test_country_names_codes_and_regions_are_recognised():
    assert tk.zone_for_place("usa", "california") == "America/Los_Angeles"
    assert tk.zone_for_place("U.S.A.", "WA") == "America/Los_Angeles"
    assert tk.zone_for_place("Canada", "BC") == "America/Vancouver"
    assert tk.zone_for_place("Australia", "Western Australia") == "Australia/Perth"
    assert tk.zone_for_place("India", None) == "Asia/Kolkata"
    assert tk.zone_for_place("united kingdom", "somewhere") == "Europe/London"
    assert tk.zone_for_place("", None) == ""


def test_business_day_arithmetic_skips_weekends():
    friday, saturday = date(2026, 9, 18), date(2026, 9, 19)
    assert tk.next_business_day(friday) == date(2026, 9, 21)
    assert tk.roll_to_business_day(saturday) == date(2026, 9, 21)
    assert tk.add_business_days(saturday, 0) == date(2026, 9, 21)
    assert tk.add_business_days(friday, 2) == date(2026, 9, 22)


def test_an_auto_follow_up_goes_out_at_the_same_local_time():
    chicago = tk.zone_or_none("America/Chicago")
    sent = datetime(2026, 9, 18, 20, 30, tzinfo=UTC)  # Friday 15:30 in Chicago
    due = tk.auto_followup_at(previous_sent_at=sent, delay_business_days=2, zone=chicago)
    assert tk.to_local(due, chicago).replace(tzinfo=None) == datetime(2026, 9, 22, 15, 30)


def test_local_time_survives_a_daylight_saving_change():
    london = tk.zone_or_none("Europe/London")
    sent = datetime(2026, 10, 23, 8, 0, tzinfo=UTC)  # Friday 09:00 BST
    due = tk.auto_followup_at(previous_sent_at=sent, delay_business_days=1, zone=london)
    assert due == datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # Monday 09:00 GMT


def test_manual_timing_uses_the_chosen_time_and_allowed_weekdays():
    chicago = tk.zone_or_none("America/Chicago")
    sent = datetime(2026, 9, 18, 20, 30, tzinfo=UTC)
    due = tk.manual_followup_at(previous_sent_at=sent, delay_business_days=2,
                                send_time_local=time(9, 0), allowed_weekdays=[2, 3],
                                zone=chicago)
    assert tk.to_local(due, chicago).replace(tzinfo=None) == datetime(2026, 9, 23, 9, 0)


def test_local_day_start_is_midnight_in_the_mailbox_zone():
    la = tk.zone_or_none("America/Los_Angeles")
    start = tk.local_day_start(datetime(2026, 9, 17, 2, 0, tzinfo=UTC), la)
    assert start == datetime(2026, 9, 16, 7, 0, tzinfo=UTC)


def test_clock_strings_parse_or_refuse():
    assert tk.parse_clock("09:30") == time(9, 30)
    assert tk.parse_clock("9") is None
    assert tk.parse_clock(None) is None
