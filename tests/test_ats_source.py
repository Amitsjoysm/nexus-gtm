"""ATS job boards: discovery, ownership verification, and the hiring signal (M17).

Offline — the HTTP transport is injected. The shapes below are the ones the live APIs actually
returned when this was built, not invented fixtures.

The rule this suite exists to defend: a *guessed* board token must prove it belongs to the account.
Measured, `example.com` guesses the Greenhouse token `example`, which is a real board with 21 open
roles owned by a company called "Democorp" — every one of which would have become a hiring signal on
the wrong account.
"""
from __future__ import annotations

import json

from nexus.ingestion.ats import (
    BoardRef,
    discover_board,
    extract_board_ref,
    fetch_board,
    guess_refs,
    resolve_and_fetch,
)
from nexus.ingestion.sources import AtsSignalSource
from nexus.models.account import Account

# Real markup shapes seen on the live careers pages.
VANTA_CAREERS = '<div><script src="https://jobs.ashbyhq.com/vanta/embed"></script></div>'
LINEAR_CAREERS = '<a href="https://jobs.ashbyhq.com/Linear">Open roles</a>'
FIGMA_CAREERS = '<iframe src="https://boards.greenhouse.io/embed/job_board?for=figma"></iframe>'

ASHBY_BOARD = json.dumps({"apiVersion": "1", "jobs": [
    {"id": "a1", "title": "Security Engineer, Cloud", "department": "Engineering",
     "location": "Remote", "publishedAt": "2026-07-01", "isListed": True,
     "jobUrl": "https://jobs.ashbyhq.com/vanta/a1"},
    {"id": "a2", "title": "Account Executive", "department": "Sales",
     "location": "NYC", "publishedAt": "2026-07-10", "isListed": True},
    {"id": "a3", "title": "Confidential Role", "department": "Exec", "isListed": False},
]})
GREENHOUSE_BOARD = json.dumps({"meta": {"total": 2}, "jobs": [
    {"id": 1, "title": "Account Executive, AI Sales", "absolute_url": "https://gh/1",
     "location": {"name": "SF"}, "updated_at": "2026-07-02"},
    {"id": 2, "title": "Staff Engineer", "absolute_url": "https://gh/2",
     "location": {"name": "Remote"}, "updated_at": "2026-07-05"},
]})


def _transport(routes: dict[str, tuple[int, str]]):
    """Injected fetch: exact-URL routing, 404 for anything unlisted."""
    calls: list[str] = []

    async def fetch(url: str):
        calls.append(url)
        for needle, response in routes.items():
            if needle in url:
                return response
        return 404, ""

    fetch.calls = calls
    return fetch


# ---- token extraction -------------------------------------------------------------------------

def test_it_reads_the_board_token_from_a_careers_page():
    for html, provider, token in (
        (VANTA_CAREERS, "ashby", "vanta"),
        (FIGMA_CAREERS, "greenhouse", "figma"),
    ):
        ref = extract_board_ref(html)
        assert (ref.provider, ref.token) == (provider, token)


def test_token_case_is_preserved():
    """Linear's Ashby token is `Linear`. Lowercasing it produces a 404, and a 404 is
    indistinguishable from "not hiring"."""
    assert extract_board_ref(LINEAR_CAREERS).token == "Linear"


def test_embed_helpers_are_not_mistaken_for_tokens():
    assert extract_board_ref("boards.greenhouse.io/embed/job_board?for=acme").token == "acme"
    assert extract_board_ref("<p>we use greenhouse somewhere</p>") is None


async def test_discovery_walks_the_conventional_careers_paths():
    fetch = _transport({"/company/careers": (200, VANTA_CAREERS)})
    ref = await discover_board("vanta.com", fetch=fetch)
    assert (ref.provider, ref.token, ref.via) == ("ashby", "vanta", "careers_page")
    # It tried the more conventional paths first rather than jumping to the one that worked.
    assert fetch.calls[0].endswith("/careers")


# ---- ownership verification -------------------------------------------------------------------

def test_only_providers_that_expose_an_owner_may_be_guessed():
    """An unverifiable guess is worse than no guess: it produces confident, specific, wrong signals
    instead of an obvious gap."""
    providers = {r.provider for r in guess_refs("acme.com")}
    assert providers == {"greenhouse"}
    assert "ashby" not in providers and "lever" not in providers


