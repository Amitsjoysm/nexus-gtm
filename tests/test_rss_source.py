"""RSS/Atom signal source — parser + fetch. Offline (injected feed fetcher)."""
from __future__ import annotations

from nexus.ingestion.sources import RssSignalSource, _parse_feed
from nexus.models.account import Account

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Acme Blog</title>
  <item><title>Acme raises Series B funding</title>
        <link>https://acme.com/b</link><description>We raised.</description></item>
  <item><title>Acme launches new product</title>
        <link>https://acme.com/launch</link><description>Big launch.</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Acme partners with Globex</title>
         <link href="https://acme.com/partner"/><summary>Partnership.</summary></entry>
</feed>"""


def test_parse_rss_items():
    items = _parse_feed(RSS)
    assert [i["title"] for i in items] == [
        "Acme raises Series B funding", "Acme launches new product",
    ]
    assert items[0]["link"] == "https://acme.com/b"


def test_parse_atom_link_href():
    items = _parse_feed(ATOM)
    assert items[0]["title"] == "Acme partners with Globex"
    assert items[0]["link"] == "https://acme.com/partner"


def test_parse_garbage_is_empty():
    assert _parse_feed("not xml at all") == []
    assert _parse_feed("") == []


async def test_source_emits_classified_signals():
    async def fake_fetch(url):
        return RSS if url.endswith("/feed") else None

    sigs = await RssSignalSource(fetch=fake_fetch).fetch(Account(name="Acme", domain="acme.com"))
    assert sigs
    assert "funding" in {s.kind for s in sigs}          # "raises Series B funding" classified
    assert all(s.source == "rss" for s in sigs)
    assert all(s.dedupe_key.startswith("rss:") for s in sigs)
    assert sigs[0].url == "https://acme.com/b"


async def test_source_prefers_custom_field_feed():
    seen: list[str] = []

    async def fake_fetch(url):
        seen.append(url)
        return RSS

    acct = Account(name="Acme", domain="acme.com")
    acct.custom_fields = {"rss_feed": "https://feeds.acme.com/all"}
    await RssSignalSource(fetch=fake_fetch).fetch(acct)
    assert seen == ["https://feeds.acme.com/all"]        # used the explicit feed, not conventions


async def test_source_no_feed_returns_empty():
    async def fake_fetch(url):
        return None

    assert await RssSignalSource(fetch=fake_fetch).fetch(Account(name="Acme", domain="acme.com")) == []
