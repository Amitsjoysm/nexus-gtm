# tests/test_b2b_enrichment_actor.py
"""Structured firmographics, preferred over the search+LLM path.

The web path is a SEARCH plus an LLM completion that reads fields out of page text, and the
completion is the fragile half. Measured on the live deployment 2026-09-09: enriching
`anthropic.com` took 66.6 seconds and filled NOTHING. Exa answered 200 to every search; Groq
answered `retry-after=862s` on one key and 15s on the other, the chain fell through to the offline
stub, and the stub extracts nothing. One of the most documented companies on the web came back
blank because the READER was down, not because the web was quiet.

`teodor_banea/b2b-lead-enrichment-free` returns already-parsed fields, so it has no reader to be
down. It is therefore tried first and the search becomes the fallback.
"""
from __future__ import annotations

import pytest

from nexus.enrichment.b2b_actor import to_fields

# Shape taken from a real run against anthropic.com and stripe.com on 2026-09-09.
ROW = {
    "companyDomain": "stripe.com",
    "companyName": "Home | Stripe",          # the scraped page title, not a company name
    "companyDescription": "Stripe is a financial services platform.",
    "companyIndustry": "Financial Technology",
    "companyIndustries": [],
    "companySizeEmployees": None,
    "companyCountry": "US",
    "companyRegion": "California",
    "companyCity": "South San Francisco",
    "companyLinkedinUrl": "https://www.linkedin.com/company/stripe",
    "companyAnnualRevenue": None,
    "techStack": ["Nginx", "Next.js"],
    "personEmail": "someone@stripe.com",
    "personPhone": "+15551234567",
}


# ---- what it takes, and what it refuses ----------------------------------------------------------

def test_the_fields_a_rep_reads_are_mapped():
    got = to_fields(ROW)
    assert got["industry"] == "Financial Technology"
    assert got["country"] == "US"
    assert got["region"] == "California"
    assert got["city"] == "South San Francisco"
    assert got["tech_stack"] == ["Nginx", "Next.js"]
    assert "financial services platform" in got["description"]


def test_the_scraped_page_title_never_becomes_the_account_name():
    """`companyName` is the page title — it came back as "Home \\ Anthropic" for anthropic.com.
    An account renamed to a page title is worse than one carrying the name the customer typed."""
    assert "name" not in to_fields(ROW)


def test_no_person_data_is_taken():
    """Contact enrichment is a separate capability with its own waterfall, consent posture and
    price. Harvesting people here would bill the wrong meter and bypass `nexus/people/`."""
    got = to_fields(ROW)
    for leaked in ("email", "phone", "person_email", "personEmail"):
        assert leaked not in got


def test_a_headcount_range_is_not_a_number():
    """`apply` only accepts a real int, and "51-200" is not one."""
    assert to_fields({**ROW, "companySizeEmployees": "51-200"})["employee_count"] is None
    assert to_fields({**ROW, "companySizeEmployees": "250"})["employee_count"] == 250
    assert to_fields({**ROW, "companySizeEmployees": True})["employee_count"] is None
    assert to_fields({**ROW, "companySizeEmployees": 0})["employee_count"] is None


def test_revenue_becomes_the_display_string_apply_stores():
    """`apply` keeps revenue as a short string in custom_fields. Widening it for one new provider
    would give this actor a write path the others do not have."""
    assert to_fields({**ROW, "companyAnnualRevenue": 32_300_000})["revenue"] == "$32.3M"
    assert to_fields({**ROW, "companyAnnualRevenue": 2_000_000_000})["revenue"] == "$2B"
    assert to_fields({**ROW, "companyAnnualRevenue": None})["revenue"] == ""


def test_an_industry_list_is_used_when_the_scalar_is_blank():
    got = to_fields({**ROW, "companyIndustry": "", "companyIndustries": ["Payments", "SaaS"]})
    assert got["industry"] == "Payments"


def test_a_junk_row_yields_nothing_rather_than_raising():
    assert to_fields(None) == {}          # type: ignore[arg-type]
    assert to_fields("not a dict") == {}  # type: ignore[arg-type]


# ---- identity ------------------------------------------------------------------------------------

