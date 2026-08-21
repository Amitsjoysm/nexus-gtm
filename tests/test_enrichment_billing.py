# tests/test_enrichment_billing.py
"""`enrich.account` and `enrich.contact`, now charged where they are spent.

Both capabilities sat in the catalog with a rate card and a plan entitlement, metered at **no call
site**, for most of this project's life. Enrichment is the most expensive thing the product does
per unit — a search request, an LLM completion, a verification credit, sometimes an actor run —
and none of it reached the usage stream. These tests pin the wiring, and more importantly they pin
the three ways wiring it up could have made things worse:

1. **A quota must not take down the sweep it runs inside.** The account-refresh pipeline, ICP
   discovery and lookalike search all enrich. If a 402 escaped into those, an enrichment limit
   would stop signal collection — the product's whole job — for a tenant who is merely thrifty.
2. **A batch must not meter inside its own `gather`.** Metering touches the TenantSession, and
   SQLAlchemy's AsyncSession is not safe for concurrent use. One charge for the batch, taken
   before the concurrency starts.
3. **A free answer is still billable, and still marked.** A registered source database changes our
   COGS, not the customer's price.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, make_tenant, signup, tenant_session

from nexus.enrichment.account import ACCOUNT_CAPABILITY, SearchBackedAccountEnricher
from nexus.enrichment.waterfall import CONTACT_CAPABILITY
from nexus.models.account import Account, Contact


async def _seed_billing():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()


async def _usage(tenant_id: str, capability: str) -> list:
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingUsageEvent

    async with get_platform_sessionmaker()() as s:
        return list((await s.scalars(
            select(BillingUsageEvent).where(
                BillingUsageEvent.tenant_id == tenant_id,
                BillingUsageEvent.capability_id == capability,
            )
        )).all())


def _enricher(fetched: dict | None = None) -> SearchBackedAccountEnricher:
    """An account enricher whose paid half is a stub, so the test is about billing, not the web."""
    enricher = SearchBackedAccountEnricher(search=None, llm=None)

    async def _fetch(account):
        return dict(fetched or {})

    enricher.fetch = _fetch  # type: ignore[method-assign]
    return enricher


# ---- enrich.account -----------------------------------------------------------------------------

async def test_a_web_enrichment_is_charged():
    """The capability was catalogued and priced from the start; it simply reached no call site."""
    await _seed_billing()
    tid = await make_tenant(slug="eb1", name="EB One")
    account = Account(tenant_id=tid, name="Northwind", domain="northwind.com")

    async with tenant_session(tid) as ts:
        filled = await _enricher({"industry": "Logistics", "employee_count": 1200}).enrich(
            ts, account
        )
        await ts.session.commit()

    assert "industry" in filled
    rows = await _usage(tid, ACCOUNT_CAPABILITY)
    assert len(rows) == 1
    assert rows[0].attrs.get("cached") is False


async def test_an_account_with_nothing_to_search_on_is_not_charged():
    """`fetch` returns without issuing a request, so nothing was bought. Same rule that keeps an
    unconfigured phone lookup off the bill."""
    await _seed_billing()
    tid = await make_tenant(slug="eb2", name="EB Two")
    account = Account(tenant_id=tid, name="", domain="")

    async with tenant_session(tid) as ts:
        await _enricher({"industry": "Logistics"}).enrich(ts, account)
        await ts.session.commit()

    assert await _usage(tid, ACCOUNT_CAPABILITY) == []


async def test_a_source_database_answer_is_charged_and_marked_cached(monkeypatch):
    """Same revenue, no COGS. Billing only the web crawl would hand the saving to whichever
    customer's domain we happened to hold, and make revenue depend on our procurement."""
    from nexus.sources import provider

    await _seed_billing()
    tid = await make_tenant(slug="eb3", name="EB Three")
    account = Account(tenant_id=tid, name="Stripe", domain="stripe.com")

    class _Hit:
        source_name = "warehouse"
        fields = {"employee_count": "900"}

        def get(self, key):
            return {"industry": "Payments", "country": "US"}.get(key, "")

        def employee_count(self):
            return 900

    async def _hit(domain):
        return _Hit()

    monkeypatch.setattr(provider, "enrich_company", _hit)

    enricher = _enricher({"industry": "NEVER"})
    async with tenant_session(tid) as ts:
        filled = await enricher.enrich(ts, account)
        await ts.session.commit()

    assert account.industry == "Payments"
    assert set(filled) >= {"industry", "employee_count"}
    rows = await _usage(tid, ACCOUNT_CAPABILITY)
    assert len(rows) == 1, "a free answer is still an answer the customer received"
    assert rows[0].attrs.get("cached") is True
    assert rows[0].attrs.get("provider") == "source_db"


