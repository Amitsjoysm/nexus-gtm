# tests/test_rate_card_conformance.py
"""Every rate card charges the same way: quantity x credits_per_unit, once, with no second price.

This walks the WHOLE seeded rate card rather than sampling it, because the failures this suite
exists to catch have all been per-capability rather than systemic: `ai.scoring` ran 4,090 times
unbilled, `verify.email` cost 0.25 in plan and 1.00 past an invisible line, `enrich.contact` on
`core` cost more inside its quota than outside it, and a discovery sweep billed 40 candidates
through a capability meant for the 20 accounts it delivered.

Three properties, asserted for every priced capability:

1. **Uniform.** The burn is exactly `quantity x credits_per_unit`, at any quantity, whatever side
   of a quota it falls on. One price per request.
2. **Exactly once.** A retry carrying the same idempotency key charges nothing further.
3. **No overage.** There is no second, higher price for crossing a line. `overage_price_credits`
   must not reach the in-flight charge, and a usage invoice must not re-bill anything already
   paid for in credits.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session


def _priced_actions() -> list[tuple[str, float]]:
    """Every capability with a live flat price that represents an ACTION.

    Gauges are excluded on purpose: `seat.member` and `platform.storage` resolve to a live count
    rather than to something somebody did, so there is no request to price. They keep a plan limit
    and are never charged in credits.
    """
    from nexus.billing.catalog import CAPABILITY_SEED
    from nexus.billing.rates import RATE_SEED

    kinds = {c["id"]: c.get("meter_kind", "counter") for c in CAPABILITY_SEED}
    modes = {c["id"]: c.get("default_mode", "shadow") for c in CAPABILITY_SEED}
    out = []
    for row in RATE_SEED:
        cid = row["capability_id"]
        price = float(row.get("credits_per_unit") or 0)
        if cid.startswith("module.") or price <= 0:
            continue
        if kinds.get(cid) == "gauge":
            continue
        if modes.get(cid) == "shadow":
            continue        # observe-only by catalog; see OBSERVE_ONLY below
        if row.get("tiers"):
            continue        # a ladder prices by position; not a flat card
        out.append((cid, price))
    return sorted(out)


# Priced, but catalogued `default_mode="shadow"` — recorded and never charged. Each carries a
# price so margin is measurable, and each is paid for through a parent action rather than on its
# own. Listed here so the set is a decision somebody made rather than an accident, and so a new
# one cannot join it silently: `test_the_observe_only_set_is_exactly_this` fails if it does.
#
# `ai.scoring` shipped this way once by accident — 4,090 runs, free — which is why the set is
# pinned rather than trusted.
OBSERVE_ONLY = {
    "search.web": "inside the capability that issued the search",
    "signal.news_scan": "inside the account refresh that ran it",
    "signal.rss_scan": "inside the account refresh that ran it",
    "signal.stored": "carried by module.signals; storing a signal is not a request",
    "inbox.task": "carried by module.signals; created by ingestion, not requested",
    "ai.tokens": "recorded alongside the flat per-action charge, never instead of it",
    "ai.scoring": "the relevance score is a column on a page every plan includes",
    "notify.in_app": "telling someone about work already billed is not a second sale",
    "workflow.orchestration_step": "inside workflow.orchestration_run",
    "automation.account_refresh": "the sweep bills the capabilities it invokes",
    "api.request": "no public API surface ships yet",
}


def test_the_observe_only_set_is_exactly_this():
    """A priced capability that charges nothing must be a deliberate, named exception."""
    from nexus.billing.catalog import CAPABILITY_SEED
    from nexus.billing.rates import RATE_SEED

    kinds = {c["id"]: c.get("meter_kind", "counter") for c in CAPABILITY_SEED}
    modes = {c["id"]: c.get("default_mode", "shadow") for c in CAPABILITY_SEED}
    actual = {
        r["capability_id"] for r in RATE_SEED
        if not r["capability_id"].startswith("module.")
        and float(r.get("credits_per_unit") or 0) > 0
        and kinds.get(r["capability_id"]) != "gauge"
        and modes.get(r["capability_id"]) == "shadow"
    }
    new = sorted(actual - set(OBSERVE_ONLY))
    gone = sorted(set(OBSERVE_ONLY) - actual)
    assert not new, (
        f"these carry a price and charge nothing, with no reason recorded: {new}. "
        "Either charge them, or add them to OBSERVE_ONLY saying which action pays for them."
    )
    assert not gone, f"OBSERVE_ONLY names capabilities that are no longer observe-only: {gone}"


@pytest.fixture
def enforcing(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    return get_settings()


async def _tenant_with_credits(plan_id: str = "launch", credits: float = 5_000_000):
    """A tenant on a REAL plan with a large balance.

    `unlimited`/`internal`/`partner` classes return before the credit path and never burn, so a
    conformance test against one would pass no matter what the engine did.
    """
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.credits import grant_credits
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        await ts.flush()
        await grant_credits(ts, credits, reason="conformance", idempotency_key="grant")
    return tid


async def test_every_priced_action_burns_exactly_its_rate_card(enforcing):
    """The headline property, over the entire card."""
    from nexus.billing.credits import balance
    from nexus.billing.entitlements import check_and_meter

    priced = _priced_actions()
    assert len(priced) > 20, f"only {len(priced)} priced actions found; the seed did not load"

    tid = await _tenant_with_credits()
    wrong: list[str] = []
    async with tenant_session(tid) as ts:
        for cid, price in priced:
            before = await balance(ts)
            res = await check_and_meter(
                ts, capability_id=cid, quantity=1, idempotency_key=f"one:{cid}"
            )
            spent = before - await balance(ts)
            if not res.allowed:
                wrong.append(f"{cid}: blocked ({res.reason}) on a funded plan")
            elif abs(spent - price) > 1e-6:
                wrong.append(f"{cid}: card says {price}, burned {spent}")
    assert not wrong, "rate card not honoured:\n  " + "\n  ".join(wrong)


async def test_the_price_is_linear_in_quantity(enforcing):
    """N units cost N times one unit — no step, no discount, no penalty at any boundary."""
    from nexus.billing.credits import balance
    from nexus.billing.entitlements import check_and_meter

    tid = await _tenant_with_credits()
    wrong: list[str] = []
    async with tenant_session(tid) as ts:
        for cid, price in _priced_actions():
            for qty in (1, 7, 50):
                before = await balance(ts)
                await check_and_meter(
                    ts, capability_id=cid, quantity=qty, idempotency_key=f"lin:{cid}:{qty}"
                )
                spent = before - await balance(ts)
                if abs(spent - price * qty) > 1e-6:
                    wrong.append(f"{cid} x{qty}: expected {price * qty}, burned {spent}")
    assert not wrong, "price is not linear in quantity:\n  " + "\n  ".join(wrong)


async def test_a_retry_with_the_same_key_charges_once(enforcing):
    """Exactly once. A retried request re-derives the same charge and must not pay twice."""
    from nexus.billing.credits import balance
    from nexus.billing.entitlements import check_and_meter

    tid = await _tenant_with_credits()
    wrong: list[str] = []
    async with tenant_session(tid) as ts:
        for cid, _price in _priced_actions():
            key = f"retry:{cid}"
            before = await balance(ts)
            await check_and_meter(ts, capability_id=cid, quantity=3, idempotency_key=key)
            after_first = await balance(ts)
            for _ in range(3):
                await check_and_meter(ts, capability_id=cid, quantity=3, idempotency_key=key)
            total = before - await balance(ts)
            once = before - after_first
            if abs(total - once) > 1e-6:
                wrong.append(f"{cid}: 4 attempts on one key burned {total}, one costs {once}")
    assert not wrong, "a retry was charged again:\n  " + "\n  ".join(wrong)


async def test_crossing_a_quota_does_not_change_the_price(enforcing):
    """No overage. The unit past the line costs what the unit before it cost.

    This is the property the retired ladder broke in both directions: `verify.email` charged 4x
    more past its allowance, and `enrich.contact` on `core` charged LESS, so overflowing was the
    rational move.
    """
    from sqlalchemy import select

    from nexus.billing.credits import balance
    from nexus.billing.entitlements import check_and_meter
    from nexus.billing.usage import record_usage
    from nexus.models.billing import BillingPlanEntitlement

    tid = await _tenant_with_credits()
    async with tenant_session(tid) as ts:
        row = (
            await ts.session.scalars(
                select(BillingPlanEntitlement).where(
                    BillingPlanEntitlement.plan_id == "launch",
                    BillingPlanEntitlement.capability_id == "ai.email_draft",
                )
            )
        ).first()
        quota = (row.quota if row is not None else None) or 20

        before_in = await balance(ts)
        await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="in-plan")
        in_plan = before_in - await balance(ts)

        await record_usage(ts, capability_id="ai.email_draft", quantity=quota * 3,
                           idempotency_key="push-past-the-line")

        before_out = await balance(ts)
        await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="past-quota")
        past_quota = before_out - await balance(ts)

    assert in_plan == past_quota, (
        f"the same action costs {in_plan} inside the quota and {past_quota} past it"
    )


def test_no_second_price_reaches_the_in_flight_charge():
    """`overage_price_credits` is plan data that must not price a request.

    Structural, because the failure is a line of code rather than a behaviour: the moment the
    burn consults it again, one action has two prices depending on where in the period it lands.
    """
    import inspect

    from nexus.billing import entitlements

    burn = inspect.getsource(entitlements._burn_for_usage)
    assert "overage_price_credits" not in burn, (
        "the credit burn reads overage_price_credits again — that is the second price"
    )
    assert "credits_per_unit" in burn or "tiered_credits" in burn


async def test_a_usage_invoice_never_re_bills_a_credit_paid_action(enforcing):
    """Paid at the moment of use, so a usage invoice may only bill gauges."""
    from sqlalchemy import select

    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key
    from nexus.billing.usage import record_usage
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingCapability, BillingInvoiceLine

    tid = await _tenant_with_credits()
    pk = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await record_usage(ts, capability_id="ai.email_draft", quantity=10_000,
                           idempotency_key="huge")
        invoice = await rate_period(ts, period_key=pk)
        lines = (
            await ts.session.scalars(
                select(BillingInvoiceLine).where(BillingInvoiceLine.invoice_id == invoice.id)
            )
        ).all()
        gauges = {
            c.id for c in (await ts.session.scalars(select(BillingCapability))).all()
            if c.meter_kind == "gauge"
        }
    offenders = [
        ln.capability_id for ln in lines
        if ln.kind == "overage" and ln.capability_id not in gauges
    ]
    assert not offenders, (
        f"a usage invoice re-billed credit-paid actions: {offenders} — the customer pays twice"
    )
