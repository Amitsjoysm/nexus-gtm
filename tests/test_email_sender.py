"""Per-workspace SMTP sender + settings API. Offline: smtplib is mocked; no real email is sent."""
from __future__ import annotations

import nexus.integrations.email_sender as es
from nexus.integrations.email_sender import is_configured, resolve_smtp, send_email
from tests.conftest import auth, signup


# ----------------------------------------------------------------- sender (pure/mocked)
def test_resolve_smtp_applies_provider_presets():
    g = resolve_smtp({"provider": "gmail", "username": "me@gmail.com", "password": "x"})
    assert g["host"] == "smtp.gmail.com" and g["port"] == 587 and g["use_tls"] is True
    assert g["from_email"] == "me@gmail.com"  # defaults to username
    o = resolve_smtp({"provider": "outlook", "username": "me@outlook.com"})
    assert o["host"] == "smtp-mail.outlook.com"


def test_is_configured_requires_enabled_and_full_creds():
    base = {"provider": "gmail", "username": "u", "password": "p"}
    assert is_configured({**base, "enabled": True}) is True
    assert is_configured({**base, "enabled": False}) is False
    assert is_configured({"provider": "gmail", "username": "u", "enabled": True}) is False
    assert is_configured(None) is False


def test_list_accounts_falls_back_to_legacy_single_account():
    from nexus.integrations.email_sender import list_accounts

    legacy = {"provider": "gmail", "username": "u@x.com", "password": "p", "enabled": True}
    accts = list_accounts(legacy)
    assert len(accts) == 1
    assert accts[0]["default"] is True and accts[0]["from_email"] == "u@x.com"
    assert list_accounts({}) == []
    assert list_accounts(None) == []


def test_list_accounts_prefers_accounts_array_over_legacy():
    from nexus.integrations.email_sender import list_accounts

    settings = {
        "username": "legacy@x.com", "password": "p", "enabled": True,
        "accounts": [
            {"id": "a1", "label": "Sales", "provider": "gmail", "username": "sales@x.com",
             "password": "p1", "enabled": True, "default": True},
            {"id": "a2", "label": "Founder", "provider": "outlook", "username": "ceo@x.com",
             "password": "p2", "enabled": True},
        ],
    }
    accts = list_accounts(settings)
    assert [a["id"] for a in accts] == ["a1", "a2"]  # legacy top-level ignored once accounts exist


def test_resolve_account_picks_by_id_then_default_then_first():
    from nexus.integrations.email_sender import resolve_account

    settings = {"accounts": [
        {"id": "a1", "username": "1@x.com", "password": "p", "enabled": True},
        {"id": "a2", "username": "2@x.com", "password": "p", "enabled": True, "default": True},
    ]}
    assert resolve_account(settings, "a1")["id"] == "a1"           # explicit id
    assert resolve_account(settings)["id"] == "a2"                 # default flag
    assert resolve_account(settings, "missing")["id"] == "a2"     # unknown id -> default
    assert resolve_account({}, "a1") is None


def test_is_configured_true_when_any_account_ready():
    from nexus.integrations.email_sender import is_configured

    settings = {"accounts": [
        {"id": "a1", "username": "1@x.com", "enabled": True},  # no password -> not ready
        {"id": "a2", "provider": "gmail", "username": "2@x.com", "password": "p", "enabled": True},
    ]}
    assert is_configured(settings) is True
    off = {"accounts": [{"id": "a1", "username": "1@x.com", "password": "p", "enabled": False}]}
    assert is_configured(off) is False


class _FakeSMTP:
    captured: dict = {}

    def __init__(self, host, port, timeout=30):
        _FakeSMTP.captured = {"host": host, "port": port}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        _FakeSMTP.captured["tls"] = True

    def login(self, u, p):
        _FakeSMTP.captured["login"] = (u, p)

    def send_message(self, msg):
        _FakeSMTP.captured["msg"] = msg


async def test_send_email_success_via_starttls(monkeypatch):
    monkeypatch.setattr(es.smtplib, "SMTP", _FakeSMTP)
    res = await send_email(
        {"provider": "gmail", "username": "me@gmail.com", "password": "pw", "from_name": "Me"},
        to="x@y.com", subject="Hi", body="Body",
    )
    assert res.ok is True
    c = _FakeSMTP.captured
    assert c["host"] == "smtp.gmail.com" and c["login"] == ("me@gmail.com", "pw")
    assert c["tls"] is True
    assert c["msg"]["To"] == "x@y.com" and c["msg"]["Subject"] == "Hi"
    assert c["msg"]["From"] == "Me <me@gmail.com>"


