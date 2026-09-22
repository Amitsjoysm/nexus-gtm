"""Training & insights ledger capture: consent, the envelope, emit(), sign-up, seams (D24, §18)."""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, make_tenant, principal_from_token, signup, tenant_session

NEXUS = pathlib.Path(__file__).resolve().parents[1] / "nexus"
SRC = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"


async def _consented_tenant(slug: str = "ledger") -> str:
    from nexus.engagement.ledger import consent

    tid = await make_tenant(slug=slug, name=slug.title())
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
    return tid


async def _outbox(tid: str) -> list:
    from nexus.models.ledger import LedgerOutbox

    async with tenant_session(tid) as ts:
        return await ts.list(LedgerOutbox)


# ---- envelope -----------------------------------------------------------------------------------

def test_the_envelope_is_json_safe_bounded_and_self_correlated():
    import json

    from nexus.engagement.ledger import envelope

    body = envelope.build(
        event_id="01J0000000000000000000000A", event_type="outcome.recorded", tenant_id="t1",
        occurred_at=datetime(2026, 9, 17, 9, 14, 3, tzinfo=timezone.utc),
        refs={"account_id": "a1", "contact_id": None},
        payload={"when": datetime(2026, 9, 1, tzinfo=timezone.utc), "long": "x" * 50_000},
    )
    json.dumps(body)
    assert body["schema_version"] == envelope.SCHEMA_VERSION
    assert body["refs"] == {"account_id": "a1"}
    assert body["chain"]["correlation_id"] == "01J0000000000000000000000A"
    assert body["payload"]["long"].endswith("[truncated]")
    assert len(body["payload"]["long"]) < 21_000
    huge = envelope.build(event_id="e", event_type="ai.call", tenant_id="t", occurred_at=datetime.now(timezone.utc),
                          payload={f"k{i}": "y" * 19_000 for i in range(20)})
    assert huge["payload"] == {"truncated": True, "reason": "payload over the ledger size cap"}


# ---- consent and emit -----------------------------------------------------------------------------

async def test_nothing_is_recorded_before_a_workspace_decides_or_after_it_says_no():
    from nexus.engagement.ledger import consent
    from nexus.engagement.ledger.emit import emit

    tid = await make_tenant(slug="undecided")
    async with tenant_session(tid) as ts:
        assert await consent.status(ts) == "pending"
        assert await emit(ts, "outcome.recorded", payload={"stage": "sent"}) is None
        await consent.record(ts, status_value="off", source="prompt", user_id=None)
        assert await emit(ts, "outcome.recorded", payload={"stage": "sent"}) is None
    assert await _outbox(tid) == []


async def test_an_event_is_written_in_the_actions_transaction_and_vanishes_with_a_rollback():
    from nexus.engagement.ledger.emit import emit
    from nexus.models.outcome import Outcome

    tid = await _consented_tenant("txn")
    async with tenant_session(tid) as ts:
        event_id = await emit(ts, "outcome.recorded", refs={"account_id": "a1"},
                              payload={"stage": "meeting"})
    rows = await _outbox(tid)
    assert [r.event_id for r in rows] == [event_id]
    assert rows[0].payload["event_type"] == "outcome.recorded"
    assert rows[0].payload["payload"] == {"stage": "meeting"}

    with pytest.raises(RuntimeError):
        async with tenant_session(tid) as ts:
            ts.add(Outcome(stage="sent"))
            await emit(ts, "outcome.recorded", payload={"stage": "sent"})
            raise RuntimeError("the action failed")
    assert len(await _outbox(tid)) == 1


async def test_emit_never_breaks_the_caller(monkeypatch):
    from nexus.engagement.ledger.emit import emit
    from nexus.models.outcome import Outcome

    tid = await _consented_tenant("safe")
    async with tenant_session(tid) as ts:
        assert await emit(ts, "not.a.registered.type") is None
        # A real failure inside emit: an occurred_at that is not a datetime.
        assert await emit(ts, "outcome.recorded", occurred_at="yesterday") is None
        ts.add(Outcome(stage="sent"))  # the session is still usable afterwards
        await ts.flush()
    monkeypatch.setattr(get_settings(), "ledger_capture_enabled", False)
    async with tenant_session(tid) as ts:
        assert await emit(ts, "outcome.recorded", payload={"stage": "sent"}) is None
    assert await _outbox(tid) == []


