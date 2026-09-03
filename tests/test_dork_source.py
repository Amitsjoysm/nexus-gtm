"""Dork-backed signal search. Offline — the search provider is injected.

What these actually protect: the precision rules. A dork that returns a ZoomInfo profile page, an
eight-year-old funding round, or a news-site index page is worse than no dork at all, because each
one becomes an Inbox task a rep has to dismiss.
"""
from __future__ import annotations

import pytest

from datetime import datetime, timezone

from nexus.ingestion.dorks import DORKS, DORKS_BY_SLUG, select_dorks
from nexus.ingestion.sources import DorkedSearchSource, event_dedupe_key
from nexus.models.account import Account

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


class FakeSearch:
    """Records queries and replays canned hits, keyed by a substring of the query.

    Declares the ``operator`` dialect because the canned hits below are keyed on ``site:`` terms —
    these cases exercise the operator rendering specifically. The plain and semantic forms have
    their own suite in ``test_search_dialects.py``.
    """

    query_dialect = "operator"

    def __init__(self, by_substring: dict[str, list[dict]] | None = None):
        self.queries: list[str] = []
        self._by = by_substring or {}
        self.recency_days: list[int] = []

    async def search(self, query: str, *, limit: int = 5):
        self.queries.append(query)
        for needle, hits in self._by.items():
            if needle in query:
                return hits[:limit]
        return []

    async def search_recent(self, query: str, *, limit: int = 5, days: int = 90):
        self.recency_days.append(days)
        return await self.search(query, limit=limit)


def _account(**kw) -> Account:
    base = dict(name="Acme Corp", domain="acme.com", industry="Fintech")
    base.update(kw)
    return Account(**base)


# ---- the library ----------------------------------------------------------------------------

def test_every_dork_renders_without_leftover_placeholders():
    for dork in DORKS:
        q = dork.render(name="Acme Corp", domain="acme.com", industry="Fintech", now=NOW,
                        dialect="operator")
        assert "{" not in q and "}" not in q, f"{dork.slug} left a placeholder: {q}"
        assert q.strip()


def test_site_groups_are_or_ed_not_and_ed():
    """Repeated `site:` terms read as an impossible AND on every engine and return nothing —
    the single easiest way to ship a dork that silently finds zero results forever."""
    q = DORKS_BY_SLUG["funding_press"].render(
        name="Acme", domain="acme.com", industry="", now=NOW, dialect="operator"
    )
    assert "(site:techcrunch.com OR site:" in q
    # ...and the sites must be inside one parenthesised group, not scattered.
    assert q.count("(site:") == 1


def test_dorks_carry_the_current_year_for_recency():
    """The portable recency lever. DuckDuckGo has no date filter, so if the year is not in the
    query the 2019 round outranks last week's on link authority."""
    q = DORKS_BY_SLUG["funding_press"].render(
        name="Acme", domain="acme.com", industry="", now=NOW, dialect="operator"
    )
    assert "2026" in q
    assert "inurl:2026" in q


def test_noise_aggregators_are_excluded():
    """A ZoomInfo profile matches the account name perfectly and reports no event at all."""
    q = DORKS_BY_SLUG["funding_wire"].render(
        name="Acme", domain="acme.com", industry="", now=NOW, dialect="operator"
    )
    assert "-site:zoominfo.com" in q
    assert "-site:glassdoor.com" in q


def test_domain_dorks_are_dropped_without_a_domain():
    """Rendering `site:` with an empty value produces a query matching the entire web — a
    precision tool turned into a noise generator."""
    with_domain = select_dorks(has_domain=True, limit=99)
    without = select_dorks(has_domain=False, limit=99)
    assert len(without) < len(with_domain)
    assert all("{domain}" not in d.template for d in without)


def test_selection_respects_the_query_budget():
    """Each dork is a billed search call; the budget is the cost dial."""
    assert len(select_dorks(has_domain=True, limit=2)) == 2
    assert select_dorks(has_domain=True, limit=0) == []


def test_the_highest_value_signal_is_first():
    """The budget cuts from the end, so ordering decides what a small budget buys."""
    assert DORKS[0].kind == "funding"


# ---- fetch behaviour ------------------------------------------------------------------------