async def test_a_guessed_board_owned_by_someone_else_is_rejected():
    """The measured collision: example.com → greenhouse `example` → a real board owned by
    "Democorp" with 21 open roles."""
    fetch = _transport({
        "boards-api.greenhouse.io/v1/boards/example/jobs": (200, GREENHOUSE_BOARD),
        "boards-api.greenhouse.io/v1/boards/example": (200, json.dumps({"name": "Democorp"})),
    })
    result = await resolve_and_fetch("example.com", account_name="Example Corp", fetch=fetch)
    assert result.outcome == "not_found"
    assert result.postings == []


async def test_a_guessed_board_owned_by_this_company_is_accepted():
    """Stripe's careers site embeds no token, so the guess is the only route — it must still work."""
    fetch = _transport({
        "boards-api.greenhouse.io/v1/boards/stripe/jobs": (200, GREENHOUSE_BOARD),
        "boards-api.greenhouse.io/v1/boards/stripe": (200, json.dumps({"name": "Stripe"})),
    })
    result = await resolve_and_fetch("stripe.com", account_name="Stripe", fetch=fetch)
    assert result.outcome == "ok"
    assert result.ref.via == "domain_guess"
    assert len(result.postings) == 2


async def test_owner_matching_tolerates_legal_suffixes():
    fetch = _transport({
        "boards-api.greenhouse.io/v1/boards/figma/jobs": (200, GREENHOUSE_BOARD),
        "boards-api.greenhouse.io/v1/boards/figma": (200, json.dumps({"name": "Figma, Inc."})),
    })
    assert (await resolve_and_fetch("figma.com", account_name="Figma", fetch=fetch)).outcome == "ok"


async def test_a_discovered_token_skips_verification():
    """The company put the token on its own careers page — that IS the evidence of ownership."""
    fetch = _transport({
        "/careers": (200, VANTA_CAREERS),
        "posting-api/job-board/vanta": (200, ASHBY_BOARD),
    })
    result = await resolve_and_fetch("vanta.com", account_name="Vanta", fetch=fetch)
    assert result.outcome == "ok"
    assert result.ref.via == "careers_page"


# ---- fetch and parse --------------------------------------------------------------------------

async def test_ashby_unlisted_requisitions_are_skipped():
    """`isListed: false` means the req exists but is not public. Reporting it would announce hiring
    the company has not."""
    fetch = _transport({"posting-api/job-board/vanta": (200, ASHBY_BOARD)})
    result = await fetch_board(BoardRef("ashby", "vanta"), fetch=fetch)
    assert [p.title for p in result.postings] == ["Security Engineer, Cloud", "Account Executive"]
    assert result.postings[0].department == "Engineering"


async def test_an_empty_board_is_not_the_same_as_a_missing_one():
    """200-with-nothing means "exists, nothing open" — real information about a company that has
    stopped hiring. 404 means they are not on this ATS at all."""
    empty = _transport({"posting-api/job-board/x": (200, json.dumps({"jobs": []}))})
    assert (await fetch_board(BoardRef("ashby", "x"), fetch=empty)).outcome == "empty"

    missing = _transport({})
    assert (await fetch_board(BoardRef("ashby", "x"), fetch=missing)).outcome == "not_found"


async def test_a_shape_change_degrades_rather_than_crashing():
    bad = _transport({"posting-api/job-board/x": (200, "not json at all")})
    result = await fetch_board(BoardRef("ashby", "x"), fetch=bad)
    assert result.outcome == "error"
    assert "JSON" in result.error


async def test_an_unknown_provider_is_an_error_not_an_exception():
    result = await fetch_board(BoardRef("workday", "x"), fetch=_transport({}))
    assert result.outcome == "error"


# ---- the signal -------------------------------------------------------------------------------

def _account(**kw) -> Account:
    base = dict(tenant_id="t", name="Vanta", domain="vanta.com")
    base.update(kw)
    return Account(**base)


