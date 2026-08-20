# tests/test_source_db_provider.py
"""Step 7: reading a verified source database as an enrichment provider.

Steps 1–6 prove a source is safe to read (`tests/test_source_databases.py`). These tests are about
what happens when something actually reads one, and they pin three properties in that order of
importance:

1. **A source that has not earned it is never read.** Verified *and* enabled, both, every time.
2. **A failure falls through and never stops collection.** The locked posture from the plan: it is
   an optimisation, not a dependency. An unreachable source must be indistinguishable, from the
   caller's side, from no source being registered at all.
3. **A row is used only if it is provably about who we asked for.** Wrong attribution is the
   failure this subsystem has shipped six times, and here it writes into shared stores that every
   tenant reads.

Plus the commercial rule that is easy to get backwards: a source-database hit is metered
**identically** to the paid lookup it replaced. The saving is COGS, not price.

Nothing here reaches a real database — `engine.fetch_by_identity` is the seam that talks to a
foreign host, exactly as `engine.introspect` / `engine.dry_run` are for the ladder.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session

DSN = "postgresql://user:pw@db.example.com:5432/warehouse"

COMPANY_DISCOVERED = {
    "tables": [{
        "schema": "public", "table": "accounts",
        "columns": [
            {"name": "website", "type": "text"},
            {"name": "legal_name", "type": "text"},
            {"name": "sector", "type": "text"},
            {"name": "headcount", "type": "integer"},
        ],
    }]
}
COMPANY_MAPPING = {
    "entity": "company", "schema": "public", "table": "accounts",
    "columns": {"domain": "website", "name": "legal_name",
                "industry": "sector", "employee_count": "headcount"},
}

PERSON_DISCOVERED = {
    "tables": [{
        "schema": "public", "table": "people",
        "columns": [
            {"name": "profile", "type": "text"},
            {"name": "work_email", "type": "text"},
            {"name": "mobile", "type": "text"},
            {"name": "fullname", "type": "text"},
        ],
    }]
}
PERSON_MAPPING = {
    "entity": "person", "schema": "public", "table": "people",
    "columns": {"linkedin_url": "profile", "email": "work_email",
                "phone": "mobile", "full_name": "fullname"},
}

LINKEDIN = "https://www.linkedin.com/in/derek-lemoine-8891a6176/"


def _allow_private(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "source_db_allow_private", True)


async def _a_usable_source(monkeypatch, *, name, discovered, mapping,
                           status="verified", enabled=True):
    """Register a source and put it on the rung the test needs, without faking the ladder itself."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.source_db import SourceDatabase
    from nexus.sources import service

    _allow_private(monkeypatch)
    row = await service.register(name=name, dsn=DSN)
    async with get_platform_sessionmaker()() as s:
        live = await s.get(SourceDatabase, row.id)
        live.discovered_schema = discovered
        live.status = "introspected"
        await s.commit()
    await service.set_mapping(row.id, mapping)
    async with get_platform_sessionmaker()() as s:
        live = await s.get(SourceDatabase, row.id)
        live.status = status
        live.enabled = enabled
        live.dry_run = {"rows": 5, "usable_rows": 5, "verified": True}
        await s.commit()
    return row.id


class _Reads:
    """Stands in for the foreign host, and counts reads so 'never read' is provable."""

    def __init__(self, rows=None, *, fail=False):
        self.rows = rows or []
        self.fail = fail
        self.calls = 0
        self.seen_keys: list[list[str]] = []

    async def __call__(self, sealed, mapping, *, key_field, key_values, limit=5):
        self.calls += 1
        self.seen_keys.append(list(key_values))
        if self.fail:
            from nexus.sources.engine import SourceUnavailable

            raise SourceUnavailable("connection refused")
        return self.rows


def _patch_reads(monkeypatch, reads):
    from nexus.sources import engine

    monkeypatch.setattr(engine, "fetch_by_identity", reads)
    return reads


# ---- rule 1: only a source that earned it is ever read -----------------------------------------

