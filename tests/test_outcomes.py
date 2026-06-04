"""Outcome-feedback loop: capture, deterministic learned weights, engine override, and API.

Everything runs offline: outcomes are plain rows, the learned-weights heuristic is pure arithmetic,
and the relevance engine is dependency-free. We prove the contract end to end — snapshot capture,
the insufficient-signal fallback, the computed lean, the weight precedence, and the REST surface.
"""
from __future__ import annotations

import pytest

from nexus.models.account import Account
from nexus.outcomes.service import (
    DEFAULT_WEIGHTS,
    MIN_SIGNAL,
    OutcomeService,
    get_outcome_service,
)
from nexus.relevance.engine import RelevanceEngine
from nexus.models.relevance import RelevanceProfile
from tests.conftest import auth, make_tenant, signup, tenant_session


# ---- service: capture --------------------------------------------------------------------------

async def test_record_snapshots_account_firmographics():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(
            tenant_id=tid, name="Acme", domain="acme.co", industry="SaaS",
            employee_count=420, country="US", tech_stack=["aws", "snowflake"],
        )
        ts.add(acc)
        await ts.flush()

        out = await get_outcome_service().record(ts, stage="won", account=acc, meta={"play": "x"})

        assert out.stage == "won"
        assert out.account_id == acc.id
        assert out.industry == "SaaS"
        assert out.employee_count == 420
        assert out.country == "US"
        assert out.tech_count == 2
        assert out.meta == {"play": "x"}


async def test_record_rejects_unknown_stage():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        with pytest.raises(ValueError):
            await get_outcome_service().record(ts, stage="bogus")


# ---- service: learned weights ------------------------------------------------------------------

async def test_learned_weights_falls_back_below_min_signal():
    svc = OutcomeService()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        # One reply = impact 1, below MIN_SIGNAL → defaults, flagged not-learned.
        await svc.record(ts, stage="replied")
        lw = await svc.learned_weights(ts)

    assert lw.learned is False
    assert lw.weights == DEFAULT_WEIGHTS
    assert lw.sample_size == 1


async def test_learned_weights_leans_toward_won_account_axes():
    svc = OutcomeService()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        # Two wins (impact 4 each = 8 ≥ MIN_SIGNAL) whose accounts only ever expose industry+size.
        for _ in range(2):
            acc = Account(
                tenant_id=tid, name="W", domain=None, industry="SaaS",
                employee_count=300, country=None, tech_stack=[],
            )
            ts.add(acc)
            await ts.flush()
            await svc.record(ts, stage="won", account=acc)

        lw = await svc.learned_weights(ts)

    assert lw.learned is True
    assert lw.sample_size == 2
    # Industry and size carried all the signal → up-weighted; geo and tech → down-weighted.
    assert lw.weights["industry"] > DEFAULT_WEIGHTS["industry"]
    assert lw.weights["size"] > DEFAULT_WEIGHTS["size"]
    assert lw.weights["geo"] < DEFAULT_WEIGHTS["geo"]
    assert lw.weights["tech"] < DEFAULT_WEIGHTS["tech"]
    assert sum(lw.weights.values()) == pytest.approx(1.0, abs=1e-3)


def test_min_signal_is_meaningful():
    # Guard: a single reply must not be enough to learn (keeps the fallback test honest).
    assert MIN_SIGNAL > 1


# ---- engine: weight precedence -----------------------------------------------------------------

def _profile(**icp) -> RelevanceProfile:
    return RelevanceProfile(tenant_id="t", icp=icp, value_props=[], product_context="")


def test_learned_weights_shift_the_score():
    engine = RelevanceEngine()
    profile = _profile(industries=["fintech"], employee_min=100, employee_max=1000)
    # Matches the size band but not the industry.
    acc = Account(tenant_id="t", name="A", industry="saas", employee_count=500)

    base = engine.score_icp_fit(profile, acc).score
    leaned = engine.score_icp_fit(
        profile, acc, learned_weights={"industry": 0.1, "size": 0.7, "geo": 0.1, "tech": 0.1}
    ).score

    # Emphasizing the (matched) size dimension must raise the score above the default blend.
    assert leaned > base


def test_explicit_icp_weights_outrank_learned():
    engine = RelevanceEngine()
    learned = {"industry": 0.1, "size": 0.7, "geo": 0.1, "tech": 0.1}
    acc = Account(tenant_id="t", name="A", industry="saas", employee_count=500)

    with_icp_weights = _profile(
        industries=["fintech"], employee_min=100, employee_max=1000,
        weights={"industry": 0.5, "size": 0.5},
    )
    # The user's explicit weights must win, so passing learned weights changes nothing.
    a = engine.score_icp_fit(with_icp_weights, acc).score
    b = engine.score_icp_fit(with_icp_weights, acc, learned_weights=learned).score
    assert a == b


# ---- API ---------------------------------------------------------------------------------------

async def test_outcomes_api_record_list_weights_summary(client):
    h = auth(await signup(client))
    acc = (await client.post("/api/accounts", headers=h, json={
        "name": "Acme", "domain": "acme.co", "industry": "SaaS",
        "employee_count": 300, "country": "US"})).json()

    # Record a win against the account.
    r = await client.post("/api/outcomes", headers=h, json={"stage": "won", "account_id": acc["id"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["stage"] == "won"
    assert body["industry"] == "SaaS"
    assert body["employee_count"] == 300

    # List shows it.
    rows = (await client.get("/api/outcomes", headers=h)).json()
    assert any(o["id"] == body["id"] for o in rows)

    # Summary counts it.
    summary = (await client.get("/api/outcomes/summary", headers=h)).json()
    assert summary["total"] == 1
    assert summary["by_stage"]["won"] == 1
    assert summary["positive"] == 1

    # Weights endpoint returns the fallback shape (one win = impact 4 < MIN_SIGNAL).
    weights = (await client.get("/api/outcomes/weights", headers=h)).json()
    assert weights["learned"] is False
    assert set(weights["weights"]) == {"industry", "size", "geo", "tech"}
    assert weights["defaults"]["industry"] == DEFAULT_WEIGHTS["industry"]


async def test_outcomes_api_rejects_bad_stage(client):
    h = auth(await signup(client))
    r = await client.post("/api/outcomes", headers=h, json={"stage": "nope"})
    assert r.status_code == 422


async def test_outcomes_api_unknown_account_404(client):
    h = auth(await signup(client))
    r = await client.post(
        "/api/outcomes", headers=h, json={"stage": "won", "account_id": "missing"}
    )
    assert r.status_code == 404