async def test_it_runs_one_query_per_signal_kind():
    search = FakeSearch()
    src = DorkedSearchSource(search=search, max_queries=4)
    await src.fetch(_account())
    assert len(search.queries) == 4
    # Distinct queries — the whole point is that they are not one query repeated.
    assert len(set(search.queries)) == 4


async def test_it_asks_for_recent_results():
    search = FakeSearch()
    src = DorkedSearchSource(search=search, max_queries=2, recency_days=45)
    await src.fetch(_account())
    assert search.recency_days == [45, 45]


async def test_an_account_with_no_name_yields_nothing():
    search = FakeSearch()
    src = DorkedSearchSource(search=search)
    assert await src.fetch(_account(name="")) == []
    assert search.queries == []


async def test_a_dead_search_provider_yields_no_signals():
    """Never raises across the boundary: a broken provider costs signals, not ingestion."""

    class Broken:
        async def search_recent(self, query, *, limit=5, days=90):
            raise RuntimeError("provider down")

    src = DorkedSearchSource(search=Broken(), max_queries=3)
    assert await src.fetch(_account()) == []


# ---- precision ------------------------------------------------------------------------------

async def test_an_open_web_hit_must_name_the_account():
    """A generic news-site index ranks well and says nothing — the exact junk that polluted the
    inbox before the name-match gate existed."""
    search = FakeSearch({
        "techcrunch.com": [
            {"title": "Fintech News and Analysis | Fintech Dive",
             "url": "https://fintechdive.com", "snippet": "Latest funding rounds and more"},
        ],
    })
    src = DorkedSearchSource(search=search, max_queries=1)
    assert await src.fetch(_account()) == []


async def test_an_open_web_hit_must_carry_the_event_vocabulary():
    """Naming the company is not enough: its About page ranks for the name and reports no event."""
    search = FakeSearch({
        "techcrunch.com": [
            {"title": "Acme Corp — Company Profile", "url": "https://x.com/acme",
             "snippet": "Acme Corp is a fintech company headquartered in Boston."},
        ],
    })
    src = DorkedSearchSource(search=search, max_queries=1)
    assert await src.fetch(_account()) == []


async def test_a_real_funding_hit_becomes_a_funding_signal():
    search = FakeSearch({
        "techcrunch.com": [
            {"title": "Acme Corp raises $40M Series B",
             "url": "https://techcrunch.com/2026/07/acme-series-b",
             "snippet": "Acme Corp raised a $40 million Series B led by Sequoia."},
        ],
    })
    src = DorkedSearchSource(search=search, max_queries=1)
    out = await src.fetch(_account())
    assert len(out) == 1
    assert out[0].kind == "funding"
    assert out[0].strength >= 0.85
    assert out[0].url.endswith("acme-series-b")
    assert out[0].source == "dork"


async def test_an_ats_hit_skips_the_name_gate():
    """A job posting titled "Senior Platform Engineer" names the company nowhere, but a hit on the
    company's own Greenhouse board is about that company by construction."""
    search = FakeSearch({
        "boards.greenhouse.io": [
            {"title": "Senior Platform Engineer",
             "url": "https://boards.greenhouse.io/acmecorp/jobs/123", "snippet": "Remote, US"},
        ],
    })
    src = DorkedSearchSource(search=search, max_queries=99)
    out = await src.fetch(_account())
    postings = [s for s in out if s.kind == "job_posting"]
    assert postings, "an ATS hit must survive the name gate"
    assert postings[0].strength == DORKS_BY_SLUG["hiring_ats"].strength


async def test_a_dork_does_not_inflate_a_weaker_event():
    """A funding dork routinely surfaces acquisitions — the same publishers cover both. The signal
    must carry the acquisition's strength, not funding's, or every dork hit is a 0.9 regardless of
    what actually happened."""
    search = FakeSearch({
        "techcrunch.com": [
            {"title": "Acme Corp acquires Globex",
             "url": "https://techcrunch.com/2026/07/acme-globex",
             "snippet": "Acme Corp acquires Globex for $40 million in cash."},
        ],
    })
    src = DorkedSearchSource(search=search, max_queries=1)
    out = await src.fetch(_account())
    assert len(out) == 1
    # Passed the funding dork's relevance floor on "$"/"million", but the classifier read the
    # actual event, and the dork's 0.9 was not applied because the kinds disagree.
    assert out[0].kind == "news"
    assert out[0].strength < 0.9


