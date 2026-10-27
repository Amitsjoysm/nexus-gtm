"""Signals and accounts lead to each other (SDR feedback, 2026-10-26).

Asked: "a filter in Accounts so we can filter results by signals; from the Signals page we should
be able to open the account page". A signal row carried an account id and no name, so the list
could not say whose signal it was, and the Accounts list knew nothing about signals at all.
"""
from __future__ import annotations

from datetime import timedelta

from tests.conftest import auth, principal_from_token, signup, tenant_session


async def _workspace(client, slug):
    token = await signup(client, slug=slug, email=f"o@{slug}.com", company=slug.title())
    h = auth(token)
    funded = (await client.post("/api/accounts", headers=h,
                                json={"name": "Funded Co", "domain": "funded.io"})).json()
    quiet = (await client.post("/api/accounts", headers=h,
                               json={"name": "Quiet Co", "domain": "quiet.io"})).json()
    from nexus.core.db import utcnow
    from nexus.models.signal import SignalEvent

    tid = principal_from_token(token).tenant_id
    async with tenant_session(tid) as ts:
        now = utcnow()
        ts.add(SignalEvent(account_id=funded["id"], kind="funding", source="web",
                           title="Funded Co raises $20M", strength=0.85, dated="event",
                           occurred_at=now - timedelta(days=10), dedupe_key=f"{slug}-f"))
        ts.add(SignalEvent(account_id=funded["id"], kind="hiring", source="ats",
                           title="Funded Co is hiring 12", strength=0.7, dated="event",
                           occurred_at=now - timedelta(days=200), dedupe_key=f"{slug}-h"))
        # A weak mention is not an event, on the Accounts list as on the Signals page.
        ts.add(SignalEvent(account_id=quiet["id"], kind="news", source="rss",
                           title="Quiet Co blog post", strength=0.4,
                           occurred_at=now - timedelta(days=2), dedupe_key=f"{slug}-n"))
        await ts.flush()
    return h, funded, quiet


async def test_a_signal_names_its_account(client):
    h, funded, _ = await _workspace(client, "s2a1")

    rows = (await client.get("/api/signals", headers=h)).json()

    funding = next(r for r in rows if r["kind"] == "funding")
    assert funding["account_id"] == funded["id"]
    assert funding["account_name"] == "Funded Co"


async def test_each_account_lists_its_recent_events_by_kind(client):
    h, funded, quiet = await _workspace(client, "s2a2")

    rows = {r["name"]: r for r in (await client.get("/api/accounts", headers=h)).json()}

    kinds = {s["kind"]: s for s in rows["Funded Co"]["recent_signals"]}
    assert set(kinds) == {"funding", "hiring"}
    assert kinds["funding"]["days_ago"] == 10 and kinds["hiring"]["days_ago"] == 200
    assert rows["Quiet Co"]["recent_signals"] == []      # 0.4 is a mention, not an event


def _src(path):
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "frontend" / "src" / path).read_text("utf-8")


def test_the_signals_page_links_each_signal_to_its_account():
    page = _src("pages/SignalsPage.tsx")
    assert "to={`/accounts/${sig.account_id}`}" in page and "sig.account_name" in page


def test_the_accounts_list_filters_by_signal_kind_and_window():
    page = _src("pages/AccountsPage.tsx")
    assert 'aria-label="Signal"' in page and 'aria-label="Signal within"' in page
    assert "recent_signals" in page
