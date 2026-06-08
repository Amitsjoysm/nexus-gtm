"""Triage-grade inbox: glanceable deliverability / intent recency / grounding per row.

All offline. The triage rollup is a pure read over already-persisted records (signal,
account, best contact) — no verification calls — so it is fully provable with SQLite.
"""
from __future__ import annotations

from datetime import timedelta

from nexus.core.db import utcnow
from nexus.inbox.service import get_inbox_service
from nexus.inbox.triage import pick_contact, summarize
from nexus.models.account import Account, Contact
from nexus.models.signal import SignalEvent
from nexus.models.workflow import InboxTask
from tests.conftest import make_tenant, tenant_session


# -- pure helpers (no DB) ------------------------------------------------------------


def _contact(**kw) -> Contact:
    base = dict(tenant_id="t1", account_id="a1", full_name="Dana Rep")
    base.update(kw)
    return Contact(**base)


def test_pick_contact_prefers_verified_deliverable_then_confidence():
    no_email = _contact(full_name="No Email", email=None)
    invalid = _contact(full_name="Bad", email="x@y.co", email_status="invalid", email_confidence=0.9)
    unknown = _contact(full_name="Maybe", email="m@y.co", email_status="unknown", email_confidence=0.4)
    valid = _contact(full_name="Good", email="g@y.co", email_status="valid", email_confidence=0.3)

    # Deliverable verdict wins even though its enrichment confidence is lowest.
    assert pick_contact([no_email, invalid, unknown, valid]) is valid
    # Among non-deliverable, unknown beats known-invalid.
    assert pick_contact([invalid, unknown]) is unknown


def test_pick_contact_returns_none_when_no_emails():
    assert pick_contact([_contact(email=None)]) is None
    assert pick_contact([]) is None


def test_summarize_reports_recency_grounding_and_deliverability():
    now = utcnow()
    signal = SignalEvent(
        tenant_id="t1", account_id="a1", kind="g2_intent", source="t", title="hot",
        strength=0.8, dedupe_key="k", occurred_at=now - timedelta(hours=5),
    )
    account = Account(tenant_id="t1", name="Acme", domain="acme.co")
    contact = _contact(email="g@acme.co", email_status="valid", email_confidence=0.7)
    task = InboxTask(tenant_id="t1", title="t", priority=50)

    s = summarize(task, signal=signal, account=account, contact=contact, now=now)

    assert s.signal_kind == "g2_intent"
    assert s.signal_strength == 0.8
    assert 4.9 < s.signal_age_hours < 5.1
    assert s.research_ready is True            # account has a domain
    assert s.deliverability == "valid"
    assert s.email_confidence == 0.7


def test_summarize_degrades_without_links():
    task = InboxTask(tenant_id="t1", title="t", priority=10)
    account = Account(tenant_id="t1", name="NoDomain", domain=None)

    s = summarize(task, signal=None, account=account, contact=None)

    assert s.signal_kind is None and s.signal_age_hours is None
    assert s.research_ready is False           # no domain -> not groundable
    assert s.deliverability is None


# -- service-level batch (DB, offline) -----------------------------------------------


async def test_triage_batch_maps_each_task_to_its_summary():
    tid = await make_tenant()
    svc = get_inbox_service()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
        ts.add(acc)
        await ts.flush()
        ts.add(Contact(tenant_id=tid, account_id=acc.id, full_name="Buyer",
                       email="buyer@acme.co", email_status="unknown", email_confidence=0.5))
        sig = SignalEvent(tenant_id=tid, account_id=acc.id, kind="funding", source="t",
                          title="raised", strength=0.9, dedupe_key="k1", occurred_at=utcnow())
        ts.add(sig)
        await ts.flush()
        task = await svc.create_from_signal(ts, sig, acc, composite_score=80)

        tasks = await svc.list_open(ts)
        triage = await svc.triage(ts, tasks)

        assert set(triage) == {t.id for t in tasks}
        s = triage[task.id]
        assert s.signal_kind == "funding"
        assert s.deliverability == "unknown"
        assert s.research_ready is True
        assert s.signal_age_hours is not None and s.signal_age_hours >= 0.0


async def test_triage_empty_is_empty():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        assert await get_inbox_service().triage(ts, []) == {}
