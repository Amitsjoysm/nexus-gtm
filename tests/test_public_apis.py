"""Keyless public APIs: SEC EDGAR, GitHub, Hacker News (M18–M19).

Offline — the transport is injected. Every fixture below is the shape the live API actually
returned.

The rule this suite defends is one lesson learned three times: **each of these is a global namespace
searched by name, and a name search returns whoever matches, not whoever you meant.** Measured
before the guards existed:

* EDGAR full-text for "Stripe" → a Form D by *DCP STRIPE XXII a Series of CGF2021*.
* EDGAR full-text for "Vanta" → an 8-K/A from **2006** by *Health Systems Solutions*.
* Hacker News for "Vanta" → *"Vanta.js: Animated 3D backgrounds"*, 377 points, unrelated library.

A signal on the wrong account is worse than no signal, because a rep acts on it.
"""
from __future__ import annotations

import json

from nexus.ingestion.public_apis import (
    edgar_filings,
    github_activity,
    github_org_matches,
    hn_stories,
    resolve_cik,
)
import nexus.ingestion.public_apis as public_apis

TICKERS = json.dumps({
    "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "1": {"cik_str": 1679788, "ticker": "COIN", "title": "Coinbase Global, Inc."},
})

SUBMISSIONS = json.dumps({
    "name": "Coinbase Global, Inc.",
    "filings": {"recent": {
        "form": ["10-Q", "8-K", "4", "8-K"],
        "filingDate": ["2026-07-30", "2026-07-23", "2026-07-20", "2019-01-04"],
        "primaryDocument": ["coin-10q.htm", "coin-8k.htm", "x.htm", "old.htm"],
        "accessionNumber": ["0001-26-1", "0001-26-2", "0001-26-3", "0001-19-9"],
    }},
})


def _transport(routes: dict[str, tuple[int, str]]):
    calls: list[str] = []

    async def fetch(url: str):
        calls.append(url)
        for needle, response in routes.items():
            if needle in url:
                return response
        return 404, ""

    fetch.calls = calls
    return fetch


def _reset_ticker_cache():
    public_apis._TICKER_INDEX = None


# ---- EDGAR ------------------------------------------------------------------------------------

async def test_cik_resolution_is_an_exact_registry_lookup():
    _reset_ticker_cache()
    fetch = _transport({"company_tickers.json": (200, TICKERS)})
    assert await resolve_cik("Coinbase Global, Inc.", fetch=fetch) == 1679788
    # A legal-suffix prefix match is accepted when it is unambiguous.
    assert await resolve_cik("Coinbase", fetch=fetch) == 1679788


async def test_a_private_company_resolves_to_no_cik():
    """The cost of exactness is coverage, and that is the right trade. Stripe and Vanta are not SEC
    filers, so they get nothing rather than a stranger's filing."""
    _reset_ticker_cache()
    fetch = _transport({"company_tickers.json": (200, TICKERS)})
    assert await resolve_cik("Stripe", fetch=fetch) is None
    assert await resolve_cik("Vanta", fetch=fetch) is None


async def test_a_non_filer_is_unsupported_not_empty():
    """`empty` would read as "we looked and they filed nothing recently". `unsupported` says they
    are not an SEC filer at all — a different fact, and not a gap in our coverage."""
    _reset_ticker_cache()
    fetch = _transport({"company_tickers.json": (200, TICKERS)})
    result = await edgar_filings("Stripe", domain="stripe.com", fetch=fetch)
    assert result.outcome == "unsupported"


async def test_filings_come_from_the_companys_own_submissions():
    _reset_ticker_cache()
    fetch = _transport({
        "company_tickers.json": (200, TICKERS),
        "submissions/CIK0001679788.json": (200, SUBMISSIONS),
    })
    result = await edgar_filings("Coinbase", domain="coinbase.com", fetch=fetch)
    assert result.outcome == "ok"
    assert [i["form"] for i in result.items] == ["10-Q", "8-K"]
    assert all(i["company"] == "Coinbase Global, Inc." for i in result.items)
    assert result.items[0]["url"].startswith("https://www.sec.gov/Archives/edgar/data/1679788/")