# ---- dedupe ---------------------------------------------------------------------------------

async def test_two_outlets_covering_one_round_collapse_to_one_signal():
    """The observed failure this bucketing exists for: 9 distinct funding URLs for one account in
    two weeks, each re-alerting a completed account."""
    hits = [
        {"title": "Acme Corp raises $40M Series B", "url": "https://a.com/1",
         "snippet": "Acme raised a Series B."},
        {"title": "Acme Corp closes Series B round", "url": "https://b.com/2",
         "snippet": "Acme Corp raised $40 million."},
    ]
    search = FakeSearch({"techcrunch.com": hits})
    src = DorkedSearchSource(search=search, max_queries=1, per_query=5)
    out = await src.fetch(_account())
    assert len(out) == 1


def test_the_bucketing_rule_is_shared_with_the_broad_source():
    """Both search-backed sources route through one helper, so a round found by both becomes one
    signal. Two copies of this rule would drift and one source would start re-alerting."""
    a = event_dedupe_key("funding", "acme.com", 0.9, NOW)
    b = event_dedupe_key("funding", "acme.com", 0.85, NOW)
    assert a == b == "funding:acme.com:2026-07"
    # A weak mention and a real event stay in separate weekly buckets.
    assert event_dedupe_key("news", "acme.com", 0.6, NOW) != event_dedupe_key(
        "news", "acme.com", 0.3, NOW
    )


# ---- wiring ---------------------------------------------------------------------------------

def test_the_dork_source_is_in_the_default_pipeline(monkeypatch):
    from nexus.core.config import get_settings
    from nexus.ingestion.service import get_ingestion_service, set_ingestion_service
    from nexus.ingestion.sources import DorkedSearchSource as DS, WebNewsSource

    monkeypatch.setattr(get_settings(), "signal_sources", "demo")
    set_ingestion_service(None)
    try:
        sources = get_ingestion_service().sources
        assert any(isinstance(s, DS) for s in sources)
        # Alongside, not instead of: they fail differently.
        assert any(isinstance(s, WebNewsSource) for s in sources)
    finally:
        set_ingestion_service(None)


def test_no_dorks_removes_it_without_touching_the_rest(monkeypatch):
    """The cost lever: every dork is a billed search call."""
    from nexus.core.config import get_settings
    from nexus.ingestion.service import get_ingestion_service, set_ingestion_service
    from nexus.ingestion.sources import DorkedSearchSource as DS, WebNewsSource

    monkeypatch.setattr(get_settings(), "signal_sources", "demo,no_dorks")
    set_ingestion_service(None)
    try:
        sources = get_ingestion_service().sources
        assert not any(isinstance(s, DS) for s in sources)
        assert any(isinstance(s, WebNewsSource) for s in sources)
    finally:
        set_ingestion_service(None)


# ---- provider recency capability -------------------------------------------------------------

async def test_search_recent_falls_back_to_search_by_default():
    """Recency is a preference, not a requirement. Returning [] on a provider that cannot filter
    by date would make the whole dork library useless on the keyless default."""
    from nexus.integrations.search.provider import SearchHit, SearchProvider

    class Plain(SearchProvider):
        name = "plain"

        async def search(self, query, *, limit=5):
            return [SearchHit(title="t", url="u")]

    assert len(await Plain().search_recent("q", days=30)) == 1


async def test_exa_sends_a_real_published_date_floor(monkeypatch):
    """Exa is the only adapter that can actually enforce recency; no query string substitutes for
    an index-level date filter."""
    from nexus.integrations.search.engines import ExaSearchProvider

    captured: dict = {}
    exa = ExaSearchProvider(api_key="k")

    async def fake_post(endpoint, payload, limit):
        captured.update(payload)
        return []

    monkeypatch.setattr(exa, "_post", fake_post)
    await exa.search_recent("acme funding", limit=3, days=30)
    assert "startPublishedDate" in captured
    # Relative to TODAY, not a literal date. This asserted `< "2026-07-30"`, which was true when it
    # was written and became false the moment the calendar passed the floor it hard-coded — the
    # test then failed on a clean tree for a reason that had nothing to do with the code. A date
    # floor is inherently relative, so the assertion has to be too.
    from datetime import timedelta

    from nexus.core.db import utcnow

    expected = (utcnow() - timedelta(days=30)).date().isoformat()
    assert captured["startPublishedDate"][:10] == expected, (
        f"expected a floor 30 days back ({expected}), got {captured['startPublishedDate']!r}"
    )