# ---- sign-up -------------------------------------------------------------------------------------

async def _consent_rows(tenant_id: str):
    from nexus.models.ledger import TrainingConsent

    async with tenant_session(tenant_id) as ts:
        return await ts.list(TrainingConsent)


async def test_signup_records_the_choice_on_the_form(client):
    token = await signup(client, slug="optin", email="owner@optinco.com", company="Optin")
    rows = await _consent_rows(principal_from_token(token).tenant_id)
    assert [(r.status, r.source) for r in rows] == [("on", "signup")]

    r = await client.post("/api/auth/signup", json={
        "company_name": "Optout", "company_slug": "optout", "full_name": "Rep",
        "email": "owner@optoutco.com", "password": "password123", "training_consent": False,
    })
    rows = await _consent_rows(r.json()["tenant_id"])
    assert [(r.status, r.source) for r in rows] == [("off", "signup")]


async def test_the_otp_path_carries_the_choice_through_verification():
    from nexus.auth.otp import hash_otp, otp_secret
    from nexus.auth.registration import verify_and_create
    from nexus.core.db import get_sessionmaker, utcnow
    from nexus.models.identity import PendingRegistration

    from datetime import timedelta

    async with get_sessionmaker()() as db:
        db.add(PendingRegistration(
            email="ada@otpco.com", full_name="Ada", company_name="Otp Co", company_slug="otp-co",
            password_hash="x", otp_hash=hash_otp("424242", otp_secret()),
            expires_at=utcnow() + timedelta(minutes=10), training_consent=False,
        ))
        await db.commit()
        _user, tenant = await verify_and_create(db, email="ada@otpco.com", code="424242")
    rows = await _consent_rows(tenant.id)
    assert [(r.status, r.source) for r in rows] == [("off", "signup")]


async def test_a_second_workspace_records_its_own_choice(client):
    token = await signup(client, slug="first", email="owner@firstco.com", company="First")
    r = await client.post("/api/auth/workspaces", json={"name": "Second", "slug": "second-ws",
                                                        "training_consent": False},
                          headers=auth(token))
    assert r.status_code == 201, r.text
    rows = await _consent_rows(r.json()["tenant_id"])
    assert [(row.status, row.source) for row in rows] == [("off", "signup")]


# ---- consent API -----------------------------------------------------------------------------------

async def test_an_existing_workspace_is_asked_once_and_switching_off_deletes_the_outbox(client):
    from nexus.core.security import create_access_token
    from nexus.engagement.ledger.emit import emit
    from nexus.models.ledger import TrainingConsent

    token = await signup(client, slug="legacy", email="owner@legacyco.com", company="Legacy")
    tid = principal_from_token(token).tenant_id
    async with tenant_session(tid) as ts:  # make it look like a pre-ledger workspace
        for row in await ts.list(TrainingConsent):
            await ts.delete(row)

    state = (await client.get("/api/engagement/settings/training", headers=auth(token))).json()
    assert state["status"] == "pending" and state["prompt"] is True

    rep = create_access_token(user_id="rep", tenant_id=tid, role="rep")
    rep_state = (await client.get("/api/engagement/settings/training", headers=auth(rep))).json()
    assert rep_state["prompt"] is False
    assert (await client.put("/api/engagement/settings/training", json={"status": "on"},
                             headers=auth(rep))).status_code == 403

    on = await client.put("/api/engagement/settings/training", json={"status": "on"},
                          headers=auth(token))
    assert on.json()["status"] == "on" and on.json()["source"] == "prompt"
    assert on.json()["prompt"] is False
    async with tenant_session(tid) as ts:
        await emit(ts, "outcome.recorded", payload={"stage": "sent"})
    assert len(await _outbox(tid)) >= 2  # consent.changed, the outcome, and the audit action

    off = await client.put("/api/engagement/settings/training", json={"status": "off"},
                           headers=auth(token))
    assert off.json()["status"] == "off" and off.json()["source"] == "settings"
    assert await _outbox(tid) == []