# ---- the quota must not take down the sweep -------------------------------------------------------

def _block_everything(monkeypatch):
    """Force the engine to refuse, whatever the plan says, so the caller's posture is the subject."""
    from nexus.billing import meter
    from nexus.billing.errors import QuotaExceeded

    def _boom(*a, **k):
        raise QuotaExceeded(ACCOUNT_CAPABILITY, reason="quota_exhausted", used=1, quota=0)

    class _Refuses:
        def __call__(self, ts, capability_id, **kw):
            _boom()

    monkeypatch.setattr(meter, "metered", _Refuses())


async def test_a_blocked_background_enrichment_returns_empty_instead_of_raising(monkeypatch):
    """The load-bearing one. `pipeline.process_account` has no try/except around enrichment, so a
    402 escaping here would stop the refresh — and with it signal collection — for a tenant who is
    merely over an ENRICHMENT limit."""
    await _seed_billing()
    _block_everything(monkeypatch)
    tid = await make_tenant(slug="eb4", name="EB Four")
    account = Account(tenant_id=tid, name="Northwind", domain="northwind.com")

    async with tenant_session(tid) as ts:
        filled = await _enricher({"industry": "Logistics"}).enrich(ts, account)

    assert filled == []
    assert account.industry is None, "blocked means the search was never issued"


async def test_a_blocked_user_request_raises_so_the_person_sees_the_upsell(monkeypatch):
    """The other half of the same decision. A silent no-op on a button click is indistinguishable
    from 'we looked and found nothing', which sends the user to support instead of to billing."""
    from nexus.billing.errors import QuotaExceeded

    await _seed_billing()
    _block_everything(monkeypatch)
    tid = await make_tenant(slug="eb5", name="EB Five")
    account = Account(tenant_id=tid, name="Northwind", domain="northwind.com")

    async with tenant_session(tid) as ts:
        with pytest.raises(QuotaExceeded):
            await _enricher({"industry": "Logistics"}).enrich(
                ts, account, raise_on_block=True
            )


async def test_the_pipeline_still_processes_an_account_when_enrichment_is_blocked(monkeypatch):
    """End-to-end version of the rule above, through the real pipeline entry point."""
    from nexus.core.config import get_settings
    from nexus.enrichment.account import set_account_enricher
    from nexus.pipeline import process_account

    await _seed_billing()
    _block_everything(monkeypatch)
    monkeypatch.setattr(get_settings(), "account_enrich_enabled", True)
    set_account_enricher(_enricher({"industry": "Logistics"}))
    try:
        tid = await make_tenant(slug="eb6", name="EB Six")
        async with tenant_session(tid) as ts:
            account = Account(tenant_id=tid, name="Northwind", domain="northwind.com")
            ts.add(account)
            await ts.flush()
            result = await process_account(ts, account)
        assert result is not None, "the refresh completed despite the enrichment block"
    finally:
        set_account_enricher(None)


# ---- batches charge once, before the concurrency ---------------------------------------------------

