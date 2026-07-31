"""Signals become alerts, and users choose where alerts go (M21).

The gap: `signal.created` was published on every ingested signal and **nothing subscribed to it** —
the only subscriber on the bus listened for `account.scored`. Alerts were created in three places,
none of them from an incoming signal, so the entire collection pipeline landed in a table nobody was
notified about. A rep learned about a customer's Series F by opening the account page and scrolling.
"""
from __future__ import annotations

import pytest

from nexus.alerts.rules import ALERT_CATEGORIES, alert_dedupe_key, decide
from nexus.alerts.routing import Route, in_quiet_hours, route
from tests.conftest import make_tenant, tenant_session


class _Pref:
    """Stand-in for a NotificationPreference row — routing is a pure function over it."""

    def __init__(self, **kw):
        self.channel = kw.get("channel", "in_app")
        self.mode = kw.get("mode", "immediate")
        self.quiet_from_min = kw.get("quiet_from_min")
        self.quiet_to_min = kw.get("quiet_to_min")
        self.utc_offset_min = kw.get("utc_offset_min", 0)
        self.quiet_hours_allow_critical = kw.get("quiet_hours_allow_critical", True)


# ---- which signals are worth an interruption --------------------------------------------------

def test_a_funding_round_is_critical_and_carries_the_next_action():
    """"Acme raised a Series B" is information. The suggested action makes it a prompt."""
    d = decide("funding", 0.9)
    assert d.should_alert and d.severity == "critical" and d.category == "funding"
    assert "reach out" in d.suggested_action.lower()


def test_a_weak_mention_never_interrupts_anyone():
    """An alert costs attention. Attention spent on a press mention is attention not spent on a
    funding round — which is what the classifier's 0.4 weak tier was always for."""
    d = decide("news", 0.4)
    assert not d.should_alert
    assert "below alert floor" in d.reason


def test_a_strong_signal_in_a_quiet_category_is_escalated():
    """A 0.9 news item is an acquisition, not a press mention."""
    assert decide("news", 0.4).severity == "info"
    assert decide("news", 0.9).severity == "warning"


def test_an_unknown_kind_alerts_quietly_rather_than_vanishing():
    """A new signal kind nobody wrote a rule for must degrade to "tell someone quietly", never to
    "disappear" — the same bias that makes an unknown billing capability resolve to allow."""
    d = decide("some.brand.new.kind", 0.8)
    assert d.should_alert and d.severity == "info" and d.category == "news"


def test_categories_are_derived_from_the_rules():
    """Two hand-maintained lists would drift, and a category users can subscribe to but nothing
    ever emits is a silent dead end."""
    assert "funding" in ALERT_CATEGORIES and "hiring" in ALERT_CATEGORIES
    assert len(ALERT_CATEGORIES) == len(set(ALERT_CATEGORIES))


def test_alert_dedupe_is_separate_from_signal_dedupe():
    """Signals dedupe on the event; alerts dedupe on attention. Two different job postings are two
    real signals and one notification."""
    a = alert_dedupe_key("hiring", "acct1", "2026-07-31")
    b = alert_dedupe_key("hiring", "acct1", "2026-07-31")
    assert a == b
    assert a != alert_dedupe_key("funding", "acct1", "2026-07-31")
    assert a != alert_dedupe_key("hiring", "acct2", "2026-07-31")


# ---- routing and quiet hours ------------------------------------------------------------------

def test_no_preference_means_deliver_as_before():
    """Adding a preferences table must not mute anyone. Silence must never be an accident."""
    r = route(None, severity="info", utc_minutes=3 * 60)
    assert r.deliver and r.mode == "immediate"


def test_a_user_can_switch_a_category_off():
    r = route(_Pref(mode="off"), severity="warning", utc_minutes=600)
    assert not r.deliver


def test_overnight_quiet_hours_wrap_correctly():
    """The classic bug: a naive `start <= now < end` disables quiet hours for exactly the people
    who set them overnight, which is almost everyone."""
    pref = _Pref(quiet_from_min=22 * 60, quiet_to_min=7 * 60)
    assert in_quiet_hours(23 * 60, pref)        # 23:00 — inside
    assert in_quiet_hours(3 * 60, pref)         # 03:00 — inside, after the wrap
    assert not in_quiet_hours(12 * 60, pref)    # midday — outside