async def test_send_email_failure_returns_not_ok(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(es.smtplib, "SMTP", _Boom)
    res = await send_email({"provider": "gmail", "username": "u", "password": "p"},
                           to="x@y.com", subject="s", body="b")
    assert res.ok is False and "smtp error" in res.detail


async def test_send_email_unconfigured_returns_not_ok():
    assert (await send_email({}, to="x@y.com", subject="s", body="b")).ok is False


# ----------------------------------------------------------------- settings API
async def test_email_settings_put_get_password_write_only(client):
    token = await signup(client, slug="mailco", email="o@mailco.x", company="Mail Co")
    r = await client.put(
        "/api/workspace/email", headers=auth(token),
        json={"provider": "gmail", "username": "sender@gmail.com", "password": "app-pw",
              "from_name": "Sender", "enabled": True},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["provider"] == "gmail" and out["username"] == "sender@gmail.com"
    assert out["has_password"] is True
    assert "password" not in out  # the secret is never serialized back

    g = await client.get("/api/workspace/email", headers=auth(token))
    assert g.json()["has_password"] is True and "password" not in g.json()

    # PUT again without a password keeps the stored one.
    r2 = await client.put(
        "/api/workspace/email", headers=auth(token),
        json={"provider": "gmail", "username": "sender@gmail.com", "enabled": True},
    )
    assert r2.json()["has_password"] is True


async def test_email_test_requires_config(client):
    token = await signup(client, slug="mailco2", email="o@mailco2.x", company="Mail2")
    r = await client.post("/api/workspace/email/test", headers=auth(token), json={})
    assert r.status_code == 400


async def test_email_accounts_crud_and_default(client):
    token = await signup(client, slug="mbx", email="o@mbx.x", company="MBX")
    h = auth(token)
    r = await client.post("/api/workspace/email/accounts", headers=h, json={
        "label": "Sales", "provider": "gmail", "username": "s@x.com", "password": "pw",
        "enabled": True})
    assert r.status_code == 201, r.text
    a1 = r.json()
    assert a1["default"] is True and a1["has_password"] is True and "password" not in a1

    a2 = (await client.post("/api/workspace/email/accounts", headers=h, json={
        "label": "CEO", "provider": "outlook", "username": "c@x.com", "password": "pw2",
        "enabled": True})).json()
    assert a2["default"] is False  # only the first is default

    lst = (await client.get("/api/workspace/email/accounts", headers=h)).json()
    assert {a["id"] for a in lst} == {a1["id"], a2["id"]}

    d = await client.post(f"/api/workspace/email/accounts/{a2['id']}/default", headers=h)
    assert d.json()["default"] is True
    lst2 = (await client.get("/api/workspace/email/accounts", headers=h)).json()
    assert next(a for a in lst2 if a["id"] == a1["id"])["default"] is False

    # PUT without a password keeps the stored secret.
    u = await client.put(f"/api/workspace/email/accounts/{a1['id']}", headers=h, json={
        "label": "Sales 2", "provider": "gmail", "username": "s@x.com", "enabled": True})
    assert u.json()["label"] == "Sales 2" and u.json()["has_password"] is True

    # Deleting the default promotes another so a workspace always has one.
    dele = await client.delete(f"/api/workspace/email/accounts/{a2['id']}", headers=h)
    assert dele.status_code == 204
    lst3 = (await client.get("/api/workspace/email/accounts", headers=h)).json()
    assert len(lst3) == 1 and lst3[0]["id"] == a1["id"] and lst3[0]["default"] is True


async def test_legacy_email_settings_migrates_into_accounts(client):
    token = await signup(client, slug="lgc", email="o@lgc.x", company="LGC")
    h = auth(token)
    await client.put("/api/workspace/email", headers=h, json={
        "provider": "gmail", "username": "legacy@x.com", "password": "pw", "enabled": True})
    accts = (await client.get("/api/workspace/email/accounts", headers=h)).json()
    assert len(accts) == 1
    assert accts[0]["from_email"] == "legacy@x.com" and accts[0]["default"] is True
    assert accts[0]["has_password"] is True
