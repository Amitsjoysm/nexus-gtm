"""Revenue reporting: MRR, ARR, movement, collection health (M23).

`grep -riE "\\bmrr\\b|churn|ltv"` returned nothing across the codebase before this. A commercial
platform that cannot state its own recurring revenue is unfinished, however correct its metering is.

Everything here is **derived at read time** from subscriptions, plans and invoices. A stored MRR
figure would be a second source of truth that drifts from the rows it describes, and reconciling the
two becomes somebody's month-end job forever.
"""
from __future__ import annotations

from nexus.billing.revenue import monthly_cents, movement
from tests.conftest import make_tenant, tenant_session


class _Plan:
    def __init__(self, base, interval="month"):
        self.base_price_cents = base
        self.interval = interval


# ---- normalisation ------------------------------------------------------------------------------

def test_monthly_plans_contribute_their_price():
    assert monthly_cents(_Plan(7900)) == 7900


def test_annual_plans_are_divided_by_twelve():
    """Counting an annual plan whole makes MRR jump by twelve months of revenue in the month a
    customer signs, then fall back the next — which is not what MRR means."""
    assert monthly_cents(_Plan(120_000, "year")) == 10_000


def test_money_stays_in_whole_cents():
    """Integer division, never floats: a fraction of a cent in MRR compounds into a number that
    does not reconcile against the invoices."""
    value = monthly_cents(_Plan(100_000, "year"))
    assert isinstance(value, int)
    assert value == 8333


def test_a_missing_plan_contributes_nothing():
    """A subscription pointing at a deleted plan must not crash the report."""
    assert monthly_cents(None) == 0


# ---- movement -----------------------------------------------------------------------------------

def test_movement_categories_sum_back_to_the_net_change():
    """The property that makes the report checkable:
    net = new + expansion - contraction - churned."""
    previous = {"a": 10_000, "b": 5_000, "c": 2_000}
    current = {"a": 12_000, "b": 3_000, "d": 8_000}     # a expands, b contracts, c churns, d new
    m = movement(previous, current)

    assert m["new_cents"] == 8_000
    assert m["expansion_cents"] == 2_000
    assert m["contraction_cents"] == 2_000
    assert m["churned_cents"] == 2_000
    assert m["net_cents"] == 8_000 + 2_000 - 2_000 - 2_000
    assert sum(current.values()) - sum(previous.values()) == m["net_cents"]


def test_expansion_and_contraction_are_not_netted_against_each_other():
    """A month where one customer doubled and another halved is not a quiet month, and reporting
    only the net would hide both."""
    m = movement({"a": 10_000, "b": 10_000}, {"a": 20_000, "b": 0})
    assert m["expansion_cents"] == 10_000
    assert m["contraction_cents"] == 10_000


def test_logo_churn_is_a_share_of_the_starting_population():
    m = movement({"a": 100, "b": 100, "c": 100, "d": 100}, {"a": 100, "b": 100, "c": 100})
    assert m["churned_tenants"] == 1
    assert m["logo_churn_rate"] == 0.25


def test_churn_from_an_empty_base_is_zero_not_a_crash():
    """Dividing by an empty starting population is either an exception or a meaningless 100%."""
    assert movement({}, {"a": 100})["logo_churn_rate"] == 0.0
    assert movement({}, {})["net_cents"] == 0


# ---- snapshot -----------------------------------------------------------------------------------

async def _subscribe(slug: str, plan_id: str, status: str):
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingSubscription

    await sync_plans()
    tid = await make_tenant(slug=slug)
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status=status))
        await ts.flush()
    return tid


async def test_mrr_counts_paying_subscriptions():
    from nexus.billing.revenue import snapshot

    await _subscribe("rev1", "growth", "active")
    snap = await snapshot()
    assert snap.paying_tenants >= 1
    assert snap.mrr_cents > 0
    assert snap.arr_cents == snap.mrr_cents * 12