def test_daytime_quiet_hours_do_not_wrap():
    pref = _Pref(quiet_from_min=9 * 60, quiet_to_min=17 * 60)
    assert in_quiet_hours(12 * 60, pref)
    assert not in_quiet_hours(20 * 60, pref)


def test_quiet_hours_are_evaluated_in_the_users_local_time():
    """A user at UTC+9 with 22:00–07:00 quiet hours is asleep at 14:00 UTC."""
    pref = _Pref(quiet_from_min=22 * 60, quiet_to_min=7 * 60, utc_offset_min=9 * 60)
    assert in_quiet_hours(14 * 60, pref)        # 23:00 local
    assert not in_quiet_hours(3 * 60, pref)     # 12:00 local


def test_quiet_hours_defer_to_digest_rather_than_dropping():
    """Held, not lost — the alert still reaches them."""
    pref = _Pref(quiet_from_min=22 * 60, quiet_to_min=7 * 60)
    r = route(pref, severity="warning", utc_minutes=23 * 60)
    assert r.deliver and r.mode == "digest"


def test_a_critical_alert_overrides_quiet_hours_by_default():
    """Active vendor evaluation is a short window; a rep would rather be woken than lose it."""
    pref = _Pref(quiet_from_min=22 * 60, quiet_to_min=7 * 60)
    assert route(pref, severity="critical", utc_minutes=23 * 60).mode == "immediate"


def test_but_the_user_owns_that_trade():
    pref = _Pref(quiet_from_min=22 * 60, quiet_to_min=7 * 60, quiet_hours_allow_critical=False)
    assert route(pref, severity="critical", utc_minutes=23 * 60).mode == "digest"


def test_unset_quiet_hours_are_not_quiet_hours():
    assert not in_quiet_hours(3 * 60, _Pref())
    assert not in_quiet_hours(3 * 60, _Pref(quiet_from_min=60, quiet_to_min=60))


# ---- the subscriber -----------------------------------------------------------------------------

async def _signal(tid: str, *, kind="funding", strength=0.9, title="Acme raises $40M"):
    from nexus.core.db import utcnow
    from nexus.models.account import Account
    from nexus.models.signal import SignalEvent

    async with tenant_session(tid) as ts:
        acct = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acct)
        await ts.flush()
        sig = SignalEvent(
            tenant_id=tid, account_id=acct.id, kind=kind, source="test", title=title,
            strength=strength, dedupe_key=f"k:{kind}:{strength}", occurred_at=utcnow(),
        )
        ts.add(sig)
        await ts.flush()
        return acct.id, sig.id


async def _fire(tid: str, signal_id: str):
    """Raise alerts for a signal, in the same transaction that owns it.

    Called directly rather than through the event bus: at publish time the signal is flushed but
    NOT committed, so a bus subscriber opening its own session cannot see it — and `alerts.
    signal_id` is a foreign key it could not satisfy anyway. The deployed bus version produced five
    signals and zero alerts.
    """
    from nexus.alerts.signal_alerts import raise_alerts_for
    from nexus.models.account import Account
    from nexus.models.signal import SignalEvent

    async with tenant_session(tid) as ts:
        signal = await ts.first(SignalEvent, SignalEvent.id == signal_id)
        if signal is None:
            return
        account = await ts.first(Account, Account.id == signal.account_id)
        await raise_alerts_for(ts, account, [signal])


async def _alerts(tid: str):
    from nexus.models.alerts import Alert

    async with tenant_session(tid) as ts:
        return await ts.list(Alert)


async def test_a_strong_signal_becomes_an_alert():
    tid = await make_tenant(slug="sa1")
    _acct, sig = await _signal(tid)
    await _fire(tid, sig)

    alerts = await _alerts(tid)
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert alerts[0].signal_id == sig
    assert alerts[0].source == "signal"
    # The next action travels with the alert, not just the fact.
    assert "reach out" in alerts[0].body.lower()
    assert alerts[0].meta["category"] == "funding"


