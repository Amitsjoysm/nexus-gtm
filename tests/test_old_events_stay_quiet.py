"""An old event found today goes on the timeline, not into anyone's attention.

With real dates the product can tell a 2023 funding round found this morning from one announced
this morning. Decided with the product owner 2026-09-23: only events under
`signal_alert_max_age_days` (30 by default) raise an alert, an Inbox task, or a channel ping. An
undated ("found") signal still does — its date is when we found it, and it may well be new.
Plays are unchanged: they were kept out of the window when it was decided.

Without this, connecting a company's feed could ping Slack with ten posts from last year, and reps
learn to ignore alerts — the failure alerts exist to prevent.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.core.db import utcnow
from nexus.models.account import Account
from nexus.models.alerts import Alert
from nexus.models.signal import SignalEvent
from nexus.models.workflow import InboxTask
from tests.conftest import make_tenant, tenant_session


async def _seed(tid, rows):
    """Persist one account and the given (title, age_days, dated) funding signals."""
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        ids = []
        for title, age, dated in rows:
            sig = SignalEvent(tenant_id=tid, account_id=acc.id, kind="funding", source="rss",
                              title=title, strength=0.9, dedupe_key=title, dated=dated,
                              occurred_at=utcnow() - timedelta(days=age))
            ts.add(sig)
            await ts.flush()
            ids.append(sig.id)
        return acc.id, ids


@pytest.mark.parametrize("age, dated, alerts", [
    (400, "event", False),   # a 2023 round found today: history, not news
    (31, "event", False),
    (5, "event", True),
    (0, "found", True),      # undated: the day we found it is all we know, and it may be new
])
async def test_only_a_recent_event_raises_an_alert(age, dated, alerts):
    from nexus.alerts.signal_alerts import raise_alerts_for

    tid = await make_tenant()
    acc_id, (sig_id,) = await _seed(tid, [("Acme raises money", age, dated)])
    async with tenant_session(tid) as ts:
        account = await ts.get(Account, acc_id)
        signal = await ts.get(SignalEvent, sig_id)
        await raise_alerts_for(ts, account, [signal])
        raised = await ts.list(Alert)

    assert bool(raised) is alerts


async def test_the_age_limit_is_a_setting(monkeypatch):
    from nexus.alerts.signal_alerts import raise_alerts_for
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "signal_alert_max_age_days", 90)
    tid = await make_tenant()
    acc_id, (sig_id,) = await _seed(tid, [("Acme raises money", 60, "event")])
    async with tenant_session(tid) as ts:
        await raise_alerts_for(ts, await ts.get(Account, acc_id), [await ts.get(SignalEvent, sig_id)])
        assert await ts.list(Alert), "a 60-day-old event should alert when the limit is 90 days"


@pytest.mark.parametrize("age, opens", [(400, False), (2, True)])
async def test_only_a_recent_event_opens_an_inbox_task(age, opens):
    # One signal per run: the Inbox keeps one open task per account, so a newer signal in the same
    # batch would update the task and hide whether the old one opened it.
    from nexus.ingestion.service import set_ingestion_service
    from nexus.pipeline import process_account

    tid = await make_tenant()
    acc_id, (sig_id,) = await _seed(tid, [("Acme raises money", age, "event")])

    class _Ingestion:
        async def run_sources(self, ts, account):
            return [await ts.get(SignalEvent, sig_id)]

    set_ingestion_service(_Ingestion())
    try:
        async with tenant_session(tid) as ts:
            account = await ts.get(Account, acc_id)
            account.last_refreshed_at = utcnow()   # not a first crawl: take the sources' signals
            await process_account(ts, account)
            tasks = await ts.list(InboxTask)
    finally:
        set_ingestion_service(None)

    assert bool(tasks) is opens


def test_the_setting_is_in_the_control_plane():
    from nexus.runtime_config.catalog import CATALOG, SIGNALS

    spec = CATALOG["signal_alert_max_age_days"]
    assert spec.group == SIGNALS and spec.kind == "int"
    assert spec.minimum == 1 and spec.maximum == 365