async def test_managers_set_the_confidence_range_and_reps_only_read_it(client):
    from nexus.core.security import create_access_token

    token = await signup(client, slug="range", email="owner@rangeco.com", company="Range")
    tid = principal_from_token(token).tenant_id
    rep = create_access_token(user_id="rep", tenant_id=tid, role="rep")
    read = (await client.get("/api/engagement/settings", headers=auth(rep))).json()
    assert read["reply_confidence_default"] == 0.8 and read["can_edit"] is False
    assert (await client.put("/api/engagement/settings", json={"ooo_default_days": 5},
                             headers=auth(rep))).status_code == 403
    bad = await client.put("/api/engagement/settings",
                           json={"reply_confidence_min": 0.9, "reply_confidence_default": 0.8},
                           headers=auth(token))
    assert bad.status_code == 422
    good = await client.put("/api/engagement/settings",
                            json={"reply_confidence_min": 0.7, "ooo_default_days": 5},
                            headers=auth(token))
    assert good.status_code == 200
    assert (good.json()["reply_confidence_min"], good.json()["ooo_default_days"]) == (0.7, 5)


# ---- seams ---------------------------------------------------------------------------------------

async def test_existing_actions_record_their_events_for_a_consented_workspace():
    from nexus.core.audit import record_audit
    from nexus.engagement.suppression.service import suppress
    from nexus.models.account import Account
    from nexus.outcomes.service import get_outcome_service

    tid = await _consented_tenant("seams")
    async with tenant_session(tid) as ts:
        account = Account(name="Acme", domain="acme.io")
        ts.add(account)
        await ts.flush()
        await get_outcome_service().record(ts, stage="replied", account=account)
        await record_audit(ts, "test.action", target_type="thing", target_id="t1")
        await suppress(ts, email="jane@acme.io", reason="manual")
    types = {row.event_type for row in await _outbox(tid)}
    assert {"outcome.recorded", "audit.action", "contact.suppressed"} <= types


def test_every_seam_in_the_dependency_map_emits():
    seams = {
        "agents/runtime.py": "ai.call",
        "billing/meter.py": "credits.charged",
        "orchestration/engine.py": "workflow.run_finished",
        "outcomes/service.py": "outcome.recorded",
        "calling/service.py": "call.disposition",
        "ingestion/service.py": "signal.ingested",
        "agents/scoring.py": "account.scored",
        "enrichment/waterfall.py": "enrichment.contact",
        "enrichment/account.py": "enrichment.account",
        "core/audit.py": "audit.action",
        "workers/tasks.py": "error.job_failed",
        "engagement/suppression/service.py": "contact.suppressed",
    }
    for rel, event_type in seams.items():
        source = (NEXUS / rel).read_text(encoding="utf-8")
        assert "from nexus.engagement.ledger.emit import emit" in source, f"{rel} does not emit"
        assert f'"{event_type}"' in source, f"{rel} does not emit {event_type}"


def test_signup_offers_the_choice_and_the_prompt_and_settings_exist():
    login = (SRC / "pages/LoginPage.tsx").read_text(encoding="utf-8")
    assert "useState(true)" in login and "training_consent: trainingConsent" in login
    assert 'href="/data-use"' in login
    app = (SRC / "App.tsx").read_text(encoding="utf-8")
    assert '<Route path="/data-use" element={<DataUsePage />} />' in app
    shell = (SRC / "components/layout/AppShell.tsx").read_text(encoding="utf-8")
    assert "<ConsentPrompt />" in shell
    assert "<TrainingConsentCard />" in (SRC / "pages/SettingsPage.tsx").read_text(encoding="utf-8")