async def test_a_source_that_was_never_verified_is_not_read(fresh_db, monkeypatch):
    """The dry run is the entire safety argument. A provider that read a `mapped` source would
    make it a suggestion rather than a gate."""
    from nexus.sources.provider import lookup_company

    await _a_usable_source(monkeypatch, name="wh", discovered=COMPANY_DISCOVERED,
                           mapping=COMPANY_MAPPING, status="mapped", enabled=False)
    reads = _patch_reads(monkeypatch, _Reads([{"domain": "stripe.com"}]))

    assert await lookup_company("stripe.com") is None
    assert reads.calls == 0, "an unverified source must never be reached at all"


async def test_a_verified_but_disabled_source_is_not_read(fresh_db, monkeypatch):
    """`enabled` is the operator's kill switch. Flipping it off during an incident means 'stop
    reading this', and a provider testing `status` alone would keep reading."""
    from nexus.sources import service
    from nexus.sources.provider import lookup_company

    source_id = await _a_usable_source(monkeypatch, name="wh", discovered=COMPANY_DISCOVERED,
                                       mapping=COMPANY_MAPPING)
    reads = _patch_reads(monkeypatch, _Reads([{"domain": "stripe.com"}]))

    assert await lookup_company("stripe.com") is not None
    assert reads.calls == 1

    await service.set_enabled(source_id, False)
    assert await lookup_company("stripe.com") is None
    assert reads.calls == 1, "disabling stops the reads, it does not merely hide the results"


async def test_a_source_mapped_to_another_entity_is_not_asked(fresh_db, monkeypatch):
    """A person source has nothing to say about a company domain, and asking it would spend a
    round trip to a foreign host to learn that."""
    from nexus.sources.provider import lookup_company

    await _a_usable_source(monkeypatch, name="people-wh", discovered=PERSON_DISCOVERED,
                           mapping=PERSON_MAPPING)
    reads = _patch_reads(monkeypatch, _Reads([{"linkedin_url": "x"}]))

    assert await lookup_company("stripe.com") is None
    assert reads.calls == 0


# ---- rule 2: failure falls through, never stops collection --------------------------------------

async def test_an_unreachable_source_returns_nothing_rather_than_raising(fresh_db, monkeypatch):
    """The locked failure posture. From the caller's side an unreachable source has to be
    indistinguishable from no source being registered."""
    from nexus.sources.provider import enrich_company, lookup_company

    await _a_usable_source(monkeypatch, name="wh", discovered=COMPANY_DISCOVERED,
                           mapping=COMPANY_MAPPING)
    _patch_reads(monkeypatch, _Reads(fail=True))

    assert await lookup_company("stripe.com") is None
    assert await enrich_company("stripe.com") is None


async def test_one_broken_source_does_not_hide_the_answer_in_the_next(fresh_db, monkeypatch):
    """Sources are tried independently. Aborting the sweep on the first failure would make one
    dead warehouse look like 'nobody has this company'."""
    from nexus.sources.provider import lookup_company

    await _a_usable_source(monkeypatch, name="a-broken", discovered=COMPANY_DISCOVERED,
                           mapping=COMPANY_MAPPING)
    await _a_usable_source(monkeypatch, name="b-working", discovered=COMPANY_DISCOVERED,
                           mapping=COMPANY_MAPPING)

    calls: list[str] = []

    async def _one_fails(sealed, mapping, *, key_field, key_values, limit=5):
        calls.append("call")
        if len(calls) == 1:
            raise RuntimeError("connection refused")
        return [{"domain": "stripe.com", "name": "Stripe"}]

    _patch_reads(monkeypatch, _one_fails)

    hit = await lookup_company("stripe.com")
    assert hit is not None and hit.get("name") == "Stripe"


async def test_the_paid_enricher_still_runs_when_the_source_is_down(fresh_db, monkeypatch):
    """The whole posture in one test: a source database being down degrades to 'use the paid
    provider', never to 'this account does not get enriched'."""
    from nexus.enrichment.account import SearchBackedAccountEnricher
    from nexus.models.account import Account

    await _a_usable_source(monkeypatch, name="wh", discovered=COMPANY_DISCOVERED,
                           mapping=COMPANY_MAPPING)
    _patch_reads(monkeypatch, _Reads(fail=True))

    enricher = SearchBackedAccountEnricher(search=None, llm=None)

    async def _paid(account):
        return {"industry": "Payments", "employee_count": 900}

    monkeypatch.setattr(enricher, "fetch", _paid)
    account = Account(tenant_id="t", name="Stripe", domain="stripe.com")

    filled = await enricher.enrich(account)
    assert account.industry == "Payments"
    assert "industry" in filled


