"""The display rules for cross-workspace insights (D26). Pure: a profile in, what may be shown out.

The insights store keeps real people and companies (D25) so the app can advise about real buyers.
What a viewing workspace may SEE of that is decided here and nowhere else:

* **Patterns** ("usually replies on Tuesdays around 10am, typically within 4 hours") only when the
  person has history from **three or more** workspaces. With fewer, a pattern would let a workspace
  infer that a particular other vendor had been emailing the same buyer.
* **Below that, only the speed band of their last reply** ("within an hour", "same day", "within a
  week"), with no date, no count and no sender.
* **Never** who emailed them, what was said, or which workspaces. The profile's workspace keys never
  reach this process: the client reads only the store's `workspace_count`, which is all these rules
  need.
* **Only opted-in workspaces receive.** A workspace that has not switched the ledger on sees nothing
  from it, the same rule that keeps it from contributing.

A company profile follows the same threshold: an aggregate of one other vendor's campaign is still
that vendor's campaign.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

MIN_WORKSPACES = 3
WEEKDAYS = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")
BAND_TEXT = {
    "within_hour": "Last replied within an hour",
    "same_day": "Last replied the same day",
    "within_week": "Last replied within a week",
    "longer": "Last took over a week to reply",
}


@dataclass(slots=True)
class Insight:
    #: "pattern" | "band" | "none"
    level: str = "none"
    text: str = ""
    best_weekday: int | None = None
    best_hour: int | None = None
    typical_response_hours: float | None = None
    band: str = ""
    #: Reply rate across workspaces, 0..1; present only at the pattern level.
    propensity: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _clock(hour: int) -> str:
    suffix = "am" if hour < 12 else "pm"
    shown = hour % 12 or 12
    return f"{shown}{suffix}"


def _duration(hours: float) -> str:
    if hours < 1:
        return "an hour"
    if hours < 24:
        return f"{round(hours)} hours"
    days = round(hours / 24)
    return "a day" if days <= 1 else f"{days} days"


def describe(profile: dict | None, *, viewer_consented: bool, subject: str = "person") -> Insight:
    """What may be shown of one person (or company) profile to a viewing workspace."""
    if not viewer_consented or not profile:
        return Insight()
    workspaces = int(profile.get("workspace_count") or 0)
    if workspaces >= MIN_WORKSPACES:
        weekday, hour = profile.get("best_weekday"), profile.get("best_hour")
        median_s = profile.get("median_response_s")
        hours = round(median_s / 3600, 1) if median_s is not None else None
        parts = []
        if weekday is not None:
            when = f"on {WEEKDAYS[weekday]}"
            if hour is not None:
                when += f" around {_clock(hour)}"
            parts.append(f"usually replies {when}" if subject == "person"
                         else f"people there usually reply {when}")
        if hours is not None:
            parts.append(f"typically within {_duration(hours)}")
        if parts:
            text = ", ".join(parts)
            return Insight(level="pattern", text=text[0].upper() + text[1:],
                           best_weekday=weekday, best_hour=hour, typical_response_hours=hours,
                           propensity=profile.get("reply_propensity"))
    band = profile.get("last_reply_band") or ""
    if subject == "person" and band in BAND_TEXT:
        return Insight(level="band", text=BAND_TEXT[band], band=band)
    return Insight()
