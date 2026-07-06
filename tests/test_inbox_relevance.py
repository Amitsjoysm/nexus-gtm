"""Inbox relevance: WebNewsSource must only emit signals that NAME the account (no generic
news-site junk), classify buying events, and weak signals must not create tasks. Plus reopen."""
from __future__ import annotations

from nexus.core.db import utcnow
from nexus.ingestion.sources import WebNewsSource, _account_keys, _classify_news
from nexus.inbox.service import get_inbox_service
from nexus.models.account import Account
from nexus.models.signal import SignalEvent
from tests.conftest import make_tenant, tenant_session


class _FakeBrowser:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, query, *, limit=5):
        return self._hits[:limit]


def test_account_keys_and_classifier():
    acc = Account(tenant_id="t", name="Brex Inc", domain="brex.com")
    keys = _account_keys(acc)
    assert "brex" in keys and "inc" not in keys  # generic suffix dropped
    assert _classify_news("Brex raises $300M Series D")[0] == "funding"
    assert _classify_news("Acme appoints new CFO")[0] == "hiring"
    assert _classify_news("Acme launches new product")[0] == "news"
    assert _classify_news("just some text")[1] == 0.4  # unclassified -> low strength


async def test_web_news_drops_results_that_dont_name_the_account():
    acc = Account(tenant_id="t", name="Brex", domain="brex.com")
    hits = [
        # The exact production junk — a news-site index that never says "Brex".
        {"title": "Banking News and Analysis | Banking Dive", "snippet": "Daily banking news",
         "url": "https://bankingdive.com"},
        # A real article that names the account -> kept + classified as funding.
        {"title": "Brex raises $300M Series D", "snippet": "fintech funding", "url": "https://x/1"},
    ]
    out = await WebNewsSource(_FakeBrowser(hits)).fetch(acc)
    assert len(out) == 1
    assert out[0].kind == "funding" and out[0].strength == 0.85
    assert "Brex" in out[0].title


async def test_web_news_empty_when_nothing_names_the_account():
    acc = Account(tenant_id="t", name="Brex", domain="brex.com")
    hits = [{"title": "Generic industry roundup", "snippet": "no names", "url": "https://x"}]
    assert await WebNewsSource(_FakeBrowser(hits)).fetch(acc) == []


async def test_inbox_reopen_round_trips():
    tid = await make_tenant()
    svc = get_inbox_service()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
        ts.add(acc)
        await ts.flush()
        sig = SignalEvent(tenant_id=tid, account_id=acc.id, kind="funding", source="t",
                          title="Acme raises", strength=0.85, dedupe_key="k", occurred_at=utcnow())
        ts.add(sig)
        await ts.flush()
        task = await svc.create_from_signal(ts, sig, acc, composite_score=70)

        await svc.complete(ts, task.id)
        assert await svc.list_tasks(ts, status="open") == []
        assert len(await svc.list_tasks(ts, status="done")) == 1

        reopened = await svc.reopen(ts, task.id)
        assert reopened.status == "open"
        assert len(await svc.list_tasks(ts, status="open")) == 1


async def test_web_news_dedupes_by_event_bucket_not_url():
    """Two funding stories under different URLs are ONE monthly funding signal; a classified
    event and a weak mention keep separate weekly buckets (the mention can't shadow the event)."""
    from nexus.ingestion.sources import WebNewsSource
    from nexus.models.account import Account

    class FakeBrowser:
        async def search(self, query, limit=6):
            return [
                {"title": "Pluto raises $30M Series B", "snippet": "", "url": "https://a.com/1"},
                {"title": "Pluto raised a new funding round", "snippet": "", "url": "https://b.com/2"},
                {"title": "Pluto launches a new product", "snippet": "", "url": "https://c.com/3"},
                {"title": "Pluto covered in the market roundup", "snippet": "", "url": "https://d.com/4"},
            ]

    acc = Account(tenant_id="t", name="Pluto", domain="pluto.co")
    out = await WebNewsSource(FakeBrowser()).fetch(acc)

    kinds = [s.kind for s in out]
    assert kinds.count("funding") == 1  # both funding URLs collapsed into one event signal
    keys = {s.dedupe_key for s in out}
    assert any(k.startswith("funding:pluto.co:") for k in keys)  # monthly bucket
    assert any(k.startswith("news:pluto.co:") and k.endswith(":evt") for k in keys)
    assert any(k.endswith(":mention") for k in keys)  # weak mention keeps its own bucket
