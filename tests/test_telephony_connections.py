"""Telephony: a workspace connects its own Twilio, a superadmin connects the platform's, calls cost.

Three reports, 2026-09-23, each true when made:

* **No superadmin screen.** Twilio lived only in NEXUS_TWILIO_* env vars, so every change was an
  env edit and a restart. The platform account is now a Provider key (`ACsid:authtoken`) and the
  caller ID and on/off switch are Runtime settings.
* **Not per workspace.** Every customer's calls went out on our Twilio under one caller ID. A
  workspace can now connect its own account and number (decided: "both, like CRM" — its own when
  connected, the platform's otherwise).
* **Calls were free.** `calling.minutes` was priced at 4 credits a minute and charged by nothing.
  Calls on the PLATFORM account are now checked before dialling and charged their measured minutes
  once. A workspace on its own Twilio pays Twilio directly, so its calls use no credits.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.calling.provider import CallHandle, CallProvider, set_call_provider
from nexus.core.config import get_settings
from nexus.core.security import decode_access_token
from nexus.models.account import Account, Contact
from tests.conftest import auth, signup, tenant_session

SID = "AC" + "1" * 32
OTHER_SID = "AC" + "2" * 32
TOKEN = "f" * 32
CALL_SID = "CA" + "9" * 32
URL = "/api/integrations/telephony/connection"


async def _workspace(client, slug):
    token = await signup(client, slug=slug, email=f"o@{slug}.x", company=slug.upper())
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
        ts.add(acc)
        await ts.flush()
        c = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe", phone="+15551234567")
        ts.add(c)
        await ts.flush()
    r = await client.post("/api/calling/tasks", headers=auth(token),
                          json={"account_id": acc.id, "contact_id": c.id})
    return token, tid, r.json()["id"]


def _connect_body(**kw):
    return {"account_sid": SID, "auth_token": TOKEN, "from_number": "+15550001111", **kw}


# ---- a workspace connects its own Twilio ---------------------------------------------------------


async def test_a_workspace_connects_its_own_twilio_and_the_secret_never_comes_back(client):
    token, *_ = await _workspace(client, "tc1")

    r = await client.put(URL, headers=auth(token), json=_connect_body())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "workspace"
    assert body["from_number"] == "+15550001111"
    assert body["has_credentials"] is True
    assert body["status"] == "unverified", "a saved credential is not connected until tested"
    assert TOKEN not in r.text and SID not in r.text, "the credential left the server"
    assert body["account_hint"].endswith(SID[-4:])


@pytest.mark.parametrize("bad, field", [
    ({"account_sid": "not-a-sid"}, "account SID"),
    ({"from_number": "555-0100"}, "caller ID"),
])
async def test_a_malformed_connection_is_refused_with_the_reason(client, bad, field):
    token, *_ = await _workspace(client, f"tc2{len(field)}")

    r = await client.put(URL, headers=auth(token), json=_connect_body(**bad))

    assert r.status_code == 400
    assert field in r.json()["detail"]


async def test_only_an_admin_connects_telephony(client):
    # Connecting a phone account decides whose number every rep's calls show and who pays for
    # them, so it sits with the other workspace integrations: manage_workspace, admin and up.
    from nexus.core.security import create_access_token
    from nexus.models.identity import Membership, User

    _, tid, _ = await _workspace(client, "tc3")
    async with tenant_session(tid) as ts:
        user = User(email="rep@tc3.x", full_name="Rep", password_hash="x")
        ts.session.add(user)
        await ts.session.flush()
        ts.add(Membership(tenant_id=tid, user_id=user.id, role="rep"))
        rep_id = user.id
    rep = create_access_token(user_id=rep_id, tenant_id=tid, role="rep")

    assert (await client.put(URL, headers=auth(rep), json=_connect_body())).status_code == 403


async def test_disconnecting_falls_back_to_the_platform(client):
    token, *_ = await _workspace(client, "tc4")
    await client.put(URL, headers=auth(token), json=_connect_body())

    assert (await client.delete(URL, headers=auth(token))).status_code == 204
    assert (await client.get(URL, headers=auth(token))).json()["source"] != "workspace"


async def test_testing_the_connection_checks_the_account_and_the_caller_id(client, monkeypatch):
    from nexus.calling.twilio import TwilioCallProvider

    async def fake_check(self, *, caller_id=None):
        assert self.account_sid == SID and caller_id == "+15550001111"
        return True, "Twilio account 'Acme Sales' is active and owns +15550001111."

    monkeypatch.setattr(TwilioCallProvider, "check_account", fake_check)
    token, *_ = await _workspace(client, "tc5")
    await client.put(URL, headers=auth(token), json=_connect_body())

    r = await client.post(f"{URL}/test", headers=auth(token))

    assert r.status_code == 200 and r.json()["ok"] is True
    assert (await client.get(URL, headers=auth(token))).json()["status"] == "connected"


async def test_the_account_check_reads_the_account_and_the_numbers_it_owns():
    from nexus.calling.twilio import TwilioCallProvider

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/Accounts/{SID}.json"):
            return httpx.Response(200, json={"friendly_name": "Acme Sales", "status": "active"})
        if path.endswith("/IncomingPhoneNumbers.json"):
            owned = request.url.params.get("PhoneNumber") == "+15550001111"
            return httpx.Response(200, json={"incoming_phone_numbers": [{}] if owned else []})
        if path.endswith("/OutgoingCallerIds.json"):
            return httpx.Response(200, json={"outgoing_caller_ids": []})
        return httpx.Response(404, json={})

    p = TwilioCallProvider(account_sid=SID, auth_token=TOKEN,
                           transport=httpx.MockTransport(handler))

    ok, detail = await p.check_account(caller_id="+15550001111")
    assert ok and "Acme Sales" in detail

    ok, detail = await p.check_account(caller_id="+15559999999")
    assert not ok and "+15559999999" in detail, "a caller ID the account does not own passed"


# ---- which account a call goes out on --------------------------------------------------------------


async def test_a_workspaces_own_twilio_wins_over_the_platform(client, monkeypatch):
    from nexus.calling.connection import resolve_call_provider

    monkeypatch.setattr(get_settings(), "telephony_provider", "twilio")
    monkeypatch.setattr(get_settings(), "telephony_from_number", "+15557770000")
    _platform_key(monkeypatch, f"{OTHER_SID}:{TOKEN}")
    token, tid, _ = await _workspace(client, "tc6")
    await client.put(URL, headers=auth(token), json=_connect_body())

    async with tenant_session(tid) as ts:
        resolved = await resolve_call_provider(ts)

    assert resolved.source == "workspace"
    assert resolved.provider.account_sid == SID
    assert resolved.from_number == "+15550001111"


async def test_without_one_the_platform_account_is_used(client, monkeypatch):
    from nexus.calling.connection import resolve_call_provider

    monkeypatch.setattr(get_settings(), "telephony_provider", "twilio")
    monkeypatch.setattr(get_settings(), "telephony_from_number", "+15557770000")
    _platform_key(monkeypatch, f"{OTHER_SID}:{TOKEN}")
    _, tid, _ = await _workspace(client, "tc7")

    async with tenant_session(tid) as ts:
        resolved = await resolve_call_provider(ts)

    assert resolved.source == "platform"
    assert resolved.provider.account_sid == OTHER_SID
    assert resolved.from_number == "+15557770000"


async def test_with_neither_it_is_click_to_dial(client):
    from nexus.calling.connection import resolve_call_provider

    _, tid, _ = await _workspace(client, "tc8")
    async with tenant_session(tid) as ts:
        resolved = await resolve_call_provider(ts)

    assert resolved.provider.name == "stub"
    assert resolved.source == "none"


async def test_an_unreadable_workspace_credential_never_falls_back_to_the_platform(client, monkeypatch):
    # Falling back would bill the customer's credits and ring on OUR caller ID after they chose
    # their own — the CRM rule that a person's action never silently lands somewhere else.
    from nexus.calling.connection import resolve_call_provider
    from nexus.calling.provider import TelephonyNotConfigured
    from nexus.models.integration import IntegrationConnection

    monkeypatch.setattr(get_settings(), "telephony_provider", "twilio")
    _platform_key(monkeypatch, f"{OTHER_SID}:{TOKEN}")
    token, tid, _ = await _workspace(client, "tc9")
    await client.put(URL, headers=auth(token), json=_connect_body())
    async with tenant_session(tid) as ts:
        row = await ts.first(IntegrationConnection, IntegrationConnection.kind == "telephony")
        row.secret = {"enc": "garbage"}

    async with tenant_session(tid) as ts:
        with pytest.raises(TelephonyNotConfigured, match="reconnect"):
            await resolve_call_provider(ts)


def _platform_key(monkeypatch, key):
    from nexus.providers import resolver

    async def pool(provider):
        return [key] if provider == "twilio" else []

    monkeypatch.setattr(resolver, "managed_pool", pool)


# ---- calls on the platform account cost credits ---------------------------------------------------


class _FakeTwilio(CallProvider):
    name = "twilio"

    def __init__(self, seconds=125):
        self.placed = 0
        self.seconds = seconds
        self.account_sid = SID

    async def place_call(self, *, to, from_, context=None):
        self.placed += 1
        return CallHandle(mode="live", provider_call_id=CALL_SID)

    async def get_call_status(self, provider_call_id):
        return {"status": "completed", "duration_s": self.seconds}

    async def get_recording(self, provider_call_id):
        return None

    async def get_transcript(self, provider_call_id):
        return None


@pytest.fixture
async def billing(fresh_db):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates

    await sync_catalog()
    await sync_plans()
    await sync_rates()


async def _minutes_charged(tid) -> float:
    from sqlalchemy import func, select

    from nexus.models.billing import BillingUsageEvent

    async with tenant_session(tid) as ts:
        return float((await ts.session.scalar(
            select(func.coalesce(func.sum(BillingUsageEvent.quantity), 0)).where(
                BillingUsageEvent.tenant_id == tid,
                BillingUsageEvent.capability_id == "calling.minutes",
            )
        )) or 0)


async def test_a_platform_call_is_charged_its_measured_minutes_once(client, billing, monkeypatch):
    monkeypatch.setattr(get_settings(), "telephony_from_number", "+15557770000")
    fake = _FakeTwilio(seconds=125)
    set_call_provider(fake)
    try:
        token, tid, task = await _workspace(client, "tm1")
        await client.post(f"/api/calling/tasks/{task}/dial", headers=auth(token),
                          json={"agent_number": "+15559998888"})
        for _ in range(2):   # logging the same call twice must not charge twice
            r = await client.post(f"/api/calling/tasks/{task}/disposition", headers=auth(token),
                                  json={"disposition": "callback", "provider_call_id": CALL_SID})
            assert r.status_code == 200, r.text
    finally:
        set_call_provider(None)

    assert await _minutes_charged(tid) == 3, "125 seconds is three started minutes"


async def test_a_platform_call_is_refused_before_dialling_when_not_affordable(
    client, billing, monkeypatch,
):
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    monkeypatch.setattr(get_settings(), "telephony_from_number", "+15557770000")
    fake = _FakeTwilio()
    set_call_provider(fake)
    try:
        token, tid, task = await _workspace(client, "tm2")   # a new workspace is on Free
        r = await client.post(f"/api/calling/tasks/{task}/dial", headers=auth(token),
                              json={"agent_number": "+15559998888"})
    finally:
        set_call_provider(None)

    assert r.status_code == 402, r.text
    assert fake.placed == 0, "the prospect was rung before the charge was checked"


async def test_a_call_on_the_workspaces_own_twilio_uses_no_credits(client, billing, monkeypatch):
    from nexus.calling import connection

    fake = _FakeTwilio(seconds=300)

    async def own(ts):
        return connection.ResolvedTelephony(provider=fake, from_number="+15550001111",
                                            source="workspace")

    monkeypatch.setattr(connection, "resolve_call_provider", own)
    token, tid, task = await _workspace(client, "tm3")
    await client.post(f"/api/calling/tasks/{task}/dial", headers=auth(token),
                      json={"agent_number": "+15559998888"})
    await client.post(f"/api/calling/tasks/{task}/disposition", headers=auth(token),
                      json={"disposition": "connected", "provider_call_id": CALL_SID})

    assert fake.placed == 1
    assert await _minutes_charged(tid) == 0


# ---- the superadmin's screens ----------------------------------------------------------------------


def test_twilio_is_a_provider_key_and_its_switches_are_runtime_settings():
    from nexus.providers.catalog import PROVIDERS
    from nexus.runtime_config.catalog import CATALOG

    assert "twilio" in PROVIDERS
    assert CATALOG["telephony_provider"].options == ("stub", "twilio")
    assert CATALOG["telephony_from_number"].kind == "str"


@pytest.mark.parametrize("bad", ["555-0100", "15550001111", "+1 (555) abc"])
def test_a_platform_caller_id_must_be_e164(bad):
    from nexus.runtime_config.service import _VALIDATORS

    with pytest.raises(ValueError):
        _VALIDATORS["telephony_from_number"](bad)


async def test_the_platform_key_is_probed_against_the_twilio_account():
    from nexus.providers.testing import probe

    bad = await probe("twilio", "just-a-token")
    assert not bad.ok and "ACCOUNT_SID:AUTH_TOKEN" in bad.detail

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/Accounts/{SID}.json")
        return httpx.Response(200, json={"status": "active", "friendly_name": "Platform"})

    good = await probe("twilio", f"{SID}:{TOKEN}", transport=httpx.MockTransport(handler))
    assert good.ok, good.detail


async def test_the_call_console_is_told_whose_account_it_is_on(client):
    token, *_ = await _workspace(client, "tc10")
    await client.put(URL, headers=auth(token), json=_connect_body())

    body = (await client.get("/api/calling/telephony", headers=auth(token))).json()

    assert body["source"] == "workspace"
    assert body["from_number"] == "+15550001111"