async def test_a_trial_is_a_live_logo_and_zero_revenue():
    """A trial has not paid anything yet. Counting it in MRR inflates the number with revenue that
    may never arrive."""
    from nexus.billing.revenue import snapshot

    before = await snapshot()
    await _subscribe("rev2", "growth", "trialing")
    after = await snapshot()

    assert after.trialing_tenants == before.trialing_tenants + 1
    assert after.mrr_cents == before.mrr_cents
    assert after.paying_tenants == before.paying_tenants


async def test_past_due_still_counts_as_revenue():
    """The debt is real and still owed. Dropping it would make a dunning problem look like churn,
    which is a different decision entirely."""
    from nexus.billing.revenue import snapshot

    before = await snapshot()
    await _subscribe("rev3", "growth", "active")
    baseline = await snapshot()
    contribution = baseline.mrr_cents - before.mrr_cents

    await _subscribe("rev4", "growth", "past_due")
    after = await snapshot()
    assert after.past_due_tenants >= 1
    assert after.mrr_cents == baseline.mrr_cents + contribution


async def test_a_canceled_subscription_contributes_nothing():
    from nexus.billing.revenue import snapshot

    before = await snapshot()
    await _subscribe("rev5", "growth", "canceled")
    after = await snapshot()
    assert after.mrr_cents == before.mrr_cents


async def test_the_plan_mix_is_reported():
    from nexus.billing.revenue import snapshot

    await _subscribe("rev6", "starter", "active")
    snap = await snapshot()
    assert "starter" in snap.by_plan
    tenants, mrr = snap.by_plan["starter"]
    assert tenants >= 1 and mrr > 0


async def test_the_snapshot_never_raises(monkeypatch):
    """A reporting failure must not take down the admin console that surfaces it."""
    import nexus.billing.revenue as revenue

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(revenue, "get_platform_sessionmaker", boom, raising=False)
    snap = await revenue.snapshot()
    assert snap.mrr_cents == 0


# ---- collection health --------------------------------------------------------------------------

async def test_an_empty_period_has_a_full_collection_rate():
    """Reporting 0% for a period that invoiced nothing would raise an alarm about no activity at
    all — which is a different problem from failing to collect."""
    from nexus.billing.revenue import CollectionHealth

    assert CollectionHealth().collection_rate == 1.0


def test_collection_rate_is_paid_over_invoiced():
    from nexus.billing.revenue import CollectionHealth

    health = CollectionHealth(invoiced_cents=10_000, paid_cents=7_500)
    assert health.collection_rate == 0.75
    assert health.as_dict()["collection_rate"] == 0.75


async def test_collection_health_reads_finalized_invoices_only():
    """Draft invoices have not been presented to anyone; counting them as uncollected makes every
    open period look like a collection failure."""
    from nexus.billing.revenue import collection_health
    from nexus.models.billing import BillingInvoice

    tid = await make_tenant(slug="rev7")
    async with tenant_session(tid) as ts:
        ts.add(BillingInvoice(tenant_id=tid, period_key="2026-07", status="draft",
                              total_cents=50_000))
        ts.add(BillingInvoice(tenant_id=tid, period_key="2026-06", status="paid",
                              total_cents=10_000))
        await ts.flush()

    health = await collection_health()
    assert health.paid_cents >= 10_000
    assert health.invoiced_cents < 50_000 + 10_000     # the draft is excluded


async def test_a_voided_invoice_is_neither_invoiced_nor_outstanding():
    """It was never owed."""
    from nexus.billing.revenue import collection_health
    from nexus.models.billing import BillingInvoice

    before = await collection_health()
    tid = await make_tenant(slug="rev8")
    async with tenant_session(tid) as ts:
        ts.add(BillingInvoice(tenant_id=tid, period_key="2026-05", status="void",
                              total_cents=99_000))
        await ts.flush()
    after = await collection_health()
    assert after.invoiced_cents == before.invoiced_cents