async def test_it_emits_one_aggregate_signal_not_one_per_requisition():
    """Vanta has 100 open roles and Stripe 542. One signal each would bury every other signal in
    the inbox and tell a rep nothing actionable."""
    fetch = _transport({
        "/careers": (200, VANTA_CAREERS),
        "posting-api/job-board/vanta": (200, ASHBY_BOARD),
    })
    out = await AtsSignalSource(fetch=fetch).fetch(_account())
    assert len(out) == 1
    sig = out[0]
    assert sig.kind == "hiring"
    assert sig.source == "ats"
    assert "2 open roles" in sig.title
    # The body carries what a rep opens with: which teams are growing.
    assert "Engineering" in sig.body and "Sales" in sig.body


async def test_the_discovered_board_is_cached_on_the_account():
    """Discovery costs a careers-page crawl; doing it on every refresh would multiply the cost of
    the cheapest source in the pipeline."""
    fetch = _transport({
        "/careers": (200, VANTA_CAREERS),
        "posting-api/job-board/vanta": (200, ASHBY_BOARD),
    })
    account = _account()
    await AtsSignalSource(fetch=fetch).fetch(account)
    assert account.custom_fields["ats_board"] == {"provider": "ashby", "token": "vanta"}


async def test_a_cached_board_skips_discovery():
    fetch = _transport({"posting-api/job-board/vanta": (200, ASHBY_BOARD)})
    account = _account(custom_fields={"ats_board": {"provider": "ashby", "token": "vanta"}})
    out = await AtsSignalSource(fetch=fetch).fetch(account)
    assert len(out) == 1
    assert not any("/careers" in c for c in fetch.calls), fetch.calls


async def test_an_account_with_no_domain_yields_nothing():
    fetch = _transport({})
    assert await AtsSignalSource(fetch=fetch).fetch(_account(domain="")) == []
    assert fetch.calls == []


async def test_an_empty_board_produces_no_signal_but_is_recorded():
    """"They are not hiring" is not an event a rep can act on, and monthly repetition of it is
    noise. The crawl-history row still records that the source ran and found nothing."""
    fetch = _transport({
        "/careers": (200, VANTA_CAREERS),
        "posting-api/job-board/vanta": (200, json.dumps({"jobs": []})),
    })
    src = AtsSignalSource(fetch=fetch)
    assert await src.fetch(_account()) == []
    assert src.last_provenance["outcome"] == "empty"


async def test_strength_scales_with_hiring_volume():
    """A company with 100 open reqs is a materially different prospect from one with 3 — but still
    a softer signal than a funding round."""
    many = json.dumps({"jobs": [
        {"id": str(i), "title": f"Role {i}", "department": "Eng", "isListed": True}
        for i in range(60)
    ]})
    fetch = _transport({
        "/careers": (200, VANTA_CAREERS),
        "posting-api/job-board/vanta": (200, many),
    })
    big = (await AtsSignalSource(fetch=fetch).fetch(_account()))[0]

    small = _transport({
        "/careers": (200, VANTA_CAREERS),
        "posting-api/job-board/vanta": (200, ASHBY_BOARD),
    })
    little = (await AtsSignalSource(fetch=small).fetch(_account()))[0]

    assert big.strength > little.strength
    assert big.strength < 0.85          # never outranks a funding round


async def test_a_dead_board_never_breaks_ingestion():
    async def explode(url):
        raise RuntimeError("network down")

    assert await AtsSignalSource(fetch=explode).fetch(_account()) == []


def test_the_source_is_in_the_default_pipeline(monkeypatch):
    from nexus.core.config import get_settings
    from nexus.ingestion.service import get_ingestion_service, set_ingestion_service

    monkeypatch.setattr(get_settings(), "signal_sources", "web,rss")
    set_ingestion_service(None)
    try:
        assert any(isinstance(s, AtsSignalSource) for s in get_ingestion_service().sources)
    finally:
        set_ingestion_service(None)


def test_no_ats_opts_out(monkeypatch):
    from nexus.core.config import get_settings
    from nexus.ingestion.service import get_ingestion_service, set_ingestion_service

    monkeypatch.setattr(get_settings(), "signal_sources", "web,no_ats")
    set_ingestion_service(None)
    try:
        assert not any(isinstance(s, AtsSignalSource) for s in get_ingestion_service().sources)
    finally:
        set_ingestion_service(None)


def test_it_claims_a_timeout_budget_above_the_shared_default():
    """Discovery is several requests plus a board fetch; the shared 8s default assumes one."""
    assert AtsSignalSource.timeout_s > 8.0
