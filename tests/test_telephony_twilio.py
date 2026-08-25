"""Twilio call provider: construction, wire format, error contract, and the calling flow.

Every request is served by ``httpx.MockTransport`` — the same test seam
``ReacherEmailVerifier`` uses — because there is no Twilio account in this environment. These
tests pin the *wire contract* (URL, auth, form fields, TwiML) and the error behaviour; they do
not and cannot prove that live Twilio accepts the request.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.calling.provider import (
    KNOWN_PROVIDERS,
    CallProviderError,
    InvalidPhoneNumber,
    StubCallProvider,
    TelephonyNotConfigured,
    TelephonyNotImplemented,
    build_call_provider,
    set_call_provider,
)
from nexus.calling.twilio import TwilioCallProvider
from nexus.core.config import get_settings
from nexus.core.security import decode_access_token
from nexus.models.account import Account, Contact
from nexus.models.calling import CallActivity
from tests.conftest import auth, signup, tenant_session

SID = "AC00000000000000000000000000000001"
TOKEN = "test-auth-token"
CALL_SID = "CA00000000000000000000000000000009"
REC_SID = "RE00000000000000000000000000000007"


def _provider(handler, **kw) -> TwilioCallProvider:
    """A real TwilioCallProvider whose HTTP goes to ``handler`` instead of the network."""
    return TwilioCallProvider(
        account_sid=SID, auth_token=TOKEN,
        transport=httpx.MockTransport(handler), **kw,
    )


def _created(_req: httpx.Request) -> httpx.Response:
    return httpx.Response(201, json={"sid": CALL_SID, "status": "queued"})


# --------------------------------------------------------------------------- construction


def test_twilio_is_a_known_provider():
    assert "twilio" in KNOWN_PROVIDERS


@pytest.mark.parametrize("key", ["", "stub", "none", "STUB"])
def test_blank_and_stub_keys_build_the_stub(key):
    assert isinstance(build_call_provider(key), StubCallProvider)


def test_unknown_provider_raises_not_implemented():
    with pytest.raises(TelephonyNotImplemented) as exc:
        build_call_provider("carrier-pigeon")
    assert "carrier-pigeon" in str(exc.value)


def test_twilio_without_credentials_raises_not_configured(monkeypatch):
    """Unkeyed must fail loudly. Falling back to the stub would let a deployment believe it was
    dialling while it was only rendering tel: links."""
    monkeypatch.delenv("NEXUS_TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("NEXUS_TWILIO_AUTH_TOKEN", raising=False)
    with pytest.raises(TelephonyNotConfigured) as exc:
        build_call_provider("twilio")
    msg = str(exc.value)
    assert "not configured" in msg
    assert "NEXUS_TWILIO_ACCOUNT_SID" in msg


def test_twilio_with_credentials_builds_from_env(monkeypatch):
    monkeypatch.setenv("NEXUS_TWILIO_ACCOUNT_SID", SID)
    monkeypatch.setenv("NEXUS_TWILIO_AUTH_TOKEN", TOKEN)
    provider = build_call_provider("twilio")
    assert isinstance(provider, TwilioCallProvider)
    assert provider.name == "twilio"


def test_auth_token_is_never_in_the_repr(monkeypatch):
    """A provider landing in a log line or traceback must not leak the account's auth token."""
    provider = _provider(_created)
    assert TOKEN not in repr(provider)


# --------------------------------------------------------------------------- place_call wire format


async def test_place_call_posts_a_rep_first_bridge():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("authorization", "")
        seen["body"] = dict(httpx.QueryParams(request.content.decode()))
        return _created(request)

    handle = await _provider(handler).place_call(
        to="+1 (555) 123-4567", from_="+15550000000",
        context={"agent_number": "+15559998888"},
    )

    assert seen["method"] == "POST"
    assert seen["url"] == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls.json"
    assert seen["auth"].startswith("Basic ")

    body = seen["body"]
    # The rep is rung first; Twilio bridges to the prospect once they answer.
    assert body["To"] == "+15559998888"
    assert body["From"] == "+15550000000"
    assert "<Dial callerId=\"+15550000000\">" in body["Twiml"]
    assert "<Number>+15551234567</Number>" in body["Twiml"]  # normalised from the messy input

    assert handle.mode == "live"
    assert handle.provider_call_id == CALL_SID
    assert handle.dial_url is None


async def test_recording_is_off_unless_explicitly_enabled():
    """Call recording is a legal decision, not a default."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return _created(request)

    await _provider(handler).place_call(
        to="+15551234567", from_="+15550000000",
        context={"agent_number": "+15559998888"},
    )
    assert "Record" not in seen or seen["Record"] == "false"


async def test_recording_flag_is_sent_when_enabled():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return _created(request)

    await _provider(handler, record_calls=True).place_call(
        to="+15551234567", from_="+15550000000",
        context={"agent_number": "+15559998888"},
    )
    assert seen["Record"] == "true"


async def test_place_call_requires_an_agent_number():
    """The bridge has no first leg without the rep's number — fail before spending a request."""
    with pytest.raises(CallProviderError) as exc:
        await _provider(_created).place_call(to="+15551234567", from_="+15550000000")
    assert "agent_number" in str(exc.value)


