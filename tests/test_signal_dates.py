"""A signal is dated when it HAPPENED, and says so when all we know is when we found it.

Customers reported the day filter "not working". It was working — on the wrong date. No source ever
set `occurred_at`, so every signal was stamped the moment it was collected: measured on the local
database, 1,099 of 1,112 signals dated within ten minutes of collection and none older than the day
collection began. "Last 7 days" meant "found in the last 7 days", so a freshly crawled account
showed the same list under every window. RSS `pubDate`, SEC `filed_at` and Hacker News `created_at`
were all read and thrown away.

Decided with the product owner 2026-09-23: use the real date wherever one exists, and otherwise the
date first found, labelled so a rep can tell the two apart. Nothing disappears.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexus.core.dates import date_from_url, parse_when
from nexus.core.db import utcnow
from nexus.models.account import Account

UTC = timezone.utc
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


# ---- the parser --------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("Tue, 04 Mar 2025 10:00:00 GMT", datetime(2025, 3, 4, 10, 0, tzinfo=UTC)),   # RSS pubDate
    ("2025-03-04T10:00:00Z", datetime(2025, 3, 4, 10, 0, tzinfo=UTC)),            # Atom, Exa
    ("2025-03-04T15:30:00+05:30", datetime(2025, 3, 4, 10, 0, tzinfo=UTC)),
    ("2025-03-04", datetime(2025, 3, 4, tzinfo=UTC)),                             # SEC filed_at
    ("Mar 4, 2025", datetime(2025, 3, 4, tzinfo=UTC)),                            # Serper, Brave
    ("March 4, 2025", datetime(2025, 3, 4, tzinfo=UTC)),
    ("4 Mar 2025", datetime(2025, 3, 4, tzinfo=UTC)),
])
def test_the_date_formats_our_sources_actually_send(raw, expected):
    assert parse_when(raw, now=NOW) == expected


@pytest.mark.parametrize("raw, delta", [
    ("3 days ago", timedelta(days=3)),
    ("5 hours ago", timedelta(hours=5)),
    ("2 weeks ago", timedelta(weeks=2)),
    ("1 day ago", timedelta(days=1)),
])
def test_relative_ages_from_search_providers(raw, delta):
    assert parse_when(raw, now=NOW) == NOW - delta


@pytest.mark.parametrize("raw", [None, "", "soon", "not a date", "2025-13-45", 12])
def test_anything_unreadable_is_no_date_rather_than_a_guess(raw):
    assert parse_when(raw, now=NOW) is None


def test_a_future_date_is_clamped_to_now():
    # A feed with a wrong timezone, or a scheduled post, must not float above every real signal.
    assert parse_when("2027-01-01", now=NOW) == NOW


def test_an_implausibly_old_date_is_no_date():
    # 1970-01-01 is what an unset epoch looks like, not when Acme raised money.
    assert parse_when("1970-01-01", now=NOW) is None


@pytest.mark.parametrize("url, expected", [
    ("https://techcrunch.com/2025/03/04/acme-raises-20m/", datetime(2025, 3, 4, tzinfo=UTC)),
    ("https://example.com/news/2025/03/acme-raises", datetime(2025, 3, 1, tzinfo=UTC)),
    ("https://example.com/press/2025-03-04-acme-raises", datetime(2025, 3, 4, tzinfo=UTC)),
])
def test_a_date_in_the_article_url(url, expected):
    assert date_from_url(url, now=NOW) == expected


@pytest.mark.parametrize("url", [
    "https://acme.com/about",
    "https://acme.com/2025/",                 # a year alone would date everything to 1 January
    "https://acme.com/products/12345678",     # digits that are an id, not a date
    None,
])
def test_a_url_without_a_date_says_nothing(url):
    assert date_from_url(url, now=NOW) is None


# ---- every source that has a date, uses it ----------------------------------------------------


_FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Acme launches Orbit</title><link>https://acme.com/blog/orbit</link>
        <pubDate>Tue, 04 Mar 2025 10:00:00 GMT</pubDate></item>
  <item><title>Acme at the summit</title><link>https://acme.com/blog/summit</link></item>
</channel></rss>"""


