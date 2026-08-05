# tests/test_refresh_tiering.py
"""Tiered account refresh, and the claim that reads it.

Every account used to be refreshed on the same 6h cycle. Measured, that is what makes the pipeline
unaffordable: 500 tenants x 1000 accounts demands 23.15 accounts/sec against a measured drain of
0.036/sec. Tiering attacks the demand side — most of that spend is re-crawling accounts where
nothing has happened in months.

The thing these tests are really guarding is the direction of the mistake. Wrongly hot costs one
crawl; wrongly cold means a rep learns about a funding round three days late, which is the failure
the product exists to prevent. So every rule is a reason to stay hot, and the tests below check
that far more than they check the cold path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.conftest import make_tenant


async def _account(ts, **kw):
    from nexus.models.account import Account

    acct = Account(name=kw.pop("name", "Acme"), domain=kw.pop("domain", "acme.com"), **kw)
    ts.add(acct)
    await ts.flush()
    return acct


# ---- classification -------------------------------------------------------------------------

async def test_a_fresh_signal_makes_an_account_hot_without_a_single_query(fresh_db, monkeypatch):
    """The common case for an active account costs zero queries: a signal in hand is the
    strongest possible evidence the account is worth watching."""
    from nexus.ingestion import tiering
    from nexus.workers.tasks import tenant_session

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)

        called = []
        monkeypatch.setattr(
            tiering, "_has_recent_signal",
            lambda *a, **k: called.append("queried") or False,
        )
        tier = await tiering.classify(ts, acct, new_signals=[object()])

    assert tier == tiering.HOT
    assert called == []          # short-circuited before touching the database


async def test_a_recent_signal_in_history_keeps_an_account_hot(fresh_db):
    from nexus.ingestion import tiering
    from nexus.models.signal import SignalEvent
    from nexus.workers.tasks import tenant_session

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        ts.add(SignalEvent(
            account_id=acct.id, kind="funding", source="test", title="Series B", strength=0.9,
            dedupe_key="s1", occurred_at=datetime.now(timezone.utc) - timedelta(days=3),
        ))
        await ts.flush()
        assert await tiering.classify(ts, acct, new_signals=[]) == tiering.HOT


async def test_an_old_signal_does_not_keep_it_hot(fresh_db):
    """The window has to actually expire, or nothing ever cools and tiering does nothing."""
    from nexus.ingestion import tiering
    from nexus.models.signal import SignalEvent
    from nexus.workers.tasks import tenant_session

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        ts.add(SignalEvent(
            account_id=acct.id, kind="news", source="test", title="Old news", strength=0.5,
            dedupe_key="s2", occurred_at=datetime.now(timezone.utc) - timedelta(days=400),
        ))
        await ts.flush()
        assert await tiering.classify(ts, acct, new_signals=[]) == tiering.COLD


async def test_an_account_in_an_active_cadence_stays_hot(fresh_db):
    """A rep is emailing this company today. Whatever the signal history says, it must not drop
    to a three-day crawl cycle."""
    from nexus.ingestion import tiering
    from nexus.models.cadence import Cadence, CadenceEnrollment, ENROLL_ACTIVE
    from nexus.models.campaign import Campaign
    from nexus.models.workflow import ProspectList
    from nexus.workers.tasks import tenant_session

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        lst = ProspectList(name="Targets")
        cad = Cadence(name="Outbound")
        ts.add_all([lst, cad])
        await ts.flush()
        camp = Campaign(name="Q3 push", list_id=lst.id)
        ts.add(camp)
        await ts.flush()
        ts.add(CadenceEnrollment(
            cadence_id=cad.id, campaign_id=camp.id, account_id=acct.id, status=ENROLL_ACTIVE,
            next_touch_at=datetime.now(timezone.utc) + timedelta(days=1),
            started_at=datetime.now(timezone.utc),
        ))
        await ts.flush()
        assert await tiering.classify(ts, acct, new_signals=[]) == tiering.HOT


async def test_an_account_on_a_list_stays_hot(fresh_db):
    """Somebody deliberately put it there. That is an explicit statement of interest."""
    from nexus.ingestion import tiering
    from nexus.models.workflow import ListItem, ProspectList
    from nexus.workers.tasks import tenant_session

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        lst = ProspectList(name="Q3 targets")
        ts.add(lst)
        await ts.flush()
        ts.add(ListItem(list_id=lst.id, account_id=acct.id))
        await ts.flush()
        assert await tiering.classify(ts, acct, new_signals=[]) == tiering.HOT


async def test_an_untouched_account_goes_cold(fresh_db):
    from nexus.ingestion import tiering
    from nexus.workers.tasks import tenant_session

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        assert await tiering.classify(ts, acct, new_signals=[]) == tiering.COLD


async def test_a_classification_failure_falls_back_to_hot(fresh_db, monkeypatch):
    """A bookkeeping problem must not be able to quietly stop crawling an account. Hot is the
    pre-tiering behaviour, so failing that way changes nothing."""
    from nexus.ingestion import tiering
    from nexus.workers.tasks import tenant_session

    async def _boom(*a, **k):
        raise RuntimeError("db is unhappy")

    monkeypatch.setattr(tiering, "_has_recent_signal", _boom)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        assert await tiering.classify(ts, acct, new_signals=[]) == tiering.HOT


async def test_cold_is_scheduled_further_out_than_hot(fresh_db):
    from nexus.core.config import get_settings
    from nexus.ingestion import tiering

    settings = get_settings()
    assert tiering.interval_for(tiering.COLD) > tiering.interval_for(tiering.HOT)
    assert tiering.interval_for(tiering.HOT) == settings.account_refresh_interval_s
    assert tiering.interval_for(tiering.COLD) == settings.account_refresh_interval_cold_s


# ---- the claim ------------------------------------------------------------------------------

async def test_the_claim_only_takes_accounts_that_are_due(fresh_db, monkeypatch):
    from nexus.core.config import get_settings
    from nexus.models.identity import Tenant
    from nexus.workers.queue import InMemoryTaskQueue, set_task_queue
    from nexus.workers.tasks import handle_refresh_due_accounts, tenant_session

    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    set_task_queue(InMemoryTaskQueue())
    now = datetime.now(timezone.utc)

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        t = await ts.session.get(Tenant, tid)
        t.automation_enabled = True
        due = await _account(ts, domain="due.com", name="Due")
        due.next_refresh_at = now - timedelta(hours=1)
        not_due = await _account(ts, domain="later.com", name="Later")
        not_due.next_refresh_at = now + timedelta(hours=5)
        await ts.flush()

    result = await handle_refresh_due_accounts({"now_iso": now.isoformat()})
    assert result["accounts"] == 1


async def test_the_claim_reschedules_so_a_lost_job_cannot_stall_an_account(fresh_db, monkeypatch):
    """The claim stamps a conservative 6h default BEFORE the pipeline runs. If processing never
    completes, the account returns on the old cycle rather than never being looked at again."""
    from nexus.core.config import get_settings
    from nexus.models.account import Account
    from nexus.models.identity import Tenant
    from nexus.workers.queue import InMemoryTaskQueue, set_task_queue
    from nexus.workers.tasks import handle_refresh_due_accounts, tenant_session

    settings = get_settings()
    monkeypatch.setattr(settings, "automation_enabled", True)
    set_task_queue(InMemoryTaskQueue())
    now = datetime.now(timezone.utc)

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        t = await ts.session.get(Tenant, tid)
        t.automation_enabled = True
        acct = await _account(ts)
        acct.next_refresh_at = now - timedelta(hours=1)
        await ts.flush()
        aid = acct.id

    await handle_refresh_due_accounts({"now_iso": now.isoformat()})

    async with tenant_session(tid) as ts:
        after = await ts.get(Account, aid)
        expected = now + timedelta(seconds=settings.account_refresh_interval_s)
        assert abs((after.next_refresh_at - expected).total_seconds()) < 2
        # Claimed, so a second tick at the same instant must not pick it up again.
        assert after.next_refresh_at > now

    second = await handle_refresh_due_accounts({"now_iso": now.isoformat()})
    assert second["accounts"] == 0


async def test_a_new_account_is_due_immediately(fresh_db):
    """next_refresh_at is NOT NULL defaulting to now, so a brand-new account is picked up on the
    next tick — exactly what a NULL last_refreshed_at used to mean."""
    from nexus.workers.tasks import tenant_session

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        assert acct.next_refresh_at is not None
        assert acct.next_refresh_at <= datetime.now(timezone.utc) + timedelta(seconds=2)