async def test_a_row_about_another_domain_is_discarded(monkeypatch):
    """The actor takes a list and returns a dataset. Taking row zero is how a stranger's
    firmographics land on a customer's account — the wrong-attribution failure `nexus/companies/`
    has shipped six times."""
    from nexus.enrichment import b2b_actor

    class _Client:
        async def run_actor(self, actor, run_input, **kw):
            return [{**ROW, "companyDomain": "someone-else.com"}]

    monkeypatch.setattr(
        "nexus.integrations.apify.get_apify_client", lambda: _Client(), raising=False
    )
    assert await b2b_actor.fetch("stripe.com") == {}


async def test_the_matching_row_is_picked_out_of_a_mixed_dataset(monkeypatch):
    from nexus.enrichment import b2b_actor

    class _Client:
        async def run_actor(self, actor, run_input, **kw):
            return [{**ROW, "companyDomain": "other.com", "companyIndustry": "Wrong"}, ROW]

    monkeypatch.setattr(
        "nexus.integrations.apify.get_apify_client", lambda: _Client(), raising=False
    )
    assert (await b2b_actor.fetch("stripe.com"))["industry"] == "Financial Technology"


@pytest.mark.parametrize("bad", ["", "   ", "not-a-domain", "@", None])
async def test_a_domain_that_is_not_a_domain_never_reaches_the_actor(bad, monkeypatch):
    """Nothing bought, nothing charged — the rule that keeps an unconfigured lookup off the bill."""
    from nexus.enrichment import b2b_actor

    called: list[str] = []

    class _Client:
        async def run_actor(self, actor, run_input, **kw):
            called.append(actor)
            return [ROW]

    monkeypatch.setattr(
        "nexus.integrations.apify.get_apify_client", lambda: _Client(), raising=False
    )
    assert await b2b_actor.fetch(bad) == {}  # type: ignore[arg-type]
    assert called == []


# ---- it must never break enrichment ---------------------------------------------------------------

async def test_a_failing_actor_falls_through_rather_than_raising(monkeypatch):
    """Provider isolation, as every other provider here. It is an optimisation, not a dependency."""
    from nexus.enrichment import b2b_actor

    class _Client:
        async def run_actor(self, actor, run_input, **kw):
            raise RuntimeError("actor exploded")

    monkeypatch.setattr(
        "nexus.integrations.apify.get_apify_client", lambda: _Client(), raising=False
    )
    assert await b2b_actor.fetch("stripe.com") == {}


async def test_a_slow_actor_is_bounded(monkeypatch):
    """It runs inside a user-facing enrich, and an unbounded provider on that path is the
    48-second "Add company" this codebase just finished removing."""
    import asyncio
    import time

    from nexus.enrichment import b2b_actor

    monkeypatch.setattr(b2b_actor, "TIMEOUT_S", 0.2)

    class _Client:
        async def run_actor(self, actor, run_input, **kw):
            await asyncio.sleep(30)
            return [ROW]

    monkeypatch.setattr(
        "nexus.integrations.apify.get_apify_client", lambda: _Client(), raising=False
    )
    started = time.monotonic()
    assert await b2b_actor.fetch("stripe.com") == {}
    assert time.monotonic() - started < 5


# ---- the ordering ----------------------------------------------------------------------------------

def test_the_actor_is_tried_before_the_web_path():
    """The whole point of the change. If the search runs first, a rate-limited model chain still
    produces the 66-second empty answer this replaced."""
    import inspect

    from nexus.enrichment.account import SearchBackedAccountEnricher

    src = inspect.getsource(SearchBackedAccountEnricher.enrich)
    assert src.index("from_b2b_actor") < src.index("self.fetch("), (
        "the web+LLM path runs before the structured actor again"
    )


def test_a_good_actor_answer_stops_the_search():
    """"Prefer it, and go to the search only when it found nothing." The `industry and
    employee_count` gate below is not enough on its own: the actor's free providers rarely return a
    headcount, so a run that filled industry, description, geography and tech stack still fell
    through to the search and the LLM completion — the pair this ordering exists to avoid."""
    import inspect

    from nexus.enrichment.account import SearchBackedAccountEnricher

    src = inspect.getsource(SearchBackedAccountEnricher.enrich)
    assert "if actor_filled and account.industry:" in src


def test_the_actor_is_registered_with_a_consumer():
    from nexus.enrichment.b2b_actor import ACTOR
    from nexus.integrations.apify import ACTORS

    assert ACTOR in ACTORS
    assert ACTORS[ACTOR] == "teodor_banea~b2b-lead-enrichment-free"