async def test_a_candidate_batch_is_charged_once_for_the_whole_batch():
    """Metering inside the gather would put N coroutines on one AsyncSession — the concurrency
    trap CLAUDE.md documents. One charge, taken before any of it starts."""
    await _seed_billing()
    tid = await make_tenant(slug="eb7", name="EB Seven")
    accounts = [
        Account(tenant_id=tid, name=f"Cand {i}", domain=f"cand{i}.com") for i in range(5)
    ]

    async with tenant_session(tid) as ts:
        await _enricher({"industry": "Logistics"}).enrich_batch(ts, accounts, concurrency=5)
        await ts.session.commit()

    rows = await _usage(tid, ACCOUNT_CAPABILITY)
    assert len(rows) == 1, "one row for the batch, not one per candidate"
    assert rows[0].quantity == 5, "...priced for what it actually enriched"
    assert rows[0].attrs.get("batch") is True
    # ...and the work still happened.
    assert all(a.industry == "Logistics" for a in accounts)


async def test_an_empty_batch_is_not_charged():
    await _seed_billing()
    tid = await make_tenant(slug="eb8", name="EB Eight")
    async with tenant_session(tid) as ts:
        await _enricher().enrich_batch(ts, [], concurrency=5)
        await ts.session.commit()
    assert await _usage(tid, ACCOUNT_CAPABILITY) == []


async def test_a_blocked_batch_skips_the_work_rather_than_raising(monkeypatch):
    """Lookalike and ICP discovery both call this. Over quota they must return a weaker ranking,
    never a failed search."""
    await _seed_billing()
    _block_everything(monkeypatch)
    tid = await make_tenant(slug="eb9", name="EB Nine")
    accounts = [Account(tenant_id=tid, name="Cand", domain="cand.com")]

    async with tenant_session(tid) as ts:
        await _enricher({"industry": "Logistics"}).enrich_batch(ts, accounts, concurrency=2)

    assert accounts[0].industry is None


# ---- enrich.contact ------------------------------------------------------------------------------

class _StubProvider:
    """A contact provider with a declared cost, so the waterfall's billing split is the subject."""

    def __init__(self, name, *, costs_money=True, email="", phone=""):
        self.name = name
        self.costs_money = costs_money
        self.email = email
        self.phone = phone
        self.calls = 0

    async def enrich(self, account, contact):
        from nexus.enrichment.providers import EnrichmentResult

        self.calls += 1
        r = EnrichmentResult(source=self.name)
        if self.email:
            r.email, r.email_confidence, r.found = self.email, 0.9, True
        if self.phone:
            r.phone, r.phone_confidence, r.found = self.phone, 0.9, True
        return r


async def _a_contact(ts, tid):
    account = Account(tenant_id=tid, name="Acme", domain="acme.com")
    ts.add(account)
    await ts.flush()
    contact = Contact(tenant_id=tid, account_id=account.id, full_name="Jane Doe")
    ts.add(contact)
    await ts.flush()
    return account, contact


async def test_a_contact_enrichment_is_charged():
    from nexus.enrichment.waterfall import WaterfallEnricher

    await _seed_billing()
    tid = await make_tenant(slug="ec1", name="EC One")
    paid = _StubProvider("search", email="jane@acme.com", phone="+14155552671")

    async with tenant_session(tid) as ts:
        account, contact = await _a_contact(ts, tid)
        await WaterfallEnricher([paid], verify=_no_verify).enrich_contact(ts, contact, account)
        await ts.session.commit()

    rows = await _usage(tid, CONTACT_CAPABILITY)
    assert len(rows) == 1
    assert rows[0].attrs.get("cached") is False
    assert paid.calls == 1


async def test_a_free_provider_that_fully_answers_bills_without_consulting_a_paid_one():
    """The saving, stated as a test: the customer is charged the same, and the search call and
    verification credit never happen."""
    from nexus.enrichment.waterfall import WaterfallEnricher

    await _seed_billing()
    tid = await make_tenant(slug="ec2", name="EC Two")
    free = _StubProvider("source_db", costs_money=False,
                         email="jane@acme.com", phone="+14155552671")
    paid = _StubProvider("search", email="other@acme.com")

    async with tenant_session(tid) as ts:
        account, contact = await _a_contact(ts, tid)
        await WaterfallEnricher([free, paid], verify=_no_verify).enrich_contact(
            ts, contact, account
        )
        await ts.session.commit()

    assert paid.calls == 0, "nothing paid was consulted"
    rows = await _usage(tid, CONTACT_CAPABILITY)
    assert len(rows) == 1, "...and the customer is charged anyway"
    assert rows[0].attrs.get("cached") is True


