"""Workspace engagement settings: the reply confidence bar and its range (D23), and two timings.

Stored in ``Tenant.email_settings["engagement"]`` beside the mailboxes, signatures and style —
workspace preference rather than schema, so no migration and nothing for ``apply_rls.py`` to enrol.

**The confidence bar decides what happens without a human.** It governs ``later``, ``declined``
and ``unsubscribe``: a reading at or above it acts (snooze, block), a reading below it becomes
``unclear`` and waits for the SDR. Owners, admins and managers set the workspace default and the
range an SDR may choose inside; an SDR sets their own value for their own mailbox; a reply is judged
by its mailbox owner's value (spec §6).

The hard limits are 0.50 and 0.99. Below 0.50 a clear "no" would be acted on when the model thinks
it is more likely NOT a clear no; 1.00 would mean no reply is ever acted on, which is a switch
dressed as a threshold.

Reading never raises. A malformed stored value falls back to the default for that field, because a
typo in a JSON blob must not stop replies being processed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

HARD_MIN = 0.50
HARD_MAX = 0.99


@dataclass(frozen=True, slots=True)
class EngagementSettings:
    reply_confidence_default: float = 0.80
    reply_confidence_min: float = 0.50
    reply_confidence_max: float = 0.99
    # Remind the SDR when an interested/question reply is unanswered this long (§19).
    reply_reminder_business_hours: int = 4
    # Out-of-office with no return date: resume after this many days (spec §6, flagged default).
    ooo_default_days: int = 7


DEFAULTS = EngagementSettings()


def _float(raw, fallback: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    return value if HARD_MIN <= value <= HARD_MAX else fallback


def _int(raw, fallback: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return value if low <= value <= high else fallback


def read_settings(email_settings: dict | None) -> EngagementSettings:
    """The effective settings for a workspace. Never raises."""
    stored = (email_settings or {}).get("engagement")
    if not isinstance(stored, dict):
        return DEFAULTS
    low = _float(stored.get("reply_confidence_min"), DEFAULTS.reply_confidence_min)
    high = _float(stored.get("reply_confidence_max"), DEFAULTS.reply_confidence_max)
    if low > high:
        low, high = DEFAULTS.reply_confidence_min, DEFAULTS.reply_confidence_max
    default = _float(stored.get("reply_confidence_default"), DEFAULTS.reply_confidence_default)
    default = min(max(default, low), high)
    return EngagementSettings(
        reply_confidence_default=default,
        reply_confidence_min=low,
        reply_confidence_max=high,
        reply_reminder_business_hours=_int(
            stored.get("reply_reminder_business_hours"),
            DEFAULTS.reply_reminder_business_hours, 1, 72,
        ),
        ooo_default_days=_int(stored.get("ooo_default_days"), DEFAULTS.ooo_default_days, 1, 60),
    )


def validate_update(current: EngagementSettings, patch: dict) -> EngagementSettings:
    """Apply ``patch`` to ``current``. Raises ``ValueError`` naming the problem; writes nothing."""
    unknown = sorted(set(patch) - set(asdict(current)))
    if unknown:
        raise ValueError(f"unknown engagement settings: {', '.join(unknown)}")
    merged = {**asdict(current), **patch}
    for key in ("reply_confidence_default", "reply_confidence_min", "reply_confidence_max"):
        try:
            merged[key] = round(float(merged[key]), 2)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a number between {HARD_MIN} and {HARD_MAX}") from exc
        if not HARD_MIN <= merged[key] <= HARD_MAX:
            raise ValueError(f"{key} must be between {HARD_MIN} and {HARD_MAX}")
    if merged["reply_confidence_min"] > merged["reply_confidence_max"]:
        raise ValueError("the lowest value SDRs may choose cannot be above the highest")
    if not merged["reply_confidence_min"] <= merged["reply_confidence_default"] <= merged[
        "reply_confidence_max"
    ]:
        raise ValueError("the workspace default must sit inside the range SDRs may choose from")
    for key, low, high in (("reply_reminder_business_hours", 1, 72), ("ooo_default_days", 1, 60)):
        try:
            merged[key] = int(merged[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a whole number from {low} to {high}") from exc
        if not low <= merged[key] <= high:
            raise ValueError(f"{key} must be a whole number from {low} to {high}")
    return EngagementSettings(**merged)


def with_settings(email_settings: dict | None, settings: EngagementSettings) -> dict:
    """A NEW email_settings dict carrying ``settings`` (reassign it so SQLAlchemy sees the change)."""
    updated = dict(email_settings or {})
    updated["engagement"] = asdict(settings)
    return updated


def validate_mailbox_confidence(settings: EngagementSettings, value: float | None) -> float | None:
    """An SDR's own bar for their mailbox; ``None`` clears it. Raises outside the workspace range."""
    if value is None:
        return None
    try:
        number = round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError("the confidence bar must be a number") from exc
    if not settings.reply_confidence_min <= number <= settings.reply_confidence_max:
        raise ValueError(
            f"choose a value between {settings.reply_confidence_min:.2f} and "
            f"{settings.reply_confidence_max:.2f}, the range your workspace allows"
        )
    return number


def effective_confidence(settings: EngagementSettings, mailbox_value: float | None) -> float:
    """The bar a reply to this mailbox is judged by: the SDR's own value, clamped to the range."""
    if mailbox_value is None:
        return settings.reply_confidence_default
    return min(max(float(mailbox_value), settings.reply_confidence_min),
               settings.reply_confidence_max)