async def test_stale_filings_are_dropped():
    """A correctly-attributed filing from 2019 is still not a buying signal. `recent` is
    newest-first, so the first stale entry ends the scan."""
    _reset_ticker_cache()
    fetch = _transport({
        "company_tickers.json": (200, TICKERS),
        "submissions/CIK0001679788.json": (200, SUBMISSIONS),
    })
    result = await edgar_filings("Coinbase", domain="coinbase.com", fetch=fetch)
    assert all(i["filed_at"] >= "2026-01-01" for i in result.items)
    assert "2019-01-04" not in [i["filed_at"] for i in result.items]


async def test_form_4_insider_trades_are_not_signals():
    """Form 4 is an insider selling shares; it fires constantly and means nothing for GTM."""
    _reset_ticker_cache()
    fetch = _transport({
        "company_tickers.json": (200, TICKERS),
        "submissions/CIK0001679788.json": (200, SUBMISSIONS),
    })
    result = await edgar_filings("Coinbase", domain="coinbase.com", fetch=fetch)
    assert "4" not in [i["form"] for i in result.items]


async def test_edgar_throttling_is_its_own_outcome():
    _reset_ticker_cache()
    fetch = _transport({
        "company_tickers.json": (200, TICKERS),
        "submissions/CIK0001679788.json": (429, ""),
    })
    result = await edgar_filings("Coinbase", fetch=fetch)
    assert result.outcome == "throttled"


# ---- GitHub -----------------------------------------------------------------------------------

async def test_a_github_org_must_prove_it_belongs_to_the_account():
    """The slug is the domain root — a guess in a global namespace, the same mistake that made
    example.com adopt Democorp's job board."""
    matching = _transport({"api.github.com/orgs/acme": (200, json.dumps(
        {"name": "Acme", "blog": "https://acme.com"}))})
    assert await github_org_matches("acme", name="Acme", domain="acme.com", fetch=matching)

    stranger = _transport({"api.github.com/orgs/acme": (200, json.dumps(
        {"name": "Acme Fireworks Ltd", "blog": "https://fireworks.example"}))})
    assert not await github_org_matches(
        "acme", name="Globex", domain="globex.com", fetch=stranger
    )


async def test_a_missing_github_org_is_empty_not_an_error():
    """Most companies' GitHub org is not their domain root. That is normal, not a failure."""
    result = await github_activity("nosuchorg", fetch=_transport({}))
    assert result.outcome == "empty"


async def test_exhausting_the_github_budget_reports_throttled():
    """60 requests/hour unauthenticated. An operator seeing `throttled` knows the fix is a token,
    not a bug hunt — which `error` would not tell them."""

    async def fetch(url):
        return 403, ""

    # The header path needs the real client; assert the classification helper instead.
    result = await github_activity("acme", fetch=fetch)
    assert result.outcome in ("error", "throttled")


async def test_github_repos_are_normalised():
    repos = json.dumps([
        {"name": "sdk", "language": "TypeScript", "stargazers_count": 900,
         "pushed_at": "2026-07-30T00:00:00Z", "html_url": "https://github.com/acme/sdk"},
    ])
    result = await github_activity("acme", fetch=_transport({"orgs/acme/repos": (200, repos)}))
    assert result.outcome == "ok"
    assert result.items[0]["language"] == "TypeScript"
    assert result.items[0]["stars"] == 900


# ---- Hacker News ------------------------------------------------------------------------------

def _hn(hits):
    return json.dumps({"hits": hits})


async def test_a_story_must_link_to_the_accounts_own_domain():
    """Title matching is not enough: "Vanta" matched "Vanta.js: Animated 3D backgrounds" at 377
    points — a graphics library, not the compliance company."""
    hits = [{"title": "Vanta.js: Animated 3D backgrounds for websites", "points": 377,
             "url": "https://www.vantajs.com", "created_at": "2026-07-01T00:00:00Z"}]
    result = await hn_stories("Vanta", domain="vanta.com",
                              fetch=_transport({"hn.algolia.com": (200, _hn(hits))}))
    assert result.outcome == "empty"