@pytest.mark.parametrize("bad", ["5551234567", "not-a-number", "", "+1"])
async def test_non_e164_numbers_are_rejected_before_the_request(bad):
    with pytest.raises(InvalidPhoneNumber):
        await _provider(_created).place_call(
            to=bad, from_="+15550000000", context={"agent_number": "+15559998888"},
        )


# --------------------------------------------------------------------------- error contract


@pytest.mark.parametrize("status", [400, 401, 429, 500])
async def test_failed_dial_raises_rather_than_degrading(status):
    """Unlike the email verifier, a failed dial must never degrade to a manual handle — that
    would report a call as placed when no phone ever rang."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"code": 21211, "message": "boom"})

    with pytest.raises(CallProviderError):
        await _provider(handler).place_call(
            to="+15551234567", from_="+15550000000",
            context={"agent_number": "+15559998888"},
        )


async def test_twilio_error_message_is_surfaced():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"code": 21211, "message": "The 'To' number is not a valid phone number"}
        )

    with pytest.raises(CallProviderError) as exc:
        await _provider(handler).place_call(
            to="+15551234567", from_="+15550000000",
            context={"agent_number": "+15559998888"},
        )
    assert "not a valid phone number" in str(exc.value)
    assert "21211" in str(exc.value)


async def test_transport_failure_raises_call_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    with pytest.raises(CallProviderError):
        await _provider(handler).place_call(
            to="+15551234567", from_="+15550000000",
            context={"agent_number": "+15559998888"},
        )


async def test_auth_token_never_appears_in_the_raised_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 20003, "message": "Authenticate"})

    with pytest.raises(CallProviderError) as exc:
        await _provider(handler).place_call(
            to="+15551234567", from_="+15550000000",
            context={"agent_number": "+15559998888"},
        )
    assert TOKEN not in str(exc.value)


# --------------------------------------------------------------------------- outcome enrichment


async def test_get_call_status_returns_status_and_real_duration():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/Calls/{CALL_SID}.json")
        return httpx.Response(200, json={"sid": CALL_SID, "status": "completed", "duration": "42"})

    assert await _provider(handler).get_call_status(CALL_SID) == {
        "status": "completed", "duration_s": 42,
    }


async def test_get_recording_returns_the_mp3_url():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"recordings": [{"sid": REC_SID, "duration": "42"}]})

    url = await _provider(handler).get_recording(CALL_SID)
    assert url == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Recordings/{REC_SID}.mp3"


async def test_get_recording_is_none_when_there_is_none():
    """A call with no recording yields NULL, never a fabricated URL."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"recordings": []})

    assert await _provider(handler).get_recording(CALL_SID) is None


async def test_get_transcript_walks_recording_to_transcription():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/Transcriptions.json" in request.url.path:
            return httpx.Response(200, json={
                "transcriptions": [{"sid": "TR1", "transcription_text": "Hello there."}]
            })
        return httpx.Response(200, json={"recordings": [{"sid": REC_SID}]})

    assert await _provider(handler).get_transcript(CALL_SID) == "Hello there."


async def test_get_transcript_is_none_when_transcription_not_enabled():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/Transcriptions.json" in request.url.path:
            return httpx.Response(200, json={"transcriptions": []})
        return httpx.Response(200, json={"recordings": [{"sid": REC_SID}]})

    assert await _provider(handler).get_transcript(CALL_SID) is None


