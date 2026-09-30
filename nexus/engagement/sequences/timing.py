"""When each step of an enrollment is due (spec §8, D19). Pure.

* **First emails** are due when approved (`on_approval`) or at the campaign's `first_send_at`
  (`scheduled`), whichever the campaign chose — and never before the approval, because a scheduled
  time does not approve a draft.
* **Follow-ups** are due `delay_business_days` after the previous outbound email: at the same local
  time of day (`auto`), or at a chosen time on an allowed weekday (`manual`). No send window is
  imposed (D10).
* **Whose clock:** the contact's timezone by default; the SDR's when the campaign says so.
"""
from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc  # datetime.UTC is 3.11+; this project supports 3.10


def zone_name_for(enrollment, campaign, mailbox) -> str:
    if getattr(campaign, "timezone_mode", "contact") == "sdr":
        return getattr(mailbox, "timezone", "") or "UTC"
    return getattr(enrollment, "contact_timezone", "") or getattr(mailbox, "timezone", "") or "UTC"


def first_due_at(*, first_send_mode: str, first_send_at: datetime | None,
                 approved_at: datetime | None, now: datetime) -> datetime:
    ready = approved_at or now
    if first_send_mode == "scheduled" and first_send_at is not None:
        return max(first_send_at, ready)
    return ready


def followup_due_at(*, step, previous_sent_at: datetime, zone_name: str) -> datetime:
    from nexus.engagement.timekeeping import (
        auto_followup_at,
        manual_followup_at,
        parse_clock,
        zone_or_none,
    )

    zone = zone_or_none(zone_name) or UTC
    delay = max(0, int(getattr(step, "delay_business_days", 0) or 0))
    clock = parse_clock(getattr(step, "send_time_local", None))
    if getattr(step, "timing_mode", "auto") == "manual" and clock is not None:
        return manual_followup_at(previous_sent_at=previous_sent_at, delay_business_days=delay,
                                  send_time_local=clock,
                                  allowed_weekdays=getattr(step, "allowed_weekdays", None) or [],
                                  zone=zone)
    return auto_followup_at(previous_sent_at=previous_sent_at, delay_business_days=delay, zone=zone)


def after_snooze_due_at(*, step, resumed_at: datetime, zone_name: str) -> datetime:
    """After a re-engagement, the remaining steps continue from it, each after its own delay."""
    return followup_due_at(step=step, previous_sent_at=resumed_at, zone_name=zone_name)
