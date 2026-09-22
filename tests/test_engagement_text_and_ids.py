"""Subjects keep exactly one "Re:" (D16); ULIDs sort by time; workspace settings stay in range (D23)."""
from __future__ import annotations

import pytest

from nexus.engagement.ids import new_ulid, ulid_timestamp_ms
from nexus.engagement.settings import (
    DEFAULTS,
    EngagementSettings,
    effective_confidence,
    read_settings,
    validate_mailbox_confidence,
    validate_update,
    with_settings,
)
from nexus.engagement.subjects import normalize_subject, reply_subject


@pytest.mark.parametrize("raw,base", [
    ("Re: RE: AW: Quick question", "Quick question"),
    ("RE : Fwd: [EXTERNAL] Re[2]: pricing", "pricing"),
    ("SV: VS: hello", "hello"),
    ("Antw: Odp: Ynt: x", "x"),
    ("TR: RV: y", "y"),
    ("  Re:   spaced   out ", "spaced out"),
    ("Revenue ops at Acme", "Revenue ops at Acme"),
    ("R&D hiring at Acme", "R&D hiring at Acme"),
    ("", ""),
])
def test_every_reply_and_forward_marker_is_removed(raw, base):
    assert normalize_subject(raw) == base


def test_a_reply_subject_carries_exactly_one_re():
    assert reply_subject("RE: Re: RE: hello") == "Re: hello"
    assert reply_subject(reply_subject("hello")) == "Re: hello"
    assert reply_subject("") == "Re:"


def test_ulids_sort_by_time_and_carry_their_timestamp():
    early, late = new_ulid(1_700_000_000_000), new_ulid(1_700_000_000_001)
    assert len(early) == 26 and early < late
    assert ulid_timestamp_ms(early) == 1_700_000_000_000
    assert len({new_ulid() for _ in range(5000)}) == 5000
    with pytest.raises(ValueError):
        ulid_timestamp_ms("not-a-ulid")


def test_settings_default_when_nothing_is_stored_and_tolerate_garbage():
    assert read_settings({}) == DEFAULTS
    # Each bad field falls back on its own; the valid ones are kept.
    stored = {"engagement": {"reply_confidence_default": "abc", "reply_confidence_min": 0.6,
                             "reply_confidence_max": 0.2, "ooo_default_days": 400}}
    settings = read_settings(stored)
    assert settings.reply_confidence_default == DEFAULTS.reply_confidence_default
    assert settings.reply_confidence_min == 0.6
    assert settings.reply_confidence_max == DEFAULTS.reply_confidence_max
    assert settings.ooo_default_days == DEFAULTS.ooo_default_days
    # An inverted range cannot be repaired field by field, so the whole range falls back.
    inverted = read_settings({"engagement": {"reply_confidence_min": 0.9,
                                             "reply_confidence_max": 0.7}})
    assert (inverted.reply_confidence_min, inverted.reply_confidence_max) == (
        DEFAULTS.reply_confidence_min, DEFAULTS.reply_confidence_max)


def test_a_manager_sets_a_range_and_the_default_must_sit_inside_it():
    updated = validate_update(DEFAULTS, {"reply_confidence_min": 0.7,
                                         "reply_confidence_max": 0.95,
                                         "reply_confidence_default": 0.85})
    assert (updated.reply_confidence_min, updated.reply_confidence_default,
            updated.reply_confidence_max) == (0.7, 0.85, 0.95)
    assert read_settings(with_settings({"accounts": []}, updated)) == updated
    with pytest.raises(ValueError, match="inside the range"):
        validate_update(DEFAULTS, {"reply_confidence_min": 0.9, "reply_confidence_default": 0.8})
    with pytest.raises(ValueError, match="between 0.5 and 0.99"):
        validate_update(DEFAULTS, {"reply_confidence_min": 0.3})
    with pytest.raises(ValueError, match="unknown"):
        validate_update(DEFAULTS, {"threshold": 0.9})


def test_an_sdrs_own_bar_must_sit_inside_the_workspace_range():
    settings = EngagementSettings(reply_confidence_default=0.8, reply_confidence_min=0.7,
                                  reply_confidence_max=0.9)
    assert validate_mailbox_confidence(settings, 0.75) == 0.75
    assert validate_mailbox_confidence(settings, None) is None
    with pytest.raises(ValueError, match="between 0.70 and 0.90"):
        validate_mailbox_confidence(settings, 0.95)
    assert effective_confidence(settings, None) == 0.8
    assert effective_confidence(settings, 0.95) == 0.9  # a range narrowed later still binds