# ---- rule 3: a row is used only if it is about who we asked for ---------------------------------

async def test_a_row_about_a_different_company_is_discarded(fresh_db, monkeypatch):
    """`WHERE domain IN (...)` is a candidate filter over somebody else's data, not proof. This is
    the wrong-attribution guard, and it writes into a store every tenant reads."""
    from nexus.sources.provider import lookup_company

    await _a_usable_source(monkeypatch, name="wh", discovered=COMPANY_DISCOVERED,
                           mapping=COMPANY_MAPPING)
    _patch_reads(monkeypatch, _Reads([{"domain": "adyen.com", "name": "Adyen"}]))

    assert await lookup_company("stripe.com") is None


async def test_a_row_about_a_different_person_is_discarded(fresh_db, monkeypatch):
    """Getting a person wrong means a rep phones a stranger with someone else's context."""
    from nexus.sources.provider import lookup_person

    await _a_usable_source(monkeypatch, name="wh", discovered=PERSON_DISCOVERED,
                           mapping=PERSON_MAPPING)
    _patch_reads(monkeypatch, _Reads([
        {"linkedin_url": "https://linkedin.com/in/somebody-else", "phone": "+14155552671"}
    ]))

    assert await lookup_person(linkedin_url=LINKEDIN) is None


async def test_a_domain_stored_with_www_or_a_scheme_still_matches(fresh_db, monkeypatch):
    """We normalise to `stripe.com`; a source may hold `www.stripe.com`. Both are the same company
    and the match is made through the shared store's own normaliser, not by string equality."""
    from nexus.sources.provider import lookup_company

    await _a_usable_source(monkeypatch, name="wh", discovered=COMPANY_DISCOVERED,
                           mapping=COMPANY_MAPPING)
    reads = _patch_reads(monkeypatch, _Reads([
        {"domain": "https://www.stripe.com/", "name": "Stripe"}
    ]))

    hit = await lookup_company("stripe.com")
    assert hit is not None and hit.get("name") == "Stripe"
    # ...and the candidate list we asked with covered the spellings, rather than relying on a
    # function over the column that would defeat the source's index.
    assert "www.stripe.com" in reads.seen_keys[0]


async def test_a_lookup_may_never_be_keyed_on_a_name():
    """The mapping refuses a name-only identity; the query builder refuses it again. A name match
    across tenants is how this subsystem shipped six wrong-attribution bugs."""
    from nexus.sources.engine import build_lookup
    from nexus.sources.safety import SourceRejected

    mapping = dict(PERSON_MAPPING)
    with pytest.raises(SourceRejected, match="may only be keyed on"):
        build_lookup(mapping, key_field="full_name")


def test_build_lookup_revalidates_identifiers_and_binds_the_keys():
    """'It was safe when we stored it' is exactly the assumption that makes stored-value injection
    work — and this function is what puts a name into a WHERE clause."""
    from nexus.sources.engine import build_lookup
    from nexus.sources.safety import SourceRejected

    sql = build_lookup(COMPANY_MAPPING, key_field="domain")
    assert '"public"."accounts"' in sql
    assert '"website" AS "domain"' in sql
    assert ":keys" in sql and ":limit" in sql
    assert "stripe" not in sql

    poisoned = dict(COMPANY_MAPPING,
                    columns={"domain": 'w"; DROP TABLE users; --', "name": "legal_name"})
    with pytest.raises(SourceRejected, match="unsafe column"):
        build_lookup(poisoned, key_field="domain")


