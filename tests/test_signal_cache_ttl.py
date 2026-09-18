# tests/test_signal_cache_ttl.py
"""The TTL is the cadence: how long we wait before asking the web the same question again.

Funding and news keep the six-hour loop because being first is the value. Everything else moves to
a day, which is where the saving comes from. The values are settings, so a cadence can be tightened
from the Control plane without a deploy — which means the policy must READ them per call, not
capture them at import.
"""
from __future__ import annotations

from nexus.fetching.ttl import ttl_for_page, ttl_for_signal_kind


def test_time_critical_kinds_keep_the_six_hour_loop():
    assert ttl_for_signal_kind("funding") == 21600
    assert ttl_for_signal_kind("news") == 21600


def test_slower_kinds_move_to_a_day():
    for kind in ("job_posting", "hiring", "tech_install", "website_change"):
        assert ttl_for_signal_kind(kind) == 86400


def test_an_unknown_kind_is_treated_as_slow():
    # Being wrong here costs freshness on one kind; the opposite default costs money on all of them.
    assert ttl_for_signal_kind("") == 86400
    assert ttl_for_signal_kind("something_new") == 86400


def test_a_changed_setting_reaches_the_next_call(monkeypatch):
    from nexus.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "signal_cache_ttl_fast_s", 60, raising=False)
    assert ttl_for_signal_kind("funding") == 60


def test_pages_have_their_own_window():
    assert ttl_for_page() == 86400