async def test_an_rss_item_is_dated_by_its_pubdate():
    from nexus.ingestion.sources import RssSignalSource

    async def fetch(url):
        return _FEED

    src = RssSignalSource(fetch=fetch)
    signals = await src.fetch(Account(name="Acme", domain="acme.com"))
    by_title = {s.title: s for s in signals}

    dated = by_title["Acme launches Orbit"]
    assert dated.occurred_at == datetime(2025, 3, 4, 10, 0, tzinfo=UTC)
    assert dated.dated == "event"
    undated = by_title["Acme at the summit"]
    assert undated.dated == "found", "an item with no date was presented as dated"


class _Browser:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, query, limit=6):
        return list(self._hits)


@pytest.mark.parametrize("hit, when, dated", [
    ({"title": "Acme raises $20M Series B", "url": "https://news.example/acme",
      "published_at": "2025-03-04T10:00:00Z"},
     datetime(2025, 3, 4, 10, 0, tzinfo=UTC), "event"),
    ({"title": "Acme raises $20M Series B",
      "url": "https://techcrunch.com/2025/03/04/acme-raises/"},
     datetime(2025, 3, 4, tzinfo=UTC), "event"),
])
async def test_a_web_news_hit_uses_the_providers_date_then_the_urls(hit, when, dated):
    from nexus.ingestion.sources import WebNewsSource

    signals = await WebNewsSource(_Browser([hit])).fetch(Account(name="Acme", domain="acme.com"))

    assert signals and signals[0].occurred_at == when
    assert signals[0].dated == dated


async def test_a_web_news_hit_with_no_date_is_labelled_found():
    from nexus.ingestion.sources import WebNewsSource

    before = utcnow()
    hit = {"title": "Acme raises $20M Series B", "url": "https://news.example/acme"}
    (signal,) = await WebNewsSource(_Browser([hit])).fetch(Account(name="Acme", domain="acme.com"))

    assert signal.dated == "found"
    assert signal.occurred_at >= before


async def test_a_dork_hit_is_dated_the_same_way():
    from nexus.ingestion.sources import DorkedSearchSource

    class Search:
        name = "fake"
        query_dialect = "plain"
        last_failure = ""

        async def search(self, query, *, limit=5):
            return [{"title": "Acme raises $20M Series B led by Index",
                     "url": "https://techcrunch.com/2025/03/04/acme-raises/",
                     "snippet": "Acme raised a $20M Series B round."}]

        async def search_recent(self, query, *, limit=5, days=90):
            return await self.search(query, limit=limit)

    signals = await DorkedSearchSource(search=Search(), max_queries=8).fetch(
        Account(name="Acme", domain="acme.com")
    )

    assert signals, "the dork path produced nothing to check"
    assert all(s.occurred_at == datetime(2025, 3, 4, tzinfo=UTC) for s in signals)
    assert all(s.dated == "event" for s in signals)