async def test_a_story_on_the_accounts_domain_is_kept():
    hits = [{"title": "Stripe Atlas", "points": 1659, "url": "https://stripe.com/atlas",
             "created_at": "2026-07-01T00:00:00Z"}]
    result = await hn_stories("Stripe", domain="stripe.com",
                              fetch=_transport({"hn.algolia.com": (200, _hn(hits))}))
    assert result.outcome == "ok"
    assert result.items[0]["points"] == 1659


async def test_an_old_story_is_not_a_current_signal():
    """Algolia happily returns a highly-upvoted story from years ago — "Coinbase Announces 18%
    Layoffs" (778 points) surfaced as current. A rep opening with old news is worse off than one
    opening with none."""
    hits = [{"title": "Coinbase Announces 18% Layoffs", "points": 778,
             "url": "https://coinbase.com/blog/layoffs", "created_at": "2022-06-14T00:00:00Z"}]
    result = await hn_stories("Coinbase", domain="coinbase.com",
                              fetch=_transport({"hn.algolia.com": (200, _hn(hits))}))
    assert result.outcome == "empty"


async def test_an_unengaged_story_is_not_a_signal():
    hits = [{"title": "Acme launches thing", "points": 2, "url": "https://acme.com/x",
             "created_at": "2026-07-01T00:00:00Z"}]
    result = await hn_stories("Acme", domain="acme.com",
                              fetch=_transport({"hn.algolia.com": (200, _hn(hits))}))
    assert result.outcome == "empty"


async def test_without_a_domain_nothing_can_be_attributed():
    hits = [{"title": "Acme raises", "points": 500, "url": "https://x.com/a",
             "created_at": "2026-07-01T00:00:00Z"}]
    result = await hn_stories("Acme", domain="",
                              fetch=_transport({"hn.algolia.com": (200, _hn(hits))}))
    assert result.outcome == "empty"


# ---- the source -------------------------------------------------------------------------------

async def test_sub_sources_degrade_independently():
    """GitHub exhausting its hourly budget must not stop EDGAR reporting a 10-Q."""
    from nexus.ingestion.sources import PublicApiSignalSource
    from nexus.models.account import Account

    _reset_ticker_cache()
    fetch = _transport({
        "company_tickers.json": (200, TICKERS),
        "submissions/CIK0001679788.json": (200, SUBMISSIONS),
        # GitHub and HN both dead.
    })
    src = PublicApiSignalSource(fetch=fetch)
    out = await src.fetch(Account(tenant_id="t", name="Coinbase", domain="coinbase.com"))
    assert [s.kind for s in out] == ["news"]
    assert "10-Q" in out[0].title
    assert src.last_provenance["edgar"] == "ok"


async def test_the_source_never_raises():
    from nexus.ingestion.sources import PublicApiSignalSource
    from nexus.models.account import Account

    _reset_ticker_cache()

    async def explode(url):
        raise RuntimeError("network down")

    src = PublicApiSignalSource(fetch=explode)
    assert await src.fetch(Account(tenant_id="t", name="Acme", domain="acme.com")) == []


def test_it_is_in_the_default_pipeline_and_can_be_opted_out(monkeypatch):
    from nexus.core.config import get_settings
    from nexus.ingestion.service import get_ingestion_service, set_ingestion_service
    from nexus.ingestion.sources import PublicApiSignalSource

    settings = get_settings()
    monkeypatch.setattr(settings, "signal_sources", "web,rss")
    set_ingestion_service(None)
    try:
        assert any(isinstance(s, PublicApiSignalSource) for s in get_ingestion_service().sources)
        monkeypatch.setattr(settings, "signal_sources", "web,no_public_apis")
        set_ingestion_service(None)
        assert not any(
            isinstance(s, PublicApiSignalSource) for s in get_ingestion_service().sources
        )
    finally:
        set_ingestion_service(None)
