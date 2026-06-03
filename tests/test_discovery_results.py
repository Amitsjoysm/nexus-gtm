# tests/test_discovery_results.py
"""GET /runs/{id}/results: server-side source/min_fit/q/cf_<key> filters + dynamic columns."""
from __future__ import annotations

import pytest
from sqlalchemy.orm.attributes import flag_modified

from nexus.models.chat import CustomFieldDef
from nexus.models.orchestration import OrchestrationRun
from nexus.workers.tasks import tenant_session
from tests.conftest import auth, principal_from_token, signup


_CANDS = [
    {"entity": "account", "id": "a1", "name": "NorthBank", "domain": "northbank.com",
     "industry": "Fintech", "employee_count": 800, "country": "US",
     "fit_score": 91, "fit_reasons": ["industry match"], "source": "own",
     "is_new": False, "custom_fields": {"tier": "A"}},
    {"entity": "account", "id": "a2", "name": "WestPay", "domain": "westpay.io",
     "industry": "Fintech", "employee_count": 120, "country": "US",
     "fit_score": 64, "fit_reasons": [], "source": "own",
     "is_new": False, "custom_fields": {"tier": "B"}},
    {"entity": "account", "id": "a3", "name": "NewBank", "domain": "newbank.com",
     "industry": "Fintech", "employee_count": 300, "country": "US",
     "fit_score": 70, "fit_reasons": [], "source": "discovery",
     "is_new": True, "custom_fields": {}},
]


async def _seed_run(token, client) -> str:
    p = principal_from_token(token)
    async with tenant_session(p.tenant_id) as ts:
        run = OrchestrationRun(
            tenant_id=p.tenant_id, goal="discover companies", status="succeeded",
            blackboard={"discovery": {"target": "companies",
                                      "counts": {"own": 2, "new": 1},
                                      "candidates": _CANDS}},
        )
        ts.session.add(run)
        ts.session.add(CustomFieldDef(
            tenant_id=p.tenant_id, entity="account", key="tier",
            label="Tier", kind="text"))
        flag_modified(run, "blackboard")
        await ts.session.flush()
        return run.id


@pytest.mark.asyncio
async def test_results_unfiltered_returns_all_and_columns(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    run_id = await _seed_run(token, client)
    r = await client.get(f"/api/orchestration/runs/{run_id}/results", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert len(body["candidates"]) == 3
    assert body["target"] == "companies"
    assert any(col["key"] == "tier" for col in body["columns"])


@pytest.mark.asyncio
async def test_results_filters(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    run_id = await _seed_run(token, client)

    r = await client.get(
        f"/api/orchestration/runs/{run_id}/results?source=discovery", headers=auth(token))
    assert [c["id"] for c in r.json()["candidates"]] == ["a3"]

    r = await client.get(
        f"/api/orchestration/runs/{run_id}/results?min_fit=70", headers=auth(token))
    assert sorted(c["id"] for c in r.json()["candidates"]) == ["a1", "a3"]

    r = await client.get(
        f"/api/orchestration/runs/{run_id}/results?q=west", headers=auth(token))
    assert [c["id"] for c in r.json()["candidates"]] == ["a2"]

    r = await client.get(
        f"/api/orchestration/runs/{run_id}/results?cf_tier=A", headers=auth(token))
    assert [c["id"] for c in r.json()["candidates"]] == ["a1"]


@pytest.mark.asyncio
async def test_results_pagination(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    run_id = await _seed_run(token, client)
    r = await client.get(
        f"/api/orchestration/runs/{run_id}/results?limit=2&offset=0", headers=auth(token))
    body = r.json()
    assert body["total"] == 3
    assert len(body["candidates"]) == 2


@pytest.mark.asyncio
async def test_results_missing_run_404(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    r = await client.get("/api/orchestration/runs/does-not-exist/results", headers=auth(token))
    assert r.status_code == 404
