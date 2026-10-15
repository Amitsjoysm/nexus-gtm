"""A search a person starts is charged, once, and asked about before it is bought (2026-10-15).

Decided with the product owner: charge the web searches and signal scans a person triggers when no
other priced action already covers them, and leave the platform's own scheduled work uncharged.
Two endpoints ran paid searches for a person and charged nothing:

- ``POST /relevance/analyze-website``, priced all along as ``ai.icp_from_website`` and charged only
  by the chat path's ICP save. Charged once per draft that found something; an empty draft is our
  failure and costs nothing.
- ``POST /ingest/{account_id}``, the on-demand signal scan, charged as one ``signal.news_scan``.

Both ask ``preflight`` first, so a capability that is switched off is refused before any search.
"""
from __future__ import annotations

from sqlalchemy import select

from nexus.core.security import decode_access_token
from nexus.models.billing import BillingUsageEvent
from tests.conftest import auth, signup, tenant_session

_DRAFTED = {
    "icp": {"industries": ["Marketing Services"], "buyer_titles": ["VP Sales"]},
    "value_props": [{"name": "Qualified meetings", "description": "", "pains_solved": []}],
    "product_context": "B2B lead generation.",
}
_EMPTY = {"icp": {}, "value_props": [], "product_context": ""}


async def _charges(token: str, capability_id: str) -> list[BillingUsageEvent]:
    claims = decode_access_token(token)
    async with tenant_session(claims["tid"]) as ts:
        return list((await ts.session.scalars(select(BillingUsageEvent).where(
            BillingUsageEvent.tenant_id == ts.tenant_id,
            BillingUsageEvent.capability_id == capability_id,
        ))).all())


async def _switch_off(capability_id: str) -> None:
    from nexus.core.db import get_sessionmaker
    from nexus.features.switches import invalidate
    from nexus.models.feature_switch import FeatureSwitch

    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id=capability_id, state="disabled", message=""))
        await s.commit()
    invalidate()


def _analysis_returns(monkeypatch, draft: dict, calls: list | None = None) -> None:
    from nexus.relevance import website_icp

    async def analyse(url, **kw):
        if calls is not None:
            calls.append(url)
        return draft

    monkeypatch.setattr(website_icp, "analyze_website_to_icp", analyse)


async def test_a_website_analysis_that_found_something_is_charged_once(client, monkeypatch):
    token = await signup(client, slug="pc1", email="o@pc1.com", company="PC1")
    _analysis_returns(monkeypatch, _DRAFTED)

    r = await client.post("/api/relevance/analyze-website", headers=auth(token),
                          json={"url": "https://marketjoy.com"})
    assert r.status_code == 200, r.text

    rows = await _charges(token, "ai.icp_from_website")
    assert len(rows) == 1
    assert float(rows[0].quantity) == 1
    assert rows[0].user_id == decode_access_token(token)["sub"]
    assert rows[0].attrs.get("url") == "https://marketjoy.com"


async def test_an_empty_draft_costs_nothing(client, monkeypatch):
    token = await signup(client, slug="pc2", email="o@pc2.com", company="PC2")
    _analysis_returns(monkeypatch, _EMPTY)

    r = await client.post("/api/relevance/analyze-website", headers=auth(token),
                          json={"url": "https://marketjoy.com"})
    assert r.status_code == 200, r.text
    assert await _charges(token, "ai.icp_from_website") == []


async def test_a_switched_off_analysis_is_refused_before_any_search(client, monkeypatch):
    from nexus.features.switches import invalidate

    token = await signup(client, slug="pc3", email="o@pc3.com", company="PC3")
    calls: list = []
    _analysis_returns(monkeypatch, _DRAFTED, calls)
    await _switch_off("ai.icp_from_website")
    try:
        r = await client.post("/api/relevance/analyze-website", headers=auth(token),
                              json={"url": "https://marketjoy.com"})
    finally:
        invalidate()
    assert r.status_code == 402, r.text
    assert calls == [], "nothing may be searched for a refused analysis"
    assert await _charges(token, "ai.icp_from_website") == []


async def test_a_scan_a_person_asks_for_is_one_news_scan(client):
    token = await signup(client, slug="pc4", email="o@pc4.com", company="PC4")
    h = auth(token)
    acc = (await client.post("/api/accounts", headers=h, json={
        "name": "Globex", "domain": "globex.com", "industry": "Software"})).json()

    r = await client.post(f"/api/ingest/{acc['id']}", headers=h)
    assert r.status_code == 200, r.text

    rows = await _charges(token, "signal.news_scan")
    assert len(rows) == 1
    assert float(rows[0].quantity) == 1
    assert rows[0].user_id == decode_access_token(token)["sub"]
    assert rows[0].attrs.get("account_id") == acc["id"]


async def test_a_switched_off_scan_is_refused_before_any_search(client, monkeypatch):
    from nexus.api.routers import workflow
    from nexus.features.switches import invalidate

    token = await signup(client, slug="pc5", email="o@pc5.com", company="PC5")
    h = auth(token)
    acc = (await client.post("/api/accounts", headers=h, json={
        "name": "Globex", "domain": "globex.com", "industry": "Software"})).json()

    scanned: list = []

    class _Recording:
        async def run_sources(self, ts, account):
            scanned.append(account.id)
            return []

    monkeypatch.setattr(workflow, "get_ingestion_service", lambda: _Recording())
    # The capability itself: the suite seeds no catalog, so a module dependency has no row to
    # resolve through here (the dependency rule has its own tests in test_feature_switches.py).
    await _switch_off("signal.news_scan")
    try:
        r = await client.post(f"/api/ingest/{acc['id']}", headers=h)
    finally:
        invalidate()
    assert r.status_code == 402, r.text
    assert scanned == [], "nothing may be searched for a refused scan"
    assert await _charges(token, "signal.news_scan") == []


async def test_an_unknown_account_is_404_and_costs_nothing(client):
    token = await signup(client, slug="pc6", email="o@pc6.com", company="PC6")
    r = await client.post("/api/ingest/nope", headers=auth(token))
    assert r.status_code == 404
    assert await _charges(token, "signal.news_scan") == []
