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
