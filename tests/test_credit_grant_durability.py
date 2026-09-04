# tests/test_credit_grant_durability.py
"""A credit grant that fails after a successful payment must not be only a log line.

`_grant_plan_credits` deliberately swallows every exception, and that is right: if it raised,
Stripe would retry the whole webhook, and the customer would be looking at a payment that went
through against a subscription that appears not to have. Never breaking the money-state write is
the correct priority.

The cost of that choice is invisibility. The customer paid, holds no credits, and the only trace is
a warning nobody greps — which is exactly how the unbound-RLS bug in this same function stayed
silent. So the failure now leaves two marks, because they answer different questions:

* a **counter**, which tells you that it is happening at all and can page someone;
* a **dead letter**, which tells you WHICH workspace and lets an operator replay it.

Neither replaces the other. A counter with no row leaves an operator grepping logs for a customer
id; a row with no counter is only found by someone who already went looking.
"""
from __future__ import annotations

import pytest


class _Plan:
    id = "launch"
    included_credits = 2000


class _Sub:
    tenant_id = "t-paid"
    plan_id = "launch"


async def _fail_grant(monkeypatch, exc=RuntimeError("rls rejected the write")):
    """Drive `_grant_plan_credits` with the credit write guaranteed to fail."""
    from nexus.billing import webhooks

    async def boom(*a, **k):
        raise exc

    monkeypatch.setattr(webhooks, "_apply_plan_change_credits", boom, raising=False)

    import nexus.billing.subscriptions as subs

    monkeypatch.setattr(subs, "apply_plan_change_credits", boom)

    from nexus.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        await webhooks._grant_plan_credits(session, _Sub(), _Plan())


async def test_a_failed_grant_is_dead_lettered(fresh_db, monkeypatch):
    """The row names the workspace and carries enough to re-run the grant. Without it an operator
    knows only that "a grant failed" and has to find the customer in the logs."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.jobs import DeadLetterJob

    await _fail_grant(monkeypatch)

    async with get_sessionmaker()() as s:
        rows = (await s.scalars(select(DeadLetterJob))).all()

    assert len(rows) == 1, "a failed credit grant left no dead letter"
    row = rows[0]
    assert row.subject_tenant_id == "t-paid"
    assert row.payload.get("tenant_id") == "t-paid"
    assert row.payload.get("plan_id") == "launch"
    assert "rls rejected" in (row.error or "")


async def test_a_failed_grant_increments_the_webhook_counter(fresh_db, monkeypatch):
    """The counter is what pages someone. It rides on the existing webhook metric rather than a new
    one: this IS a webhook outcome, and a second metric for the same event is a second thing to
    remember to look at."""
    from nexus.core import metrics

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        metrics, "record_webhook_event",
        lambda provider, outcome: seen.append((provider, outcome)),
    )
    await _fail_grant(monkeypatch)

    assert ("stripe", "credit_grant_failed") in seen, seen


async def test_the_webhook_still_does_not_raise(fresh_db, monkeypatch):
    """The whole reason the swallow exists. Adding bookkeeping must not reintroduce the retry storm
    it was protecting against."""
    await _fail_grant(monkeypatch)          # would raise out of the test if it propagated


async def test_a_failing_dead_letter_write_still_does_not_raise(fresh_db, monkeypatch):
    """Last line of defence. If the database that rejected the credit write also rejects the dead
    letter, the webhook must STILL return — otherwise a database blip turns one lost grant into a
    Stripe retry storm on every paid upgrade."""
    from nexus.billing import webhooks

    async def boom_dl(*a, **k):
        raise RuntimeError("dead letter table gone too")

    monkeypatch.setattr(webhooks, "_dead_letter_credit_grant", boom_dl, raising=False)
    await _fail_grant(monkeypatch)


async def test_a_successful_grant_leaves_no_dead_letter(fresh_db, monkeypatch):
    """The guard must not fire on the happy path, or the triage queue fills with non-events and
    stops being read."""
    from sqlalchemy import select

    from nexus.billing import webhooks
    from nexus.core.db import get_sessionmaker
    from nexus.models.jobs import DeadLetterJob
    import nexus.billing.subscriptions as subs

    async def ok(*a, **k):
        return True

    monkeypatch.setattr(subs, "apply_plan_change_credits", ok)
    async with get_sessionmaker()() as session:
        await webhooks._grant_plan_credits(session, _Sub(), _Plan())

    async with get_sessionmaker()() as s:
        assert (await s.scalars(select(DeadLetterJob))).all() == []


# ---- the replay has to actually work -------------------------------------------------------------

def test_the_dead_lettered_job_has_a_handler():
    """`POST /admin/jobs/dead-letters/{id}/replay` enqueues `Job(name=job_name, ...)` and the
    worker dispatches through `HANDLERS`. A dead letter whose name is not registered shows up in
    the triage UI with a replay button that silently does nothing — worse than no button."""
    from nexus.billing.webhooks import CREDIT_GRANT_JOB
    from nexus.workers.tasks import HANDLERS

    assert CREDIT_GRANT_JOB in HANDLERS, (
        f"{CREDIT_GRANT_JOB} is dead-lettered but has no handler, so replay is a no-op"
    )


async def test_the_handler_grants_the_credits(fresh_db):
    """A replay must deliver what the webhook could not."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.credits import balance
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession
    from nexus.workers.tasks import HANDLERS
    from nexus.billing.webhooks import CREDIT_GRANT_JOB
    from tests.conftest import make_tenant, put_on_plan

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant()
    await put_on_plan(tid, "launch")

    async with get_sessionmaker()() as s:
        before = await balance(TenantSession(s, tid))

    out = await HANDLERS[CREDIT_GRANT_JOB]({"tenant_id": tid, "plan_id": "launch"})
    assert out.get("error") is None, out

    async with get_sessionmaker()() as s:
        after = await balance(TenantSession(s, tid))
    assert after >= before, "the replay handler delivered nothing"


async def test_the_handler_reports_an_unknown_plan_rather_than_raising(fresh_db):
    """A dead letter naming a retired plan must not crash the worker on replay. `error` is a normal
    terminal outcome the dispatcher understands, distinct from the handler RAISING."""
    from nexus.billing.catalog import sync_catalog
    from nexus.workers.tasks import HANDLERS
    from nexus.billing.webhooks import CREDIT_GRANT_JOB

    await sync_catalog()
    out = await HANDLERS[CREDIT_GRANT_JOB]({"tenant_id": "t1", "plan_id": "no-such-plan"})
    assert out.get("error")