async def test_a_keyless_exa_does_not_touch_the_network():
    from nexus.integrations.search.engines import ExaSearchProvider

    assert await ExaSearchProvider(api_key="").search_recent("q") == []


# ---- precision rules found by running against live search --------------------------------------

async def test_an_industry_roundup_that_merely_mentions_the_account_is_rejected():
    """Live Firecrawl returned "Cybersecurity Startup Investors Pulled Back In Q3" for Vanta and it
    scored funding 0.90 — the article mentions them in passing. A story genuinely *about* a
    company's round names it in the headline; a market survey listing it does not."""
    search = FakeSearch({
        "techcrunch.com": [
            {"title": "Cybersecurity Startup Investors Pulled Back In Q3",
             "url": "https://news.crunchbase.com/venture/cyber-q3/",
             "snippet": "Acme Corp and others raised less this quarter. Total funding fell."},
        ],
    })
    src = DorkedSearchSource(search=search, max_queries=1)
    assert await src.fetch(_account()) == []


async def test_the_event_is_read_from_the_headline_not_the_page_body():
    """A headline states what happened; a body mentions everything the company has ever done. Live
    Firecrawl returned a product page — "Vanta Delivers: Vanta control framework" — whose text
    recalled an earlier round, and it scored funding 0.85."""
    search = FakeSearch({
        "techcrunch.com": [
            {"title": "Acme Corp Delivers: Acme control framework",
             "url": "https://acme.com/resources/framework",
             "snippet": "Since Acme Corp raised its $40 million Series B, the team has shipped..."},
        ],
    })
    src = DorkedSearchSource(search=search, max_queries=1)
    out = await src.fetch(_account())
    # It still passes the relevance floor on the snippet, but it is not a funding event.
    assert [s.kind for s in out] == ["news"]
    assert out[0].strength < 0.85


async def test_a_headline_funding_story_still_scores_full_strength():
    """The counterweight: tightening precision must not cost the true positives. This is the exact
    headline live Firecrawl returned for Ramp."""
    search = FakeSearch({
        "techcrunch.com": [
            {"title": "Acme Corp Raises Series F at $44 Billion Valuation",
             "url": "https://www.prnewswire.com/news-releases/acme-series-f",
             "snippet": "Acme Corp announced the round today."},
        ],
    })
    src = DorkedSearchSource(search=search, max_queries=1)
    out = await src.fetch(_account())
    assert len(out) == 1
    assert out[0].kind == "funding"
    assert out[0].strength == 0.9


# ---- strict name attribution (found live) ------------------------------------------------------

def test_a_multiword_name_needs_more_than_one_common_token():
    """Live false positive: the LinkedIn title "Included Health - Member Care Advocate (MCA)" was
    attributed to *Advocate Health Care*, because it shares "advocate", "health" and "care". Any
    single token is too weak a match for a name built from common words."""
    from nexus.ingestion.sources import names_account

    advocate = Account(tenant_id="t", name="Advocate Health Care",
                       domain="advocatehealth.com")
    assert not names_account("Included Health - Member Care Advocate (MCA)", advocate)
    # ...but the real thing still matches, by phrase or by domain root.
    assert names_account("Advocate Health Care names a new CFO", advocate)
    assert names_account("advocatehealth.com launches a patient portal", advocate)


def test_a_single_token_name_matches_on_that_token():
    """There is nothing stronger available for "Ramp" or "Vanta", and such names are distinctive
    precisely because they are one word."""
    from nexus.ingestion.sources import names_account

    ramp = Account(tenant_id="t", name="Ramp", domain="ramp.com")
    assert names_account("Ramp Reaches $32 Billion Valuation", ramp)
    assert names_account("Software Engineer, GTM Platform @ Ramp", ramp)