async def test_the_generated_statement_really_runs_and_really_filters():
    """The one test that executes the SQL this module writes instead of asserting about its text.

    Everything else here replaces `fetch_by_identity`, which is right — it is the seam to a
    foreign host. But that leaves the generated statement itself unproven, and a quoting or
    bind-parameter mistake in it would only ever surface against a live customer database. SQLite
    is not Postgres, so this pins the parts that are portable and are exactly the parts we wrote:
    the quoted schema/table, the `AS` projection onto app field names, the expanding `IN` over our
    candidate keys, and the bound `LIMIT`.
    """
    from sqlalchemy import bindparam, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from nexus.sources.engine import build_lookup

    sql = build_lookup(COMPANY_MAPPING, key_field="domain", limit=5)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            # SQLite has no schemas, but an attached database is addressed exactly like one — so
            # `"public"."accounts"` resolves and the quoting is genuinely exercised.
            await conn.execute(text("ATTACH DATABASE ':memory:' AS public"))
            await conn.execute(text(
                'CREATE TABLE public.accounts '
                '(website TEXT, legal_name TEXT, sector TEXT, headcount INTEGER)'
            ))
            for site, name, sector, heads in (
                ("www.stripe.com", "Stripe", "Payments", 900),
                ("adyen.com", "Adyen", "Payments", 4000),
            ):
                await conn.execute(
                    text('INSERT INTO public.accounts VALUES (:w, :n, :s, :h)'),
                    {"w": site, "n": name, "s": sector, "h": heads},
                )

            stmt = text(sql).bindparams(bindparam("keys", expanding=True))
            rows = [dict(r) for r in (await conn.execute(
                stmt, {"keys": ["stripe.com", "www.stripe.com"], "limit": 5}
            )).mappings().all()]
    finally:
        await engine.dispose()

    assert len(rows) == 1, "the candidate keys must filter, not return the whole table"
    # Projected onto APP field names, which is what makes a row usable without knowing the source.
    assert rows[0]["domain"] == "www.stripe.com"
    assert rows[0]["name"] == "Stripe"
    assert rows[0]["employee_count"] == 900


# ---- results land in the shared stores ----------------------------------------------------------

async def test_a_company_hit_lands_in_the_shared_store(fresh_db, monkeypatch):
    """Bought once for every tenant. If the hit stayed in the caller's account row, the next
    workspace to ask the same question would pay again."""
    from nexus.companies.resolution import company_id_for
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.company import Company
    from nexus.sources.provider import enrich_company

    await _a_usable_source(monkeypatch, name="wh", discovered=COMPANY_DISCOVERED,
                           mapping=COMPANY_MAPPING)
    _patch_reads(monkeypatch, _Reads([{
        "domain": "stripe.com", "name": "Stripe", "industry": "Payments",
        "employee_count": "900",
    }]))

    assert await enrich_company("stripe.com") is not None
    async with get_platform_sessionmaker()() as s:
        company = await s.get(Company, company_id_for("stripe.com"))
    assert company is not None
    assert company.industry == "Payments"
    # A string headcount from a foreign column still lands as a real integer.
    assert company.employee_count == 900
    assert company.source == "source_db"


async def test_a_headcount_band_is_dropped_rather_than_guessed_at(fresh_db, monkeypatch):
    """Headcount drives ICP scoring. A wrong number silently moves accounts in and out of a
    rep's list, which is worse than a blank."""
    from nexus.sources.provider import SourceHit

    assert SourceHit(entity="company", fields={"employee_count": "51-200"}).employee_count() is None
    assert SourceHit(entity="company", fields={"employee_count": ""}).employee_count() is None
    assert SourceHit(entity="company", fields={"employee_count": True}).employee_count() is None
    assert SourceHit(entity="company", fields={"employee_count": "900"}).employee_count() == 900


async def test_a_person_hit_lands_in_the_shared_store(fresh_db, monkeypatch):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.people.store import person_id_for, read_person
    from nexus.sources.provider import enrich_person

    await _a_usable_source(monkeypatch, name="wh", discovered=PERSON_DISCOVERED,
                           mapping=PERSON_MAPPING)
    _patch_reads(monkeypatch, _Reads([{
        "linkedin_url": LINKEDIN, "full_name": "Derek Lemoine", "phone": "(415) 555-2671",
    }]))

    assert await enrich_person(linkedin_url=LINKEDIN) is not None
    async with get_platform_sessionmaker()() as s:
        view = await read_person(s, person_id_for(linkedin=LINKEDIN))
    assert view is not None
    assert view.full_name == "Derek Lemoine"
    # Canonicalised on the way in, so the same human does not read differently depending on
    # whether an actor or a source database supplied the number.
    assert view.phone == "+14155552671"


