# tests/test_enrichment_backoff.py
"""An account that cannot be enriched must not be re-searched every refresh cycle.

Measured on the live deployment: 123 `enrich.account` events across 56 accounts, and account
enrichment is the largest consumer of Exa credits in the product.

`enrich` already skipped the paid path when `industry` AND `employee_count` were both present. The
gap is the opposite case: an account the web simply has no firmographics for never satisfies that
gate, so it issues a search request and an LLM completion on EVERY refresh, forever, and buys
nothing each time. Firmographics change on a scale of quarters; the refresh cycle runs in hours.

So attempts are now spaced by `account_enrich_min_interval_days`, and a person pressing "Enrich"
passes `force=True` and is never throttled — the interval exists to stop a background sweep
re-buying the same empty answer, not to tell a user "no".
"""
from __future__ import annotations

from datetime import timedelta

from nexus.core.db import utcnow


class _CountingSearch:
    def __init__(self):
        self.calls = 0

    async def search(self, query, limit=5, **kw):
        self.calls += 1
        return []


def _enricher(search):
    from nexus.enrichment.account import SearchBackedAccountEnricher

    return SearchBackedAccountEnricher(search=search, llm=None)


def _account(**kw):
    from nexus.models.account import Account

    kw.setdefault("tenant_id", "t1")
    kw.setdefault("name", "Acme")
    kw.setdefault("domain", "acme.com")
    return Account(id="a1", **kw)


def test_a_recent_attempt_is_not_repeated():
    """The saving. A background sweep must not re-buy an answer it bought an hour ago."""
    from nexus.enrichment.account import should_attempt

    account = _account()
    account.custom_fields = {"enrich_attempted_at": utcnow().isoformat()}
    assert should_attempt(account, force=False) is False


def test_an_old_attempt_is_retried():
    """Firmographics do change; 'never again' would be as wrong as 'every cycle'."""
    from nexus.enrichment.account import should_attempt

    account = _account()
    account.custom_fields = {
        "enrich_attempted_at": (utcnow() - timedelta(days=45)).isoformat()
    }
    assert should_attempt(account, force=True) is True
    assert should_attempt(account, force=False) is True


def test_a_never_attempted_account_is_always_attempted():
    """Regression guard: a brand-new account must enrich immediately, not wait out an interval."""
    from nexus.enrichment.account import should_attempt

    assert should_attempt(_account(), force=False) is True


def test_a_person_pressing_enrich_is_never_throttled():
    """The interval stops a background sweep re-buying an empty answer. It must never tell a user
    who explicitly asked that nothing happened -- that reads as a broken button."""
    from nexus.enrichment.account import should_attempt

    account = _account()
    account.custom_fields = {"enrich_attempted_at": utcnow().isoformat()}
    assert should_attempt(account, force=True) is True


def test_a_malformed_timestamp_does_not_block_enrichment():
    """`custom_fields` is a free-form JSON column that several paths write. An unparseable value
    must fail OPEN -- refusing to enrich because a timestamp is corrupt would be a silent,
    permanent outage for that account."""
    from nexus.enrichment.account import should_attempt

    for bad in ("not-a-date", "", None, 12345, {"nested": True}):
        account = _account()
        account.custom_fields = {"enrich_attempted_at": bad}
        assert should_attempt(account, force=False) is True, f"blocked by {bad!r}"


def test_the_interval_is_configurable():
    from nexus.core.config import get_settings

    assert getattr(get_settings(), "account_enrich_min_interval_days", None) is not None


async def test_the_search_is_not_issued_for_a_recent_attempt(fresh_db):
    """End to end: the point is that no request is MADE, not merely that nothing is stored."""
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession
    from nexus.models.identity import Tenant

    search = _CountingSearch()
    enricher = _enricher(search)

    async with get_sessionmaker()() as s:
        tenant = Tenant(name="EB", slug="eb")
        s.add(tenant)
        await s.flush()
        ts = TenantSession(s, tenant.id)

        from nexus.models.account import Account

        account = Account(tenant_id=tenant.id, name="Acme", domain="acme.com")
        account.custom_fields = {"enrich_attempted_at": utcnow().isoformat()}
        ts.add(account)
        await ts.flush()

        await enricher.enrich(ts, account, meter=False)
        assert search.calls == 0, "a recently attempted account still issued a paid search"
