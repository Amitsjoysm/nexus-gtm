# tests/test_billing_cost_context.py
from __future__ import annotations

import asyncio


def test_report_cost_outside_a_scope_is_a_noop():
    """Infrastructure must never crash because nothing is metering it."""
    from nexus.billing.context import report_cost

    report_cost(usd=0.01, tokens=100)      # must not raise


def test_cost_scope_accumulates():
    from nexus.billing.context import cost_scope, report_cost

    with cost_scope() as cost:
        report_cost(usd=0.004, tokens=400)
        report_cost(usd=0.006, tokens=600)
    assert round(cost.usd, 6) == 0.01
    assert cost.tokens == 1000
    assert cost.calls == 2


def test_nested_scopes_do_not_leak_into_the_parent():
    """An inner scope owns its own costs; the parent keeps only its own."""
    from nexus.billing.context import cost_scope, report_cost

    with cost_scope() as outer:
        report_cost(usd=0.001)
        with cost_scope() as inner:
            report_cost(usd=0.002)
        assert round(inner.usd, 6) == 0.002
    assert round(outer.usd, 6) == 0.001


async def test_concurrent_tasks_get_independent_scopes():
    """ContextVar, not a global: two requests in flight must not pool their costs."""
    from nexus.billing.context import cost_scope, report_cost

    async def one(amount: float) -> float:
        with cost_scope() as cost:
            report_cost(usd=amount)
            await asyncio.sleep(0.01)      # force interleaving
            report_cost(usd=amount)
            return cost.usd

    a, b = await asyncio.gather(one(0.001), one(0.010))
    assert round(a, 6) == 0.002
    assert round(b, 6) == 0.020


async def test_llm_calls_report_their_cost_into_the_active_scope():
    """Cost is measured from the provider's own token count, not guessed at the endpoint."""
    from nexus.agents.llm import CostTrackingProvider, LLMMessage, StubLLMProvider
    from nexus.billing.context import cost_scope

    inner = StubLLMProvider()
    provider = CostTrackingProvider(inner, usd_per_1k_tokens=0.002)
    with cost_scope() as cost:
        resp = await provider.complete([LLMMessage(role="user", content="hi there")])
    assert resp.text                       # delegation is transparent
    assert cost.calls == 1
    assert cost.usd == resp.tokens / 1000 * 0.002


async def test_cost_tracking_provider_is_transparent_without_a_scope():
    from nexus.agents.llm import CostTrackingProvider, LLMMessage, StubLLMProvider

    provider = CostTrackingProvider(StubLLMProvider(), usd_per_1k_tokens=0.002)
    resp = await provider.complete([LLMMessage(role="user", content="hi")])
    assert resp.text                       # no scope, no crash


def test_llm_cost_rate_is_configurable():
    """Nothing about price or cost is hardcoded — it is settings, like every other rate."""
    from nexus.core.config import get_settings

    assert hasattr(get_settings(), "llm_usd_per_1k_tokens")
