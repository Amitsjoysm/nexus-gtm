# nexus/alerts/routing.py
"""Where an alert goes, and whether now is an acceptable time to send it.

Pure functions over a user's stored preferences, deliberately free of database and clock access so
the quiet-hours arithmetic — which is where this kind of code goes wrong — is directly testable.

The default when a user has expressed no preference is **deliver as before**: tenant-level channels,
immediately. Silence must never be the accidental result of adding a preferences table.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Route:
    """The delivery decision for one (user, alert) pair."""

    deliver: bool
    channel: str = "in_app"
    mode: str = "immediate"          # immediate | digest
    reason: str = ""


def _local_minutes(utc_minutes: int, offset_min: int) -> int:
    """Minutes past local midnight, wrapping across the day boundary."""
    return (utc_minutes + offset_min) % (24 * 60)


def in_quiet_hours(utc_minutes: int, pref) -> bool:
    """Whether the user's local time falls inside their quiet window.

    Handles the overnight wrap (22:00 → 07:00) by arithmetic rather than a special case: when the
    start is after the end, the window is the *union* of the two ends of the day. Getting this wrong
    is the classic bug — a naive ``start <= now <= end`` silently disables quiet hours for exactly
    the people who set them overnight, which is almost everyone.
    """
    start, end = pref.quiet_from_min, pref.quiet_to_min
    if start is None or end is None or start == end:
        return False
    now = _local_minutes(utc_minutes, pref.utc_offset_min or 0)
    if start < end:
        return start <= now < end
    return now >= start or now < end


def route(pref, *, severity: str, utc_minutes: int) -> Route:
    """Resolve one preference row into a delivery decision.

    ``pref is None`` means the user has expressed no preference, which is **not** "do not deliver":
    it falls through to the existing tenant-level behaviour. Adding this table must not mute anyone.
    """
    if pref is None:
        return Route(deliver=True, reason="no preference; tenant default applies")
    if pref.mode == "off":
        return Route(deliver=False, channel=pref.channel, mode="off",
                     reason="user disabled this category")
    if in_quiet_hours(utc_minutes, pref):
        # A critical alert overrides quiet hours by default — active vendor evaluation is a short
        # window and a rep would rather be woken than lose it — but the user owns that trade.
        if severity == "critical" and pref.quiet_hours_allow_critical:
            return Route(deliver=True, channel=pref.channel, mode="immediate",
                         reason="critical overrides quiet hours")
        # Held, not dropped: the alert still exists and reaches them in the digest.
        return Route(deliver=True, channel=pref.channel, mode="digest",
                     reason="quiet hours; deferred to digest")
    return Route(deliver=True, channel=pref.channel, mode=pref.mode or "immediate",
                 reason="user preference")


def minutes_utc(now) -> int:
    """Minutes past midnight UTC for a datetime."""
    return now.hour * 60 + now.minute
