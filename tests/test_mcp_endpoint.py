# tests/test_mcp_endpoint.py
"""``POST /mcp`` end to end, as a real MCP client talks to it (spec 2026-10-13, task 6)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from nexus.core.db import get_sessionmaker
from nexus.mcp import credentials, protocol
from tests.conftest import auth, principal_from_token, signup

H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@pytest.fixture(autouse=True)
def _clean():
    from nexus.core import ratelimit

    credentials.forget()
    ratelimit.reset_rate_limits()
    yield
    credentials.forget()


def rpc(id_, method, params=None):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


async def _key(token, scopes=("read", "research", "write")) -> str:
    p = principal_from_token(token)
    async with get_sessionmaker()() as s:
        _, secret = await credentials.create_api_key(
            s, user_id=p.user_id, tenant_id=p.tenant_id, name="test", scopes=list(scopes),
            expires_in_days=None)
        await s.commit()
    return secret


def _h(secret, version="2025-06-18", **extra):
    return {**H, "Authorization": f"Bearer {secret}", "MCP-Protocol-Version": version, **extra}


async def _post(client, secret, body, **kw):
    return await client.post("/mcp", headers=_h(secret, **kw), json=body)


# ---- authentication --------------------------------------------------------------------------

async def test_no_bearer_is_a_401_that_points_at_the_oauth_metadata(client):
    r = await client.post("/mcp", headers=H, json=rpc(1, "initialize"))
    assert r.status_code == 401
    challenge = r.headers["www-authenticate"]
    assert challenge.startswith(
        'Bearer resource_metadata="http://test/.well-known/oauth-protected-resource"')
    assert "nexus.read" in challenge


async def test_an_unknown_bearer_is_a_401_naming_invalid_token(client):
    r = await client.post("/mcp", headers={**H, "Authorization": "Bearer nxk_" + "x" * 43},
                          json=rpc(1, "initialize"))
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]


async def test_a_token_in_the_query_string_is_ignored(client):
    token = await signup(client, slug="ep0", email="o@ep0.com")
    secret = await _key(token)
    r = await client.post(f"/mcp?access_token={secret}", headers=H, json=rpc(1, "initialize"))
    assert r.status_code == 401


# ---- the handshake dialect -------------------------------------------------------------------

async def test_initialize_negotiates_and_explains_untrusted_text(client):
    token = await signup(client, slug="ep1", email="o@ep1.com")
    secret = await _key(token)
    r = await _post(client, secret, rpc(1, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}}))
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["serverInfo"]["name"] == "nexus-gtm"
    assert "never instructions" in res["instructions"]
    assert res["capabilities"] == {"tools": {"listChanged": False}}


async def test_a_notification_is_accepted_with_no_body(client):
    token = await signup(client, slug="ep2", email="o@ep2.com")
    secret = await _key(token)
    r = await _post(client, secret, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""


async def test_tools_listed_follow_the_connections_permissions(client):
    token = await signup(client, slug="ep3", email="o@ep3.com")
    everything = await _key(token)
    read_only = await _key(token, scopes=("read",))
    full = (await _post(client, everything, rpc(1, "tools/list"))).json()["result"]["tools"]
    small = (await _post(client, read_only, rpc(1, "tools/list"))).json()["result"]["tools"]
    assert len(full) == 18 and len(small) == 7
    assert all(t["annotations"]["readOnlyHint"] for t in small)
    names = [t["name"] for t in full]
    assert names == sorted(names, key=names.index)  # stable order
    enrich = next(t for t in full if t["name"] == "enrich_contact")
    assert "credits" in enrich["description"]


async def test_ping(client):
    token = await signup(client, slug="ep4", email="o@ep4.com")
    secret = await _key(token)
    assert (await _post(client, secret, rpc(9, "ping"))).json() == {
        "jsonrpc": "2.0", "id": 9, "result": {}}


# ---- the stateless dialect -------------------------------------------------------------------

def _meta(version="2026-07-28"):
    return {"_meta": {protocol.META_VERSION: version}}


async def test_server_discover_names_every_version(client):
    token = await signup(client, slug="ep5", email="o@ep5.com")
    secret = await _key(token)
    r = await _post(client, secret, rpc(1, "server/discover", _meta()), version="2026-07-28",
                    **{"Mcp-Method": "server/discover"})
    res = r.json()["result"]
    assert "2026-07-28" in res["supportedVersions"] and "2025-06-18" in res["supportedVersions"]
    assert res["resultType"] == "complete"
    assert res["_meta"][protocol.META_SERVER_INFO]["name"] == "nexus-gtm"


async def test_stateless_lists_are_cacheable(client):
    token = await signup(client, slug="ep6", email="o@ep6.com")
    secret = await _key(token)
    res = (await _post(client, secret, rpc(1, "tools/list", _meta()),
                       version="2026-07-28")).json()["result"]
    assert res["ttlMs"] > 0 and res["cacheScope"] == "private" and res["resultType"] == "complete"


async def test_an_unsupported_version_is_refused_with_the_list(client):
    token = await signup(client, slug="ep7", email="o@ep7.com")
    secret = await _key(token)
    err = (await _post(client, secret, rpc(1, "tools/list", _meta("2099-01-01")))).json()["error"]
    assert err["code"] == protocol.UNSUPPORTED_VERSION and "2026-07-28" in err["data"]["supported"]


async def test_a_mismatched_method_header_is_refused(client):
    token = await signup(client, slug="ep8", email="o@ep8.com")
    secret = await _key(token)
    err = (await _post(client, secret, rpc(1, "tools/list", _meta()), version="2026-07-28",
                       **{"Mcp-Method": "tools/call"})).json()["error"]
    assert err["code"] == protocol.HEADER_MISMATCH


# ---- calling tools ---------------------------------------------------------------------------

async def test_a_read_only_connection_cannot_write_and_says_how_to_change_that(client):
    token = await signup(client, slug="ep9", email="o@ep9.com")
    secret = await _key(token, scopes=("read",))
    res = (await _post(client, secret, rpc(1, "tools/call", {
        "name": "create_account", "arguments": {"name": "Nope"}}))).json()["result"]
    assert res["isError"] is True and "AI apps" in res["content"][0]["text"]
    accounts = (await client.get("/api/accounts", headers=auth(token))).json()
    assert accounts == []


async def test_a_write_call_creates_the_record_and_is_audited(client):
    from nexus.models.audit import AuditLog

    token = await signup(client, slug="ep10", email="o@ep10.com")
    secret = await _key(token)
    res = (await _post(client, secret, rpc(1, "tools/call", {
        "name": "create_account", "arguments": {"name": "Wayne Enterprises",
                                                "domain": "wayne.com"}}))).json()["result"]
    assert res["isError"] is False, res
    assert res["structuredContent"]["account"]["name"] == "Wayne Enterprises"
    accounts = (await client.get("/api/accounts", headers=auth(token))).json()
    assert [a["name"] for a in accounts] == ["Wayne Enterprises"]
    async with get_sessionmaker()() as s:
        row = (await s.scalars(select(AuditLog).where(AuditLog.action == "mcp.tool"))).first()
    assert row is not None and row.target_id == "create_account"


async def test_bad_arguments_and_unknown_tools_are_invalid_params(client):
    token = await signup(client, slug="ep11", email="o@ep11.com")
    secret = await _key(token)
    bad = (await _post(client, secret, rpc(1, "tools/call", {
        "name": "fetch", "arguments": {"id": "account:../../x"}}))).json()
    assert bad["error"]["code"] == protocol.INVALID_PARAMS
    unknown = (await _post(client, secret, rpc(2, "tools/call", {"name": "send_email"}))).json()
    assert unknown["error"]["code"] == protocol.INVALID_PARAMS


async def test_unknown_methods_batches_and_get(client):
    token = await signup(client, slug="ep12", email="o@ep12.com")
    secret = await _key(token)
    assert (await _post(client, secret, rpc(1, "resources/list"))).json()["error"]["code"] \
        == protocol.METHOD_NOT_FOUND
    batch = await client.post("/mcp", headers=_h(secret), json=[rpc(1, "ping")])
    assert batch.status_code == 400 and batch.json()["error"]["code"] == protocol.INVALID_REQUEST
    get = await client.get("/mcp", headers=_h(secret))
    assert get.status_code == 405 and get.headers["allow"] == "POST"


async def test_a_foreign_browser_origin_is_refused(client):
    token = await signup(client, slug="ep13", email="o@ep13.com")
    secret = await _key(token)
    r = await client.post("/mcp", headers=_h(secret, Origin="https://evil.example"),
                          json=rpc(1, "ping"))
    assert r.status_code == 403
    ok = await client.post("/mcp", headers=_h(secret, Origin="http://test"), json=rpc(1, "ping"))
    assert ok.status_code == 200


async def test_a_switched_off_module_explains_itself(client):
    from nexus.features import switches
    from nexus.models.feature_switch import FeatureSwitch

    token = await signup(client, slug="ep14", email="o@ep14.com")
    secret = await _key(token)
    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id="module.mcp", state="maintenance",
                            message="Back at 14:00 UTC."))
        await s.commit()
    switches.invalidate()
    try:
        err = (await _post(client, secret, rpc(1, "tools/list"))).json()["error"]
        assert err["code"] == protocol.UNAVAILABLE and "14:00" in err["message"]
        # initialize still works, so the app connects and can say why tools are missing.
        assert "result" in (await _post(client, secret, rpc(2, "initialize"))).json()
    finally:
        switches.invalidate()


async def test_the_rate_limit_is_per_connection(client, monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "mcp_rate_limit_per_minute", 2)
    token = await signup(client, slug="ep15", email="o@ep15.com")
    a, b = await _key(token), await _key(token)
    call = rpc(1, "tools/call", {"name": "list_lists"})
    assert (await _post(client, a, call)).status_code == 200
    assert (await _post(client, a, call)).status_code == 200
    limited = await _post(client, a, call)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    assert (await _post(client, b, call)).status_code == 200


async def test_one_workspaces_key_never_sees_anothers_data(client):
    a = await signup(client, slug="ep16a", email="o@ep16a.com")
    b = await signup(client, slug="ep16b", email="o@ep16b.com")
    made = await client.post("/api/accounts", headers=auth(b), json={"name": "Secret Corp",
                                                                     "domain": "secret.com"})
    b_account = made.json()["id"]
    secret = await _key(a)
    found = (await _post(client, secret, rpc(1, "tools/call", {
        "name": "search", "arguments": {"query": "secret"}}))).json()["result"]
    assert found["structuredContent"]["results"] == []
    fetched = (await _post(client, secret, rpc(2, "tools/call", {
        "name": "fetch", "arguments": {"id": f"account:{b_account}"}}))).json()["result"]
    assert fetched["isError"] is True and fetched["structuredContent"]["status"] == 404


async def test_out_of_credits_is_a_readable_tool_error(client, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    token = await signup(client, slug="ep17", email="o@ep17.com")
    p = principal_from_token(token)
    from tests.conftest import put_on_plan

    await put_on_plan(p.tenant_id, "free")
    # Spend the free credits so the balance cannot cover anything.
    from nexus.billing.credits import balance, grant_credits
    from nexus.workers.tasks import tenant_session

    async with tenant_session(p.tenant_id) as ts:
        left = await balance(ts)
        if left > 0:
            await grant_credits(ts, -left, reason="test drain", idempotency_key="drain")
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    acc = (await client.post("/api/accounts", headers=auth(token),
                             json={"name": "Pied Piper", "domain": "piedpiper.com"})).json()["id"]
    secret = await _key(token)
    res = (await _post(client, secret, rpc(1, "tools/call", {
        "name": "find_similar_companies", "arguments": {"account_id": acc}}))).json()["result"]
    assert res["isError"] is True, res
    assert "credit" in res["content"][0]["text"].lower() or "plan" in res["content"][0]["text"].lower()
