# tests/test_llm_fallback_visibility.py
"""A stub answer must be distinguishable from a real one — and must not be billed.

`FallbackLLMProvider` logs a warning and returns the next provider's output. `LLMResponse`
carried nothing to say which provider served it, so every downstream caller treated stub output
as a genuine model answer. Measured on the live deployment 2026-09-03: Groq rate-limited (429)
during an account enrichment, the chain fell through to the stub, the stub extracted no
firmographics, and the customer was charged 3 credits for an empty answer — twice within eleven
seconds, once for a background sweep and once for their own click. The account was left with no
country, which is in turn why an Indian company can end up scored against a US ICP.

Two properties close it:

* **The response says who served it.** A caller that needs to know can ask, and the metric makes
  a silent degradation visible before a customer reports it.
* **Work the stub served is not billed.** "Charge for the answer, not for our infrastructure" is
  this package's rule, and a stub answer is not an answer — it is our outage wearing one. A real
  model saying "I found nothing" is different and IS billed, because the customer bought a
  lookup and got its true result.
"""
from __future__ import annotations


from nexus.agents.llm import (
    FallbackLLMProvider,
    LLMMessage,
    LLMResponse,
    StubLLMProvider,
)


class _Boom:
    """A provider that always fails, like a rate-limited key pool."""

    async def complete(self, messages, **kw):
        raise RuntimeError("429 Too Many Requests")


class _Real:
    async def complete(self, messages, **kw):
        return LLMResponse(text='{"industry": "Fintech"}', tokens=12)


async def test_a_response_names_the_provider_that_served_it():
    chain = FallbackLLMProvider([_Real()])
    out = await chain.complete([LLMMessage("user", "hi")])
    assert getattr(out, "provider", None) == "_Real"


async def test_a_fallback_to_the_stub_is_marked_as_such():
    chain = FallbackLLMProvider([_Boom(), StubLLMProvider()])
    out = await chain.complete([LLMMessage("user", "hi")], purpose="account_enrich")
    assert out.provider == "StubLLMProvider"
    assert out.is_stub is True, "stub output is indistinguishable from a real answer"


async def test_a_real_answer_is_not_marked_stub():
    chain = FallbackLLMProvider([_Real(), StubLLMProvider()])
    out = await chain.complete([LLMMessage("user", "hi")])
    assert out.is_stub is False


async def test_the_fallback_is_counted():
    """A silent degradation is the failure mode this whole subsystem keeps having."""
    from nexus.core import metrics

    seen = []
    original = getattr(metrics, "record_llm_fallback", None)
    assert original is not None, "no metrics hook for LLM fallback"
    metrics.record_llm_fallback = lambda **kw: seen.append(kw)
    try:
        chain = FallbackLLMProvider([_Boom(), StubLLMProvider()])
        await chain.complete([LLMMessage("user", "hi")], purpose="account_enrich")
    finally:
        metrics.record_llm_fallback = original
    assert seen, "falling through to the stub emitted no metric"
    assert seen[-1].get("to") == "StubLLMProvider"
    assert seen[-1].get("purpose") == "account_enrich"


async def test_enrichment_served_by_the_stub_is_not_billed():
    """The customer must not pay for our provider outage."""
    from sqlalchemy import select

    from nexus.enrichment.account import SearchBackedAccountEnricher
    from nexus.models.account import Account
    from nexus.models.billing import BillingUsageEvent
    from tests.conftest import make_tenant, tenant_session

    class _Search:
        async def search(self, q, *, limit=5):
            return [type("H", (), {"title": "Zerodha", "snippet": "brokerage", "url": "u"})()]

    tid = await make_tenant()
    enricher = SearchBackedAccountEnricher(
        _Search(), FallbackLLMProvider([_Boom(), StubLLMProvider()])
    )
    async with tenant_session(tid) as ts:
        account = Account(tenant_id=tid, name="Zerodha", domain="zerodha.com")
        ts.add(account)
        await ts.flush()
        await enricher.enrich(ts, account, force=True)

        rows = (
            await ts.session.scalars(
                select(BillingUsageEvent).where(
                    BillingUsageEvent.tenant_id == tid,
                    BillingUsageEvent.capability_id == "enrich.account",
                )
            )
        ).all()
        net = sum(float(r.quantity) for r in rows)
        assert net == 0, (
            f"charged {net} unit(s) for an enrichment the stub served — that is our outage, "
            "billed to the customer"
        )