def test_an_unrelated_headline_is_rejected():
    from nexus.ingestion.sources import names_account

    vanta = Account(tenant_id="t", name="Vanta", domain="vanta.com")
    assert not names_account("Okta announces new pricing", vanta)
    assert not names_account("", vanta)


def test_the_domain_root_is_the_strongest_evidence():
    """Unique by construction, so it beats any name heuristic."""
    from nexus.ingestion.sources import names_account

    acct = Account(tenant_id="t", name="The Big Company Group", domain="bigco.com")
    assert names_account("bigco.com ships a new API", acct)


# ---- the budget has to cover the tail, not the median -------------------------------------------

def test_the_query_budget_has_headroom_over_a_real_query():
    """Measured on the live deployment, 2026-08-29 to 2026-09-03:

        outcome   runs   avg duration   max duration
        ok           8       13,198ms       15,328ms
        timeout    399       16,370ms       20,579ms

    The budget was `max_queries * 4.0` = 16.0s for four queries, and the two populations are
    separated by that ceiling and nothing else — every run that finished did so at 13-15s, every
    run that died hit 16s. 399 timeouts against 8 successes is not a slow provider, it is a budget
    set at the median of a distribution it was supposed to bound.

    4.0s per query is a point estimate for a network call whose cost is a distribution. The
    allowance has to sit in the tail: a source killed mid-run reports nothing, and "nothing" is
    indistinguishable from "this account has no signals", which is exactly how this stayed
    invisible for five days while every dork run failed.
    """
    from nexus.ingestion.sources import DorkedSearchSource

    src = DorkedSearchSource(max_queries=4)
    per_query = src.timeout_s / 4
    assert per_query >= 7.0, (
        f"{per_query:.1f}s per query leaves no room above the measured ~3.8s median; "
        "the observed max for a completed run was 15.3s and killed runs reached 20.6s"
    )
    assert src.timeout_s >= 20.0, (
        f"budget {src.timeout_s}s does not cover the 20.6s a real four-query run has taken"
    )


def test_pacing_is_added_on_top_of_the_query_budget():
    """The keyless backend sleeps between queries to stay under DuckDuckGo's anti-bot heuristics.
    That sleep is dead time inside the same budget, so it has to be added, not absorbed —
    otherwise turning pacing on silently converts a working source into a timing-out one."""
    from nexus.ingestion.sources import DorkedSearchSource

    unpaced = DorkedSearchSource(max_queries=4, pace_s=0.0)
    paced = DorkedSearchSource(max_queries=4, pace_s=1.5)
    # Three gaps between four queries.
    assert paced.timeout_s == pytest.approx(unpaced.timeout_s + 4.5)


def test_a_single_query_source_still_gets_a_sane_floor():
    """`NEXUS_SIGNAL_DORK_MAX_QUERIES=1` must not produce a budget so tight that one slow request
    kills it. The floor is what stops the formula collapsing at small values."""
    from nexus.ingestion.sources import DorkedSearchSource

    assert DorkedSearchSource(max_queries=1).timeout_s >= 20.0


def test_the_budget_stays_in_line_with_its_sibling_sources():
    """Sources run CONCURRENTLY, so an account's crawl is bounded by the slowest one. A dork budget
    far above the others would raise the floor on every account refresh for one source's benefit."""
    from nexus.ingestion.sources import (
        AtsSignalSource,
        DorkedSearchSource,
        PublicApiSignalSource,
        WebsiteWatchSignalSource,
    )

    siblings = max(
        AtsSignalSource.timeout_s,
        PublicApiSignalSource.timeout_s,
        WebsiteWatchSignalSource.timeout_s,
    )
    assert DorkedSearchSource(max_queries=4).timeout_s <= siblings + 5.0, (
        "the dork budget now sets the per-account crawl ceiling on its own"
    )


# ---- a dead key pool must not read as a quiet market --------------------------------------------

