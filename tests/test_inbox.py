"""Inbox prioritization and task creation/ordering."""
from __future__ import annotations

from datetime import timedelta

from nexus.core.db import utcnow
from nexus.inbox.prioritizer import compute_priority
from nexus.inbox.service import get_inbox_service
from nexus.models.account import Account
from nexus.models.signal import SignalEvent
from tests.conftest import make_tenant, tenant_session


def test_priority_rewards_strength_and_score():
    hot = compute_priority(signal_strength=0.9, composite_score=90)
    cold = compute_priority(signal_strength=0.1, composite_score=10)
    assert hot > cold
    assert 0 <= cold <= hot <= 100


def test_priority_decays_with_age():
    fresh = compute_priority(signal_strength=0.8, composite_score=70, age_days=0)
    stale = compute_priority(signal_strength=0.8, composite_score=70, age_days=60)
    assert fresh > stale


def test_none_composite_is_neutral():
    assert compute_priority(signal_strength=0.5, composite_score=None) == compute_priority(
        signal_strength=0.5, composite_score=50
    )


async def test_open_tasks_ordered_by_priority_desc():
    tid = await make_tenant()
    svc = get_inbox_service()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
        ts.add(acc)
        await ts.flush()

        weak = SignalEvent(tenant_id=tid, account_id=acc.id, kind="news", source="t",
                           title="minor", strength=0.2, dedupe_key="a", occurred_at=utcnow())
        strong = SignalEvent(tenant_id=tid, account_id=acc.id, kind="g2_intent", source="t",
                             title="hot", strength=0.95, dedupe_key="b", occurred_at=utcnow())
        ts.add_all([weak, strong])
        await ts.flush()

        await svc.create_from_signal(ts, weak, acc, composite_score=30)
        await svc.create_from_signal(ts, strong, acc, composite_score=90)

        tasks = await svc.list_open(ts)
        # Title is now account-centric ("Acme: ...") and the headline moved to `reason`; the
        # high-priority signal still sorts first.
        assert [t.title for t in tasks] == ["Acme: Active buying intent", "Acme: Company news"]
        assert [t.reason.split(" · ")[0] for t in tasks] == ["hot", "minor"]


async def test_complete_marks_done():
    tid = await make_tenant()
    svc = get_inbox_service()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
        ts.add(acc)
        await ts.flush()
        sig = SignalEvent(tenant_id=tid, account_id=acc.id, kind="news", source="t",
                          title="x", strength=0.5, dedupe_key="k", occurred_at=utcnow())
        ts.add(sig)
        await ts.flush()
        task = await svc.create_from_signal(ts, sig, acc, composite_score=50)

        done = await svc.complete(ts, task.id)
        assert done.status == "done"
        assert await svc.list_open(ts) == []