async def test_a_weak_signal_produces_no_alert():
    tid = await make_tenant(slug="sa2")
    _acct, sig = await _signal(tid, kind="news", strength=0.4, title="Acme mentioned")
    await _fire(tid, sig)
    assert await _alerts(tid) == []


async def test_the_same_category_does_not_interrupt_twice_in_a_day():
    """Two different job postings are two real signals and one notification."""
    from nexus.core.db import utcnow
    from nexus.models.signal import SignalEvent

    tid = await make_tenant(slug="sa3")
    acct, first = await _signal(tid, kind="job_posting", strength=0.7, title="Acme hiring SRE")
    await _fire(tid, first)

    async with tenant_session(tid) as ts:
        second = SignalEvent(
            tenant_id=tid, account_id=acct, kind="job_posting", source="test",
            title="Acme hiring AE", strength=0.7, dedupe_key="k2", occurred_at=utcnow(),
        )
        ts.add(second)
        await ts.flush()
        second_id = second.id
    await _fire(tid, second_id)

    assert len(await _alerts(tid)) == 1


async def test_different_categories_both_alert():
    from nexus.core.db import utcnow
    from nexus.models.signal import SignalEvent

    tid = await make_tenant(slug="sa4")
    acct, funding = await _signal(tid)
    await _fire(tid, funding)
    async with tenant_session(tid) as ts:
        hiring = SignalEvent(
            tenant_id=tid, account_id=acct, kind="job_posting", source="test",
            title="Acme hiring", strength=0.7, dedupe_key="k3", occurred_at=utcnow(),
        )
        ts.add(hiring)
        await ts.flush()
        hiring_id = hiring.id
    await _fire(tid, hiring_id)

    assert len(await _alerts(tid)) == 2


async def test_the_kill_switch_restores_the_previous_silence(monkeypatch):
    from nexus.core.config import get_settings

    tid = await make_tenant(slug="sa5")
    _acct, sig = await _signal(tid)
    monkeypatch.setattr(get_settings(), "signal_alerts_enabled", False)
    await _fire(tid, sig)
    assert await _alerts(tid) == []


async def test_an_alerting_failure_never_loses_the_signal(monkeypatch):
    """Rolling back ingestion to save a notification would be exactly backwards."""
    from nexus.alerts import signal_alerts

    tid = await make_tenant(slug="sa6")
    _acct, sig = await _signal(tid)

    def boom(*_a, **_kw):
        raise RuntimeError("alert service down")

    monkeypatch.setattr(signal_alerts, "_already_alerted", boom)
    await _fire(tid, sig)          # must not raise
    from nexus.models.signal import SignalEvent

    async with tenant_session(tid) as ts:
        assert len(await ts.list(SignalEvent)) == 1


async def test_a_missing_signal_is_ignored():
    tid = await make_tenant(slug="sa7")
    await _fire(tid, "does-not-exist")
    assert await _alerts(tid) == []


async def test_alerts_are_created_by_ingestion_itself():
    """The integration that the bus version silently failed: ingesting a strong signal must produce
    an alert in the same transaction, with no separate subscriber involved."""
    from nexus.ingestion.service import IngestionService
    from nexus.ingestion.sources import RawSignal
    from nexus.models.account import Account

    class Source:
        name = "probe"

        async def fetch(self, account):
            return [RawSignal(kind="funding", source="probe", title="Acme raises $40M",
                              dedupe_key="probe:f1", strength=0.9)]

    tid = await make_tenant(slug="sa8")
    async with tenant_session(tid) as ts:
        acct = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acct)
        await ts.flush()
        await IngestionService(sources=[Source()]).run_sources(ts, acct)

    alerts = await _alerts(tid)
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert alerts[0].signal_id                      # the FK the bus version could not satisfy


@pytest.mark.parametrize("route_result", [Route(deliver=True), Route(deliver=False)])
def test_route_is_a_plain_value(route_result):
    assert isinstance(route_result.deliver, bool)