@pytest.mark.parametrize("method", ["get_call_status", "get_recording", "get_transcript"])
async def test_enrichment_is_best_effort_and_never_raises(method):
    """Enrichment failure must not break outcome logging — the disposition still matters."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    assert await getattr(_provider(handler), method)(CALL_SID) is None


# --------------------------------------------------------------------------- calling flow (API)


async def _seed(client, slug):
    token = await signup(client, slug=slug, email=f"o@{slug}.x", company=slug.title())
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
        ts.add(acc)
        await ts.flush()
        c = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe",
                    title="VP Sales", phone="+15551234567")
        ts.add(c)
        await ts.flush()
    r = await client.post("/api/calling/tasks", headers=auth(token),
                          json={"account_id": acc.id, "contact_id": c.id})
    return token, tid, c.id, r.json()["id"]


async def test_telephony_status_reports_manual_by_default(client):
    token, *_ = await _seed(client, "telstatus")
    body = (await client.get("/api/calling/telephony", headers=auth(token))).json()
    assert body["provider"] == "stub"
    assert body["mode"] == "manual"
    assert body["configured"] is False


async def test_dial_under_the_stub_returns_a_click_to_dial_url(client):
    """Click-to-dial stays the default workflow — /dial is coherent without any telephony."""
    token, _tid, _cid, task_id = await _seed(client, "dialstub")
    r = await client.post(f"/api/calling/tasks/{task_id}/dial", headers=auth(token), json={})
    assert r.status_code == 200
    assert r.json() == {"mode": "manual", "dial_url": "tel:+15551234567", "provider_call_id": None}


async def test_dial_places_a_live_call_through_the_provider(client, monkeypatch):
    token, _tid, _cid, task_id = await _seed(client, "diallive")
    monkeypatch.setattr(get_settings(), "telephony_from_number", "+15550000000")

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return _created(request)

    set_call_provider(_provider(handler))
    try:
        r = await client.post(f"/api/calling/tasks/{task_id}/dial", headers=auth(token),
                              json={"agent_number": "+15559998888"})
    finally:
        set_call_provider(None)

    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "live"
    assert r.json()["provider_call_id"] == CALL_SID
    assert seen["To"] == "+15559998888"           # rep first
    assert "<Number>+15551234567</Number>" in seen["Twiml"]  # then the prospect


async def test_live_dial_requires_an_agent_number(client, monkeypatch):
    """A correctly configured deployment still needs the rep's phone for the bridge's first leg."""
    token, _tid, _cid, task_id = await _seed(client, "dialnoagent")
    monkeypatch.setattr(get_settings(), "telephony_from_number", "+15550000000")
    set_call_provider(_provider(_created))
    try:
        r = await client.post(f"/api/calling/tasks/{task_id}/dial", headers=auth(token), json={})
    finally:
        set_call_provider(None)
    assert r.status_code == 422


async def test_live_dial_without_a_caller_id_is_a_503(client):
    """A live provider with no NEXUS_TELEPHONY_FROM_NUMBER is a broken deployment, not a bad
    request — and it must be reported before Twilio is ever contacted."""
    token, _tid, _cid, task_id = await _seed(client, "dialnofrom")
    set_call_provider(_provider(_created))
    try:
        r = await client.post(f"/api/calling/tasks/{task_id}/dial", headers=auth(token),
                              json={"agent_number": "+15559998888"})
    finally:
        set_call_provider(None)
    assert r.status_code == 503
    assert "NEXUS_TELEPHONY_FROM_NUMBER" in r.json()["detail"]


async def test_failed_dial_surfaces_as_502(client, monkeypatch):
    token, _tid, _cid, task_id = await _seed(client, "dialfail")
    monkeypatch.setattr(get_settings(), "telephony_from_number", "+15550000000")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 20003, "message": "Authenticate"})

    set_call_provider(_provider(handler))
    try:
        r = await client.post(f"/api/calling/tasks/{task_id}/dial", headers=auth(token),
                              json={"agent_number": "+15559998888"})
    finally:
        set_call_provider(None)
    assert r.status_code == 502
    assert TOKEN not in r.text


async def test_disposition_attaches_recording_transcript_and_real_duration(client):
    token, tid, contact_id, task_id = await _seed(client, "dialdispo")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/Calls/{CALL_SID}.json"):
            return httpx.Response(200, json={"status": "completed", "duration": "137"})
        if "/Transcriptions.json" in path:
            return httpx.Response(200, json={
                "transcriptions": [{"transcription_text": "Great chat."}]
            })
        if "/Recordings.json" in path:
            return httpx.Response(200, json={"recordings": [{"sid": REC_SID}]})
        return _created(request)

    set_call_provider(_provider(handler))
    try:
        r = await client.post(
            f"/api/calling/tasks/{task_id}/disposition", headers=auth(token),
            json={"disposition": "connected", "provider_call_id": CALL_SID},
        )
    finally:
        set_call_provider(None)
    assert r.status_code == 200, r.text

    async with tenant_session(tid) as ts:
        act = (await ts.session.scalars(
            ts.select(CallActivity, CallActivity.contact_id == contact_id)
        )).first()
        assert act.provider_call_id == CALL_SID
        assert act.recording_url.endswith(f"/Recordings/{REC_SID}.mp3")
        assert act.transcript == "Great chat."
        assert act.duration_s == 137  # Twilio's real duration, not a typed guess


async def test_manual_disposition_is_untouched_by_telephony(client):
    """The default workflow must behave exactly as before: no provider calls, no enrichment."""
    token, tid, contact_id, task_id = await _seed(client, "dispomanual")
    r = await client.post(
        f"/api/calling/tasks/{task_id}/disposition", headers=auth(token),
        json={"disposition": "connected", "duration_s": 60, "notes": "good call"},
    )
    assert r.status_code == 200
    async with tenant_session(tid) as ts:
        act = (await ts.session.scalars(
            ts.select(CallActivity, CallActivity.contact_id == contact_id)
        )).first()
        assert act.duration_s == 60
        assert act.provider_call_id is None
        assert act.recording_url is None
