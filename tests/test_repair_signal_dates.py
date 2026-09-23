"""Re-dating the signals stored before sources passed their own dates.

Every one of them was dated at collection. The fix going forward does nothing for them, so the day
filter would stay wrong for up to a quarter while they aged out. This script re-dates what a row can
PROVE — a date in its URL, the filing date in an SEC key, or (opt-in, since it fetches) the post's
own date in the company feed — and leaves everything else labelled "found", which is the truth.

A dry run by default: a maintenance script that writes on its first keystroke is one typo away from
rewriting every workspace's timeline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nexus.core.db import utcnow
from nexus.models.account import Account
from nexus.models.signal import SignalEvent
from scripts.repair_signal_dates import repair
from tests.conftest import make_tenant, tenant_session

UTC = timezone.utc
_FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title>Acme ships v2</title><link>https://acme.com/blog/v2</link>
        <pubDate>Tue, 04 Mar 2025 10:00:00 GMT</pubDate></item>
</channel></rss>"""


async def _seed(ts, tid):
    acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
    ts.add(acc)
    await ts.flush()
    found_at = utcnow() - timedelta(days=2)
    rows = {
        "url": SignalEvent(tenant_id=tid, account_id=acc.id, kind="funding", source="web_news",
                           title="Acme raises", dedupe_key="a",
                           url="https://techcrunch.com/2025/01/15/acme-raises/",
                           occurred_at=found_at, dated=None),
        "sec": SignalEvent(tenant_id=tid, account_id=acc.id, kind="news", source="public_api",
                           title="Acme filed an 8-K", dedupe_key="sec:acme.com:8-K:2025-02-20",
                           occurred_at=found_at, dated=None),
        "rss": SignalEvent(tenant_id=tid, account_id=acc.id, kind="launch", source="rss",
                           title="Acme ships v2", dedupe_key="rss:acme.com:https://acme.com/blog/v2",
                           url="https://acme.com/blog/v2", occurred_at=found_at, dated=None),
        "undatable": SignalEvent(tenant_id=tid, account_id=acc.id, kind="news", source="web_news",
                                 title="Acme mentioned", dedupe_key="b", url="https://news.example/x",
                                 occurred_at=found_at, dated=None),
        "already": SignalEvent(tenant_id=tid, account_id=acc.id, kind="funding", source="rss",
                               title="Dated already", dedupe_key="c",
                               url="https://techcrunch.com/2024/06/01/older/",
                               occurred_at=datetime(2025, 5, 5, tzinfo=UTC), dated="event"),
    }
    for row in rows.values():
        ts.add(row)
    await ts.flush()
    return rows, found_at


def _day(dt):
    return dt.replace(tzinfo=dt.tzinfo or UTC).date()


async def test_a_dry_run_reports_and_writes_nothing():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        rows, found_at = await _seed(ts, tid)

        result = await repair(session=ts.session)

        assert result["from_url"] == 1 and result["from_sec"] == 1
        assert rows["url"].occurred_at == found_at, "a dry run wrote to the database"
        assert rows["url"].dated is None


async def test_apply_re_dates_what_each_row_can_prove():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        rows, found_at = await _seed(ts, tid)

        await repair(apply=True, session=ts.session)

        assert _day(rows["url"].occurred_at).isoformat() == "2025-01-15"
        assert rows["url"].dated == "event"
        assert _day(rows["sec"].occurred_at).isoformat() == "2025-02-20"
        assert rows["sec"].dated == "event"
        # Nothing proves when this happened: it keeps its collection date and says so.
        assert rows["undatable"].occurred_at == found_at
        assert rows["undatable"].dated in (None, "found")
        # A row already dated by its source is never second-guessed by its URL.
        assert _day(rows["already"].occurred_at).isoformat() == "2025-05-05"


async def test_feeds_are_re_read_only_when_asked():
    tid = await make_tenant()
    calls: list[str] = []

    async def fetch(url):
        calls.append(url)
        return _FEED

    async with tenant_session(tid) as ts:
        rows, _ = await _seed(ts, tid)

        await repair(apply=True, session=ts.session, rss_fetch=fetch)
        assert not calls, "the feed was fetched without --feeds"

        result = await repair(apply=True, feeds=True, session=ts.session, rss_fetch=fetch)

        assert result["from_feed"] == 1
        assert rows["rss"].occurred_at.replace(tzinfo=UTC) == datetime(2025, 3, 4, 10, 0, tzinfo=UTC)
        assert rows["rss"].dated == "event"
