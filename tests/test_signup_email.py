"""Signup verification codes that never arrived (reported from staging, 2026-10-13).

Signing up showed "check your email" and no code ever came. What was there before:

* The account mailbox (signup codes, password resets, sign-in codes) came only from the
  NEXUS_SYSTEM_SMTP_* environment, in three copies, one per sender.
* Its server could only be one of three presets (Gmail, Outlook, Microsoft 365). Any other mail
  server resolved to an empty host, and the sender answered "smtp not configured" without
  connecting.
* A Gmail app password pasted as Google shows it, in groups with spaces, did not sign in.
* Registration ignored the sender's answer, so whatever went wrong, the visitor was told a code
  was on its way and the only trace was a WARNING in the server log.

Now there is one mailbox, editable in Superadmin on the screens that already existed
(Configuration > Platform email for the server, Provider keys for the password, whose Test signs in
and sends nothing), any SMTP server works, an app password's spaces are dropped for Gmail, a refusal
says what to fix, and registration answers 503 "could not send" with nothing left pending.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import nexus.auth.registration as reg
from nexus.core.config import get_settings

_BODY = {"company_name": "Mail Co", "company_slug": "mail-co", "full_name": "Ada Mail",
         "email": "ada@mail-co.com", "password": "password123"}


@pytest.fixture
def mailbox(monkeypatch):
    """A configured account mailbox; tests change what they need."""
    s = get_settings()
    for name, value in (("system_smtp_provider", "gmail"), ("system_smtp_host", ""),
                        ("system_smtp_port", 0), ("system_smtp_username", "no-reply@mail-co.com"),
                        ("system_smtp_password", "app-password"), ("system_smtp_from", "")):
        monkeypatch.setattr(s, name, value)
    return s


@pytest.fixture
def smtp_wire(monkeypatch):
    """What the sender would have dialled: the resolved config, stopped before the socket."""
    sent: list[dict] = []

    def fake_send(cfg, to, subject, body, html=None):
        sent.append({**cfg, "to": to, "subject": subject, "body": body})

    monkeypatch.setattr("nexus.integrations.email_sender._send_blocking", fake_send)
    return sent


async def _pending(email: str):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import PendingRegistration

    async with get_sessionmaker()() as s:
        return (await s.scalars(select(PendingRegistration).where(
            PendingRegistration.email == email))).first()


async def test_a_code_that_could_not_be_sent_is_not_reported_as_sent(client, mailbox, monkeypatch):
    monkeypatch.setattr(mailbox, "system_smtp_username", "")      # nothing to send from
    r = await client.post("/api/auth/register/start", json=_BODY)
    assert r.status_code == 503, r.text
    assert "could not send" in r.json()["detail"]
    assert await _pending(_BODY["email"]) is None
    # Nothing was sent, so no cooldown stands in the way of trying again.
    again = await client.post("/api/auth/register/start", json=_BODY)
    assert again.status_code == 503


async def test_a_resend_that_could_not_be_sent_is_not_counted(client, mailbox, monkeypatch):
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import PendingRegistration

    async def sent_ok(to, code):
        return True

    monkeypatch.setattr(reg, "send_otp_email", sent_ok)
    assert (await client.post("/api/auth/register/start", json=_BODY)).status_code == 202
    async with get_sessionmaker()() as s:
        row = await s.get(PendingRegistration, (await _pending(_BODY["email"])).id)
        row.last_sent_at = row.last_sent_at - timedelta(minutes=10)
        await s.commit()

    async def not_sent(to, code):
        return False

    monkeypatch.setattr(reg, "send_otp_email", not_sent)
    r = await client.post("/api/auth/register/resend", json={"email": _BODY["email"]})
    assert r.status_code == 503, r.text
    assert (await _pending(_BODY["email"])).resends == 0


async def test_any_smtp_server_sends_the_code(client, mailbox, smtp_wire, monkeypatch):
    monkeypatch.setattr(mailbox, "system_smtp_provider", "smtp")
    monkeypatch.setattr(mailbox, "system_smtp_host", "smtp.zoho.com")
    monkeypatch.setattr(mailbox, "system_smtp_port", 465)
    r = await client.post("/api/auth/register/start", json=_BODY)
    assert r.status_code == 202, r.text
    wire = smtp_wire[-1]
    assert (wire["host"], wire["port"], wire["use_ssl"]) == ("smtp.zoho.com", 465, True)
    assert wire["to"] == _BODY["email"] and "verification code" in wire["subject"]


async def test_signup_and_a_password_reset_arrive_through_the_account_mailbox(
        client, mailbox, smtp_wire, monkeypatch, no_auth_rate_limit):
    """End to end through the real senders, stopped only at the socket: the code that reaches the
    wire verifies the account, and the reset link that reaches the wire changes its password. The
    other flow tests replace the sender, so they cannot see a message that never left."""
    import re
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setattr(mailbox, "app_base_url", "https://gtm.example.com")
    assert (await client.post("/api/auth/register/start", json=_BODY)).status_code == 202
    code = re.search(r"verification code is: (\d+)", smtp_wire[-1]["body"]).group(1)
    r = await client.post("/api/auth/register/verify", json={"email": _BODY["email"], "code": code})
    assert r.status_code == 201, r.text

    r = await client.post("/api/auth/forgot-password", json={"email": _BODY["email"]})
    assert r.status_code == 202, r.text
    wire = smtp_wire[-1]
    assert wire["to"] == _BODY["email"] and (wire["host"], wire["port"]) == ("smtp.gmail.com", 587)
    link = re.search(r"https://gtm\.example\.com/reset-password\?\S+", wire["body"]).group(0)
    token = parse_qs(urlparse(link).query)["token"][0]
    r = await client.post("/api/auth/reset-password", json={
        "email": _BODY["email"], "token": token, "new_password": "newpassword456"})
    assert r.status_code == 200, r.text
    r = await client.post("/api/auth/login", json={"email": _BODY["email"],
                                                   "password": "newpassword456"})
    assert r.status_code == 200, r.text


async def test_gmail_still_needs_only_a_sign_in_and_password(client, mailbox, smtp_wire):
    r = await client.post("/api/auth/register/start", json=_BODY)
    assert r.status_code == 202, r.text
    assert (smtp_wire[-1]["host"], smtp_wire[-1]["port"]) == ("smtp.gmail.com", 587)


async def test_a_gmail_app_password_pasted_with_spaces_signs_in(mailbox, monkeypatch):
    from nexus.integrations import platform_mail

    monkeypatch.setattr(mailbox, "system_smtp_password", " abcd efgh ijkl mnop ")
    assert (await platform_mail.mailbox("account"))["password"] == "abcdefghijklmnop"
    # Any other server keeps the password exactly: a space can be a real character there.
    monkeypatch.setattr(mailbox, "system_smtp_provider", "smtp")
    monkeypatch.setattr(mailbox, "system_smtp_host", "smtp.zoho.com")
    assert (await platform_mail.mailbox("account"))["password"] == "abcd efgh ijkl mnop"


async def test_the_password_saved_in_provider_keys_beats_the_environment(mailbox):
    from nexus.integrations import platform_mail
    from nexus.providers import resolver
    from nexus.providers.service import add_key

    await add_key("system_smtp", "Account email", "from-the-panel")
    resolver.invalidate("system_smtp")
    raw = await platform_mail.mailbox("account")
    assert raw["password"] == "from-the-panel"
    assert (await platform_mail.password_source("account"))[1] == "panel"


async def test_provider_keys_test_signs_in_and_says_what_to_fix(mailbox, monkeypatch):
    from nexus.providers.testing import probe

    def refused(cfg):
        return False, "534 5.7.9 Application-specific password required."

    monkeypatch.setattr("nexus.integrations.email_sender._verify_blocking", refused)
    result = await probe("system_smtp", "my-google-password")
    assert not result.ok and "App passwords" in result.detail

    def signed_in(cfg):
        assert cfg["password"] == "abcdefghijklmnop"   # the key being tested, spaces dropped
        return True, "Signed in."

    monkeypatch.setattr("nexus.integrations.email_sender._verify_blocking", signed_in)
    assert (await probe("system_smtp", "abcd efgh ijkl mnop")).ok


def test_microsoft_365_smtp_sign_in_switched_off_is_named():
    from nexus.integrations.platform_mail import explain

    text = explain("535 5.7.139 Authentication unsuccessful, SmtpClientAuthentication is "
                   "disabled for the Tenant.")
    assert "Authenticated SMTP" in text


def test_the_settings_are_on_the_existing_configuration_screen():
    from nexus.runtime_config.catalog import CATALOG
    from nexus.runtime_config.service import validate

    for key in ("system_smtp_provider", "system_smtp_host", "system_smtp_port",
                "system_smtp_username", "system_smtp_from", "system_smtp_from_name",
                "alert_smtp_host", "alert_smtp_port", "alert_smtp_username",
                "alert_email_sender", "alert_email_to"):
        assert CATALOG[key].group == "Platform email", key
    # A line break would add a header to every email sent; a private host is a port scanner.
    for key, bad in (("system_smtp_from", "a@b.com\nBcc: x@y.com"),
                     ("system_smtp_from_name", "Evil\r\nBcc: x@y.com"),
                     ("system_smtp_host", "https://smtp.zoho.com"),
                     ("system_smtp_host", "metadata.google.internal")):
        with pytest.raises(ValueError):
            validate(key, bad)
    # The passwords are not settings (those are returned in plaintext): they are provider keys.
    assert "system_smtp_password" not in CATALOG and "alert_smtp_password" not in CATALOG


def test_one_mailbox_feeds_every_account_email():
    """Three copies of the configuration were how a fix to one sender missed the others."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "nexus"
    readers = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.py")
                     if "system_smtp_password" in p.read_text(encoding="utf-8"))
    assert readers == ["core/config.py", "integrations/platform_mail.py",
                       "providers/catalog.py"], readers


async def test_alert_email_uses_the_server_set_in_superadmin(mailbox, smtp_wire, monkeypatch):
    from nexus.alerts.channels import EmailChannel
    from tests.test_notification_channels import _alert

    s = get_settings()
    monkeypatch.setattr(s, "alert_smtp_host", "smtp.alerts.example.com")
    monkeypatch.setattr(s, "alert_smtp_port", 587)
    monkeypatch.setattr(s, "alert_smtp_username", "alerts@example.com")
    monkeypatch.setattr(s, "alert_smtp_password", "pw")
    monkeypatch.setattr(s, "alert_email_sender", "")
    res = await EmailChannel(to="rep@example.com").deliver(_alert(channel="email"))
    assert res.ok and res.detail == "sent"
    assert smtp_wire[-1]["host"] == "smtp.alerts.example.com"
    assert smtp_wire[-1]["from_email"] == "alerts@example.com"