async def test_a_free_provider_that_only_half_answers_still_falls_through_to_the_paid_one():
    """A source database holding an email but no phone leaves a gap, and the gap is what the paid
    providers are for. Charged once, as a paid enrichment, because paid work happened."""
    from nexus.enrichment.waterfall import WaterfallEnricher

    await _seed_billing()
    tid = await make_tenant(slug="ec3", name="EC Three")
    free = _StubProvider("source_db", costs_money=False, email="jane@acme.com")
    paid = _StubProvider("search", phone="+14155552671")

    async with tenant_session(tid) as ts:
        account, contact = await _a_contact(ts, tid)
        await WaterfallEnricher([free, paid], verify=_no_verify).enrich_contact(
            ts, contact, account
        )
        await ts.session.commit()

    assert paid.calls == 1
    rows = await _usage(tid, CONTACT_CAPABILITY)
    assert len(rows) == 1
    assert rows[0].attrs.get("cached") is False


async def test_a_blocked_contact_enrichment_does_not_break_campaign_sourcing(monkeypatch):
    """`campaigns/sourcing.py` enriches every sourced persona in a loop. A 402 escaping there
    would abort the whole sourcing run over one contact."""
    from nexus.billing import meter
    from nexus.billing.errors import QuotaExceeded
    from nexus.enrichment.waterfall import WaterfallEnricher

    await _seed_billing()

    class _Refuses:
        def __call__(self, ts, capability_id, **kw):
            raise QuotaExceeded(CONTACT_CAPABILITY, reason="quota_exhausted", used=1, quota=0)

    monkeypatch.setattr(meter, "metered", _Refuses())
    tid = await make_tenant(slug="ec4", name="EC Four")
    paid = _StubProvider("search", email="jane@acme.com")

    async with tenant_session(tid) as ts:
        account, contact = await _a_contact(ts, tid)
        result = await WaterfallEnricher([paid], verify=_no_verify).enrich_contact(
            ts, contact, account
        )

    assert result.found is False
    assert paid.calls == 0, "blocked means the search was never issued"
    assert not contact.email


async def _no_verify(email):
    """The waterfall's final deliverability pass, stubbed — it is `verify.email`, a different
    capability with its own metering, and it would otherwise reach the registry."""
    from nexus.verification import EmailVerification

    return EmailVerification(email=email, status="unknown", confidence=0.0)


# ---- the endpoints ------------------------------------------------------------------------------

async def test_the_enrich_endpoint_returns_402_rather_than_a_silent_no_op(client, monkeypatch):
    """A person clicked Enrich. `raise_on_block=True` is what turns the engine's refusal into the
    402 payload the UI can render as an upsell."""
    from nexus.billing import meter
    from nexus.billing.errors import QuotaExceeded

    await _seed_billing()
    token = await signup(client, slug="ec5", email="rep@ec5.com", company="EC5")
    created = await client.post(
        "/api/accounts", headers=auth(token), json={"name": "Acme", "domain": "acme.com"}
    )
    assert created.status_code in (200, 201), created.text
    account_id = created.json()["id"]

    class _Refuses:
        def __call__(self, ts, capability_id, **kw):
            raise QuotaExceeded(ACCOUNT_CAPABILITY, reason="quota_exhausted", used=1, quota=0)

    monkeypatch.setattr(meter, "metered", _Refuses())
    r = await client.post(f"/api/accounts/{account_id}/enrich", headers=auth(token))
    assert r.status_code == 402, r.text
    assert r.json().get("error") == "quota_exceeded"