async def test_an_sec_filing_and_a_hacker_news_story_carry_their_own_dates(monkeypatch):
    import json

    from nexus.ingestion import public_apis
    from nexus.ingestion.sources import PublicApiSignalSource

    monkeypatch.setattr(public_apis, "_TICKER_INDEX", None)   # a process-wide cache

    async def fetch(url):
        if "company_tickers" in url:
            return 200, json.dumps({"0": {"cik_str": 1, "ticker": "ACME", "title": "Acme Inc"}})
        if "submissions" in url:
            return 200, json.dumps({"name": "Acme Inc", "filings": {"recent": {
                "form": ["8-K"], "filingDate": [(utcnow() - timedelta(days=12)).date().isoformat()],
                "accessionNumber": ["0001"], "primaryDocument": ["a.htm"]}}})
        if "algolia" in url:
            return 200, json.dumps({"hits": [{
                "title": "Acme launches Orbit", "url": "https://acme.com/orbit", "points": 120,
                "created_at": (utcnow() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "objectID": "1"}]})
        return 404, ""

    signals = await PublicApiSignalSource(fetch=fetch).fetch(Account(name="Acme", domain="acme.com"))
    by_title = {s.title: s for s in signals}

    sec = next(s for t, s in by_title.items() if "SEC" in t)
    hn = next(s for t, s in by_title.items() if "Hacker News" in t)
    assert sec.dated == "event" and (utcnow() - sec.occurred_at).days in (11, 12)
    assert hn.dated == "event" and (utcnow() - hn.occurred_at).days in (2, 3)


def test_observations_of_the_present_are_events_not_found():
    # "Acme has 40 open roles" and "Acme changed its pricing page" are true as of the moment we
    # looked. The collection time IS their date, so labelling them "found" would be wrong.
    import inspect

    from nexus.ingestion import sources

    for cls in (sources.AtsSignalSource, sources.WebsiteWatchSignalSource):
        body = inspect.getsource(cls)
        assert 'dated="event"' in body, f"{cls.__name__} does not mark its observation as an event"


# ---- the date survives the trip to the database and the API -----------------------------------


async def test_ingest_stores_the_date_and_the_label(client):
    from nexus.core.security import decode_access_token
    from nexus.ingestion.service import IngestionService
    from nexus.ingestion.sources import RawSignal
    from tests.conftest import auth, signup, tenant_session

    token = await signup(client, slug="sd1", email="o@sd1.x", company="SD1")
    tid = (decode_access_token(token) or {})["tid"]
    old = datetime(2025, 3, 4, 10, 0, tzinfo=UTC)
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        await IngestionService().ingest(ts, acc, [
            RawSignal(kind="funding", source="rss", title="Acme raises", dedupe_key="k1",
                      occurred_at=old, dated="event"),
            RawSignal(kind="news", source="web_news", title="Acme mentioned", dedupe_key="k2"),
        ])

    rows = (await client.get("/api/signals", headers=auth(token))).json()
    by_title = {r["title"]: r for r in rows}

    assert by_title["Acme raises"]["dated"] == "event"
    assert by_title["Acme raises"]["occurred_at"].startswith("2025-03-04")
    assert by_title["Acme mentioned"]["dated"] == "found"


async def test_the_window_now_filters_on_when_it_happened(client):
    # The reported bug, end to end: an old event found today must NOT be in "last 7 days".
    from nexus.core.security import decode_access_token
    from nexus.ingestion.service import IngestionService
    from nexus.ingestion.sources import RawSignal
    from tests.conftest import auth, signup, tenant_session

    token = await signup(client, slug="sd2", email="o@sd2.x", company="SD2")
    tid = (decode_access_token(token) or {})["tid"]
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        await IngestionService().ingest(ts, acc, [
            RawSignal(kind="funding", source="rss", title="An old round", dedupe_key="old",
                      occurred_at=utcnow() - timedelta(days=400), dated="event"),
            RawSignal(kind="funding", source="rss", title="A new round", dedupe_key="new",
                      occurred_at=utcnow() - timedelta(days=2), dated="event"),
        ])

    week = (await client.get("/api/signals?max_age_days=7", headers=auth(token))).json()

    assert [r["title"] for r in week] == ["A new round"]


async def test_a_legacy_row_with_no_label_reads_as_found(client):
    # Every row stored before the column existed was dated at collection. Saying so is the truth.
    from nexus.core.security import decode_access_token
    from nexus.models.signal import SignalEvent
    from tests.conftest import auth, signup, tenant_session

    token = await signup(client, slug="sd3", email="o@sd3.x", company="SD3")
    tid = (decode_access_token(token) or {})["tid"]
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        ts.add(SignalEvent(tenant_id=tid, account_id=acc.id, kind="news", source="rss",
                           title="Legacy", dedupe_key="legacy", dated=None))

    (row,) = (await client.get("/api/signals", headers=auth(token))).json()
    assert row["dated"] == "found"