async def test_a_source_does_not_rewrite_the_provenance_of_a_person_it_did_not_create(
    fresh_db, monkeypatch
):
    """`source` records how a person came to EXIST. Stamping it on every read would relabel
    someone who arrived by contact backfill months ago, rewriting the field that exists to settle
    disagreements between sources — while `enrichment_source` still attributes the DATA."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.person import Person
    from nexus.people.store import person_id_for, resolve_person_record
    from nexus.sources.provider import enrich_person

    await _a_usable_source(monkeypatch, name="wh", discovered=PERSON_DISCOVERED,
                           mapping=PERSON_MAPPING)
    _patch_reads(monkeypatch, _Reads([{"linkedin_url": LINKEDIN, "phone": "(415) 555-2671"}]))

    async with get_platform_sessionmaker()() as s:
        await resolve_person_record(s, linkedin_url=LINKEDIN, full_name="Derek Lemoine")
        await s.commit()

    await enrich_person(linkedin_url=LINKEDIN)

    async with get_platform_sessionmaker()() as s:
        person = await s.get(Person, person_id_for(linkedin=LINKEDIN))
    assert person is not None
    assert person.source == "contact_backfill", "provenance of an existing person is not ours"
    assert person.enrichment_source == "source_db", "...but the data we supplied is attributed"


async def test_a_person_this_source_created_is_attributed_to_it(fresh_db, monkeypatch):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.person import Person
    from nexus.people.store import person_id_for
    from nexus.sources.provider import enrich_person

    await _a_usable_source(monkeypatch, name="wh", discovered=PERSON_DISCOVERED,
                           mapping=PERSON_MAPPING)
    _patch_reads(monkeypatch, _Reads([{"linkedin_url": LINKEDIN, "full_name": "Derek Lemoine"}]))

    await enrich_person(linkedin_url=LINKEDIN)
    async with get_platform_sessionmaker()() as s:
        person = await s.get(Person, person_id_for(linkedin=LINKEDIN))
    assert person is not None and person.source == "source_db"


async def test_a_non_phone_in_a_phone_column_is_not_recorded(fresh_db, monkeypatch):
    """A source column named `phone` holding "Premium feature" is not a phone number. Recording it
    would suppress re-lookup for every tenant until the TTL expired."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.people.store import person_id_for, read_person
    from nexus.sources.provider import enrich_person

    await _a_usable_source(monkeypatch, name="wh", discovered=PERSON_DISCOVERED,
                           mapping=PERSON_MAPPING)
    _patch_reads(monkeypatch, _Reads([{"linkedin_url": LINKEDIN, "phone": "Premium feature"}]))

    await enrich_person(linkedin_url=LINKEDIN)
    async with get_platform_sessionmaker()() as s:
        view = await read_person(s, person_id_for(linkedin=LINKEDIN))
    assert view is not None
    assert view.phone == ""
    assert view.phone_status == "unattempted", "a junk string must not read as a completed lookup"


# ---- the commercial rule ------------------------------------------------------------------------

async def _seed_billing():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()


async def _usage_rows(tenant_id: str, capability: str) -> list:
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


async def test_a_source_database_hit_replaces_the_paid_actor_run(fresh_db, monkeypatch):
    """Where the COGS saving actually lands: a phone lookup is the priciest capability on the rate
    card, and a registered source answers it without an actor run."""
    from nexus.integrations.apify import ApifyClient, set_apify_client
    from nexus.people.enrich import find_phone

    await _seed_billing()
    await _a_usable_source(monkeypatch, name="wh", discovered=PERSON_DISCOVERED,
                           mapping=PERSON_MAPPING)
    _patch_reads(monkeypatch, _Reads([{"linkedin_url": LINKEDIN, "phone": "(415) 555-2671"}]))

    class _CountingApify(ApifyClient):
        def __init__(self):
            super().__init__(["stub-key"])
            self.runs = 0

        async def run_actor(self, actor, run_input, *, timeout=None):
            self.runs += 1
            return [{"phone": "+15005550000"}]

    stub = _CountingApify()
    set_apify_client(stub)
    try:
        tid = await make_tenant(slug="sdp1", name="SDP One")
        async with tenant_session(tid) as ts:
            result = await find_phone(ts, linkedin_url=LINKEDIN, full_name="Derek Lemoine")
            await ts.session.commit()

        assert result.phone == "+14155552671"
        assert result.source == "source_db"
        assert stub.runs == 0, "the paid actor must not run when a source database answered"
    finally:
        set_apify_client(None)


