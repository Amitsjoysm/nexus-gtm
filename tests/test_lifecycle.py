"""Subscription lifecycle: proration, trial expiry, pause/resume (M22).

Three money bugs, all quiet:

* A mid-cycle plan change charged the new plan's **full** base fee and credited nothing. Upgrading
  on the 28th of a 30-day month billed a full month for two days of service.
* A trial had no automatic transition — it sat in `trialing` forever, a live subscription
  contributing zero revenue.
* `suspended` was in `SUBSCRIPTION_STATUSES` and nothing ever set it, so a customer asking to pause
  had to be cancelled.

Money is integer cents throughout. A fraction of a cent per proration compounds into an invoice that
does not reconcile, and "off by one cent" is indistinguishable from fraud to an auditor.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nexus.billing.lifecycle import (
    can_pause,
    can_resume,
    paused_extension,
    prorate,
    trial_expiry,
    trial_verdict,
)

START = datetime(2026, 7, 1, tzinfo=timezone.utc)
END = datetime(2026, 7, 31, tzinfo=timezone.utc)      # a 30-day period


def _at(day: int) -> datetime:
    return datetime(2026, 7, day, tzinfo=timezone.utc)


# ---- proration ----------------------------------------------------------------------------------

def test_a_change_on_day_one_is_the_full_price_difference():
    """Nothing has been consumed yet, so the whole old month is credited and the whole new one
    charged — the net is exactly the difference in list price."""
    p = prorate(old_monthly_cents=2900, new_monthly_cents=7900,
                period_start=START, period_end=END, at=START)
    assert p.credit_cents == 2900
    assert p.charge_cents == 7900
    assert p.net_cents == 5000


def test_a_late_upgrade_charges_only_the_remaining_days():
    """The headline bug: this used to bill 7900 for two days of service."""
    p = prorate(old_monthly_cents=2900, new_monthly_cents=7900,
                period_start=START, period_end=END, at=_at(29))
    assert p.days_remaining == 2 and p.days_in_period == 30
    assert p.charge_cents == 526          # 7900 * 2/30, rounded down
    assert p.net_cents < 1000             # not a full month


def test_rounding_favours_the_customer():
    """Any rounding rule loses a fraction of a cent somewhere. Losing it in the customer's favour
    makes it a policy; the other way round it is an overcharge they can dispute and be right."""
    p = prorate(old_monthly_cents=2900, new_monthly_cents=7900,
                period_start=START, period_end=END, at=_at(29))
    # Credit rounds UP from 193.33, charge rounds DOWN from 526.67.
    assert p.credit_cents == 194
    assert p.charge_cents == 526


def test_a_downgrade_produces_a_net_credit():
    p = prorate(old_monthly_cents=7900, new_monthly_cents=2900,
                period_start=START, period_end=END, at=_at(16))
    assert p.net_cents < 0                # the customer is owed


def test_a_change_at_period_end_prorates_nothing():
    p = prorate(old_monthly_cents=2900, new_monthly_cents=7900,
                period_start=START, period_end=END, at=END)
    assert p.days_remaining == 0
    assert p.credit_cents == 0 and p.charge_cents == 0


def test_a_change_after_period_end_prorates_nothing():
    p = prorate(old_monthly_cents=2900, new_monthly_cents=7900,
                period_start=START, period_end=END, at=END + timedelta(days=5))
    assert p.net_cents == 0


def test_money_never_becomes_a_float():
    p = prorate(old_monthly_cents=999, new_monthly_cents=1001,
                period_start=START, period_end=END, at=_at(7))
    assert isinstance(p.credit_cents, int) and isinstance(p.charge_cents, int)
    assert isinstance(p.net_cents, int)


def test_moving_from_a_free_plan_credits_nothing():
    p = prorate(old_monthly_cents=0, new_monthly_cents=7900,
                period_start=START, period_end=END, at=_at(16))
    assert p.credit_cents == 0
    assert p.charge_cents > 0


def test_moving_to_a_free_plan_charges_nothing():
    p = prorate(old_monthly_cents=7900, new_monthly_cents=0,
                period_start=START, period_end=END, at=_at(16))
    assert p.charge_cents == 0
    assert p.credit_cents > 0
    assert p.net_cents < 0


def test_a_degenerate_period_does_not_divide_by_zero():
    p = prorate(old_monthly_cents=2900, new_monthly_cents=7900,
                period_start=START, period_end=START, at=START)
    assert p.days_in_period >= 1


# ---- trial expiry -------------------------------------------------------------------------------

def test_a_plan_with_no_trial_has_no_expiry():
    assert trial_expiry(START, 0) is None
    assert trial_expiry(START, -1) is None


def test_a_trial_expires_after_its_configured_days():
    assert trial_expiry(START, 14) == START + timedelta(days=14)


def test_a_live_trial_is_left_alone():
    verdict = trial_verdict(status="trialing", started_at=START, trial_days=14,
                            now=START + timedelta(days=3), has_payment_method=False)
    assert verdict == "trialing"


def test_an_expired_trial_with_a_card_converts():
    verdict = trial_verdict(status="trialing", started_at=START, trial_days=14,
                            now=START + timedelta(days=15), has_payment_method=True)
    assert verdict == "active"


def test_an_expired_trial_without_a_card_is_cancelled_not_left_live():
    """Leaving it live gives the product away indefinitely. Flipping it to `active` with no way to
    charge would manufacture receivables that can never be collected and pollute MRR."""
    verdict = trial_verdict(status="trialing", started_at=START, trial_days=14,
                            now=START + timedelta(days=15), has_payment_method=False)
    assert verdict == "canceled"


def test_a_non_trial_subscription_is_untouched():
    for status in ("active", "past_due", "canceled", "suspended"):
        assert trial_verdict(status=status, started_at=START, trial_days=14,
                             now=START + timedelta(days=99), has_payment_method=False) == status


# ---- pause and resume ---------------------------------------------------------------------------

def test_an_active_subscription_can_be_paused():
    ok, reason = can_pause("active")
    assert ok and reason == ""


def test_a_past_due_subscription_cannot_be_paused():
    """Pause is not a way to make an unpaid balance stop being chased — `suspended` is a status the
    dunning sweep ignores, so allowing this would hide a real debt."""
    ok, reason = can_pause("past_due")
    assert not ok
    assert "balance" in reason


def test_pausing_twice_is_refused_clearly():
    ok, reason = can_pause("suspended")
    assert not ok and "already paused" in reason


def test_a_cancelled_subscription_cannot_be_paused():
    assert can_pause("canceled")[0] is False


def test_only_a_paused_subscription_can_resume():
    assert can_resume("suspended")[0] is True
    for status in ("active", "trialing", "canceled", "past_due"):
        assert can_resume(status)[0] is False


def test_paused_days_extend_the_period():
    """Without this, pausing for two weeks silently shortens the paid month: the customer pays for
    thirty days and receives sixteen — the same overcharge proration exists to prevent."""
    extension = paused_extension(_at(10), _at(24))
    assert extension == timedelta(days=14)


def test_a_negative_pause_window_extends_nothing():
    """Clock skew or a bad backfill must not shorten anyone's period."""
    assert paused_extension(_at(24), _at(10)) == timedelta(0)