async def test_a_condemned_key_pool_is_recorded_not_reported_as_empty():
    """Observed live: all three Firecrawl keys returned 402 (credits exhausted), so every dork run
    found nothing and `signal_source_runs` recorded `empty`.

    `empty` is deliberately not `ok` in this codebase precisely so a broken source stays visible —
    but recording a CREDENTIALS failure as `empty` hides it one level deeper, because `empty` is
    also the honest answer for an account with no news. The distinction only existed in a log line.

    The runtime write-back that would normally catch this (`_record_rejection` marking the key row
    red) does not apply here: these keys come from the environment pool, so there is no row to
    mark. The crawl-history row is the only surface left, and it was saying the wrong thing.

    A provider that has condemned its whole pool returns `[]`, which is why the existing
    `failed` flag never fired — that only triggers on `None`. So the provider now states the
    failure, and the source records it and stops rather than spending three more billed calls on
    a pool it already knows is dead.
    """
    from nexus.ingestion.sources import DorkedSearchSource
    from nexus.models.account import Account

    class _DeadPool:
        name = "firecrawl"
        query_dialect = "operator"
        last_failure = "every key in the 3-key pool was rejected (#0:402, #1:402, #2:402)"

        async def search(self, query, *, limit=5):
            return []

        async def search_recent(self, query, *, limit=5, days=90, include_domains=None,
                                exclude_domains=None):
            return []

    src = DorkedSearchSource(search=_DeadPool(), max_queries=4)
    out = await src.fetch(Account(name="Vanta", domain="vanta.com"))

    assert out == []
    assert "402" in str(src.last_provenance.get("provider_failure", "")), (
        f"the dead pool is invisible in the crawl history: {src.last_provenance}"
    )
    assert len(src.last_provenance["queries"]) == 1, (
        "kept spending billed queries against a pool it already knew was condemned"
    )


async def test_a_genuinely_quiet_account_is_still_just_empty():
    """The other half: no results from a HEALTHY provider stays a plain empty run, with no
    failure recorded. Otherwise every quiet account would look like an outage."""
    from nexus.ingestion.sources import DorkedSearchSource
    from nexus.models.account import Account

    class _Healthy:
        name = "firecrawl"
        query_dialect = "operator"
        last_failure = ""

        async def search(self, query, *, limit=5):
            return []

        async def search_recent(self, query, *, limit=5, days=90, include_domains=None,
                                exclude_domains=None):
            return []

    src = DorkedSearchSource(search=_Healthy(), max_queries=4)
    assert await src.fetch(Account(name="Vanta", domain="vanta.com")) == []
    assert not src.last_provenance.get("provider_failure")
    assert len(src.last_provenance["queries"]) == 4, "a healthy provider should get the full batch"


async def test_a_recovered_pool_stops_reporting_a_failure():
    """A sticky failure flag is worse than none.

    Once set, it would mark every later run as a provider failure — so topping up the credits
    would fix the searches while the crawl history kept saying they were broken, and the operator
    would be chasing a problem that no longer exists. It has to describe the LAST attempt, not the
    worst one ever seen.
    """
    from nexus.ingestion.sources import DorkedSearchSource
    from nexus.models.account import Account

    class _Recovering:
        name = "firecrawl"
        query_dialect = "operator"

        def __init__(self):
            self.last_failure = "every key in the 3-key pool was rejected (#0:402)"

        async def search(self, query, *, limit=5):
            self.last_failure = ""      # the pool works again
            return []

        async def search_recent(self, query, *, limit=5, days=90, include_domains=None,
                                exclude_domains=None):
            return await self.search(query, limit=limit)

    src = DorkedSearchSource(search=_Recovering(), max_queries=4)
    await src.fetch(Account(name="Vanta", domain="vanta.com"))
    assert not src.last_provenance.get("provider_failure"), (
        "a recovered provider is still reported as failing"
    )


def test_every_engine_clears_the_failure_before_searching():
    """Structural: the flag is reset at the top of each search, not left to each error path to
    remember. An error path that forgets to clear is exactly how a flag goes sticky."""
    import inspect

    from nexus.integrations.search import engines

    for cls_name in ("ExaSearchProvider", "FirecrawlSearchProvider"):
        cls = getattr(engines, cls_name, None)
        if cls is None:
            continue
        src = inspect.getsource(cls)
        assert 'self.last_failure = ""' in src, (
            f"{cls_name} never clears last_failure, so one bad minute is reported forever"
        )