async def test_a_source_database_hit_is_metered_exactly_like_the_paid_lookup(
    fresh_db, monkeypatch
):
    """The customer is charged for the answer, not for our infrastructure. Charging less because
    WE happen to hold a licence would make revenue depend on our procurement."""
    from nexus.integrations.apify import ApifyClient, set_apify_client
    from nexus.people.enrich import PHONE_CAPABILITY, find_phone

    await _seed_billing()
    await _a_usable_source(monkeypatch, name="wh", discovered=PERSON_DISCOVERED,
                           mapping=PERSON_MAPPING)
    _patch_reads(monkeypatch, _Reads([{"linkedin_url": LINKEDIN, "phone": "(415) 555-2671"}]))
    set_apify_client(ApifyClient(["stub-key"]))
    try:
        tid = await make_tenant(slug="sdp2", name="SDP Two")
        async with tenant_session(tid) as ts:
            await find_phone(ts, linkedin_url=LINKEDIN)
            await ts.session.commit()

        rows = await _usage_rows(tid, PHONE_CAPABILITY)
        assert len(rows) == 1, "the answer is billable however cheaply we obtained it"
        # ...and the margin is visible, which is the only reason the flag exists.
        assert rows[0].attrs.get("cached") is True
        assert rows[0].attrs.get("provider") == "source_db"
    finally:
        set_apify_client(None)


# ---- wired ahead of the paid providers -----------------------------------------------------------

async def test_the_source_database_is_first_in_the_contact_waterfall(fresh_db):
    """Cheapest first. Everything after it spends a search call, a verification credit or an
    actor run per contact."""
    from nexus.enrichment.providers import SourceDatabaseProvider
    from nexus.enrichment.waterfall import get_enricher, set_enricher

    set_enricher(None)  # type: ignore[arg-type]
    try:
        providers = get_enricher().providers
        assert isinstance(providers[0], SourceDatabaseProvider)
    finally:
        set_enricher(None)  # type: ignore[arg-type]


async def test_a_contact_with_no_identity_is_not_looked_up(fresh_db, monkeypatch):
    """A name is not an identity, so there is nothing to key on and the paid finders below run
    exactly as they did before this provider existed."""
    from nexus.enrichment.providers import SourceDatabaseProvider
    from nexus.models.account import Account, Contact

    await _a_usable_source(monkeypatch, name="wh", discovered=PERSON_DISCOVERED,
                           mapping=PERSON_MAPPING)
    reads = _patch_reads(monkeypatch, _Reads([{"linkedin_url": LINKEDIN}]))

    account = Account(tenant_id="t", name="Stripe", domain="stripe.com")
    contact = Contact(tenant_id="t", account_id="a", full_name="Derek Lemoine")

    result = await SourceDatabaseProvider().enrich(account, contact)
    assert result.found is False
    assert reads.calls == 0


async def test_the_account_enricher_skips_the_paid_web_path_when_a_source_answered(
    fresh_db, monkeypatch
):
    """The saving, stated as a test: a search call and an LLM completion that never happen."""
    from nexus.enrichment.account import SearchBackedAccountEnricher
    from nexus.models.account import Account

    await _a_usable_source(monkeypatch, name="wh", discovered=COMPANY_DISCOVERED,
                           mapping=COMPANY_MAPPING)
    _patch_reads(monkeypatch, _Reads([{
        "domain": "stripe.com", "name": "Stripe", "industry": "Payments",
        "employee_count": "900",
    }]))

    enricher = SearchBackedAccountEnricher(search=None, llm=None)
    paid_calls = []

    async def _paid(account):
        paid_calls.append(account)
        return {"industry": "Wrong", "employee_count": 1}

    monkeypatch.setattr(enricher, "fetch", _paid)
    account = Account(tenant_id="t", name="Stripe", domain="stripe.com")

    filled = await enricher.enrich(account)
    assert account.industry == "Payments"
    assert account.employee_count == 900
    assert set(filled) >= {"industry", "employee_count"}
    assert paid_calls == [], "the paid path must not run once the basics are filled"
