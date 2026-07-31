"""Dork-backed signal search. Offline — the search provider is injected.

What these actually protect: the precision rules. A dork that returns a ZoomInfo profile page, an
eight-year-old funding round, or a news-site index page is worse than no dork at all, because each
one becomes an Inbox task a rep has to dismiss.
"""
from __future__ import annotations

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
    assert captured["startPublishedDate"] < "2026-07-30"


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
