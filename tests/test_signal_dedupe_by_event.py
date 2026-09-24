"""News is de-duplicated by the month the event HAPPENED, not the month we found it.

`event_dedupe_key` keeps one funding (or hiring) signal per account per month, so the same round
re-covered by nine outlets alerts once. But the month was the month we FOUND it, so an old article
found this month took this month's slot and a real round found later in the month was dropped —
silently, since a dedupe skip reports nothing.

Decided with the product owner 2026-09-23: a dated event is grouped by its own month. An undated
("found") item keeps today's grouping, because the day we found it is the only date it has.
"""
from __future__ import annotations

from datetime import datetime, timezone

from nexus.core.db import utcnow
from nexus.models.account import Account

UTC = timezone.utc


class _Browser:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, query, limit=6):
        return list(self._hits)


async def test_an_old_round_and_a_new_one_found_together_are_both_kept():
    from nexus.ingestion.sources import WebNewsSource

    this_month = utcnow().strftime("%Y-%m-%dT00:00:00Z")
    hits = [
        {"title": "Acme raises $3M seed round", "url": "https://news.example/a",
         "published_at": "2023-03-10T00:00:00Z"},
        {"title": "Acme raises $20M Series B", "url": "https://news.example/b",
         "published_at": this_month},
    ]

    signals = await WebNewsSource(_Browser(hits)).fetch(Account(name="Acme", domain="acme.com"))

    titles = sorted(s.title for s in signals)
    assert titles == ["Acme raises $20M Series B", "Acme raises $3M seed round"], (
        "the 2023 article took this month's funding slot and hid the real round"
    )


def test_the_same_dated_event_keys_the_same_whenever_it_is_found():
    # Re-found next month, it must still match what is stored, or it would alert again.
    from nexus.ingestion.sources import event_bucket, event_dedupe_key

    when = datetime(2023, 3, 10, tzinfo=UTC)
    october = event_bucket(when, "event", datetime(2026, 10, 1, tzinfo=UTC))
    november = event_bucket(when, "event", datetime(2026, 11, 20, tzinfo=UTC))

    assert event_dedupe_key("funding", "acme.com", 0.85, october) == \
        event_dedupe_key("funding", "acme.com", 0.85, november)


def test_an_undated_item_keeps_the_month_it_was_found():
    from nexus.ingestion.sources import event_bucket

    now = datetime(2026, 10, 5, tzinfo=UTC)
    assert event_bucket(now, "found", now) == now


async def test_a_dork_groups_by_the_event_month_too():
    from nexus.ingestion.sources import DorkedSearchSource

    class Search:
        name = "fake"
        query_dialect = "plain"
        last_failure = ""

        async def search(self, query, *, limit=5):
            return [{"title": "Acme raises $20M Series B led by Index",
                     "url": "https://techcrunch.com/2023/03/10/acme-raises/",
                     "snippet": "Acme raised a $20M Series B round."}]

        async def search_recent(self, query, *, limit=5, days=90):
            return await self.search(query, limit=limit)

    signals = await DorkedSearchSource(search=Search(), max_queries=8).fetch(
        Account(name="Acme", domain="acme.com")
    )

    funding = [s for s in signals if s.kind == "funding"]
    assert funding and all(s.dedupe_key.endswith(":2023-03") for s in funding), [
        s.dedupe_key for s in signals
    ]
