# tests/test_mailbox_connection.py
"""Telling a rep whether their mailbox can actually send, and why not when it cannot.

Reported 2026-09-16: "even after connecting email, users get SMTP not connected". Three different
states produce that sentence in `outreach/send.py`, and the rep could tell them apart from neither
the message nor the screen:

* the mailbox belongs to nobody (added before ownership existed, or by an admin for somebody else),
* it is switched off,
* it has no app password.

Only the first is fixable by a click (claim it), and the old message sent every one of them to
"add a mailbox" — which they had already done. The verify endpoint proves the credentials against
the SMTP server WITHOUT sending anything, so a rep can check before a prospect is involved.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


def _mailbox(**over) -> dict:
    base = {
        "id": "m1", "provider": "gmail", "username": "rep@acme.com", "password": "app-password",
        "from_email": "rep@acme.com", "from_name": "Jane", "enabled": True, "default": True,
        "owner_user_id": "u1",
    }
    base.update(over)
    return base


# ---- the refusal names the actual reason --------------------------------------------------------

def test_an_unowned_mailbox_tells_the_rep_to_claim_it():
    """The workspace HAS a mailbox. "You have not connected a sending mailbox" is false, and it
    sends the rep to add a second one nobody will use."""
    from nexus.outreach.send import MailboxNotConnected, resolve_rep_mailbox

    tenant = type("T", (), {"email_settings": {"accounts": [_mailbox(owner_user_id="")]}})()
    with pytest.raises(MailboxNotConnected) as excinfo:
        resolve_rep_mailbox(tenant, "u1")
    assert "claim" in str(excinfo.value).lower()


def test_a_colleagues_mailbox_says_so_rather_than_offering_it():
    """Sending as somebody else is worse than refusing: the reply goes to them."""
    from nexus.outreach.send import MailboxNotConnected, resolve_rep_mailbox

    tenant = type("T", (), {"email_settings": {"accounts": [_mailbox(owner_user_id="someone-else")]}})()
    with pytest.raises(MailboxNotConnected) as excinfo:
        resolve_rep_mailbox(tenant, "u1")
    message = str(excinfo.value).lower()
    assert "belongs to" in message or "colleague" in message


def test_a_switched_off_mailbox_says_it_is_switched_off():
    from nexus.outreach.send import MailboxNotConnected, resolve_rep_mailbox

    tenant = type("T", (), {"email_settings": {"accounts": [_mailbox(enabled=False)]}})()
    with pytest.raises(MailboxNotConnected) as excinfo:
        resolve_rep_mailbox(tenant, "u1")
    assert "off" in str(excinfo.value).lower()


def test_a_mailbox_with_no_password_still_names_the_password():
    from nexus.outreach.send import MailboxNotConnected, resolve_rep_mailbox

    tenant = type("T", (), {"email_settings": {"accounts": [_mailbox(password="")]}})()
    with pytest.raises(MailboxNotConnected) as excinfo:
        resolve_rep_mailbox(tenant, "u1")
    assert "password" in str(excinfo.value).lower()


def test_a_usable_mailbox_is_returned():
    from nexus.outreach.send import resolve_rep_mailbox

    tenant = type("T", (), {"email_settings": {"accounts": [_mailbox()]}})()
    assert resolve_rep_mailbox(tenant, "u1")["id"] == "m1"


# ---- verifying the credentials ------------------------------------------------------------------

async def test_verify_checks_the_login_without_sending_anything(client, monkeypatch):
    """A test EMAIL proves sending works but puts a message in somebody's inbox and needs a
    recipient. Logging in proves the credential, which is what "is it connected?" asks."""
    from nexus.integrations import email_sender

    calls: list[dict] = []

    def fake_login(cfg):
        calls.append(cfg)
        return True, "Signed in to smtp.gmail.com"

    monkeypatch.setattr(email_sender, "_verify_blocking", fake_login, raising=False)

    token = await signup(client, slug="mb1", email="rep@mb1.com", company="MB One")
    created = await client.post(
        "/api/workspace/email/accounts", headers=auth(token),
        json={"provider": "gmail", "username": "rep@mb1.com", "password": "app-password"},
    )
    mailbox_id = created.json()["id"]

    r = await client.post(
        f"/api/workspace/email/accounts/{mailbox_id}/verify", headers=auth(token)
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert calls, "the verify endpoint did not reach the SMTP login"

    # ...and the result is remembered, so the screen can show it without re-testing.
    listed = (await client.get("/api/workspace/email/accounts", headers=auth(token))).json()
    assert listed[0]["verified_at"]


async def test_a_failed_verify_is_recorded_with_the_servers_own_reason(client, monkeypatch):
    """"Authentication failed" and "connection refused" send an operator to different places."""
    from nexus.integrations import email_sender

    def fake_login(cfg):
        return False, "535 Username and Password not accepted"

    monkeypatch.setattr(email_sender, "_verify_blocking", fake_login, raising=False)

    token = await signup(client, slug="mb2", email="rep@mb2.com", company="MB Two")
    created = await client.post(
        "/api/workspace/email/accounts", headers=auth(token),
        json={"provider": "gmail", "username": "rep@mb2.com", "password": "wrong"},
    )
    mailbox_id = created.json()["id"]

    r = await client.post(
        f"/api/workspace/email/accounts/{mailbox_id}/verify", headers=auth(token)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "535" in body["detail"]

    listed = (await client.get("/api/workspace/email/accounts", headers=auth(token))).json()
    assert listed[0]["verified_at"] is None
    assert "535" in (listed[0]["last_error"] or "")


async def test_verifying_a_mailbox_with_no_password_does_not_call_the_server(client, monkeypatch):
    from nexus.integrations import email_sender

    def explode(cfg):  # pragma: no cover - must not run
        raise AssertionError("no login should be attempted without a password")

    monkeypatch.setattr(email_sender, "_verify_blocking", explode, raising=False)

    token = await signup(client, slug="mb3", email="rep@mb3.com", company="MB Three")
    created = await client.post(
        "/api/workspace/email/accounts", headers=auth(token),
        json={"provider": "gmail", "username": "rep@mb3.com"},
    )
    r = await client.post(
        f"/api/workspace/email/accounts/{created.json()['id']}/verify", headers=auth(token)
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "password" in r.json()["detail"].lower()


async def test_a_real_send_reaches_smtp_with_the_mailboxs_own_credentials(monkeypatch):
    """Reported 2026-09-16: "send test email successful but send to a contact says mailbox not
    configured". Both paths end at `send_email`, and they handed it different shapes.

    `resolve_smtp` reads `provider`, `host`, `username` and `password` off the TOP LEVEL of what it
    is given. The test endpoint passes the mailbox dict, where they are. The send path passed
    `{"accounts": [mailbox]}`, where every one of them is a level down — so the resolved host was
    empty and `send_email` returned "smtp not configured" before touching the network. Nothing
    caught it because every other test of this path replaces `send_email` itself.

    This test therefore stops at the LAST function before the socket, and asserts on the config
    that would have been dialled.
    """
    from nexus.integrations import email_sender
    from nexus.models.account import Account, Contact
    from nexus.models.identity import Tenant
    from nexus.outreach import send as send_mod
    from tests.conftest import make_tenant, tenant_session

    dialled: dict = {}

    def fake_send_blocking(cfg, to, subject, body, html=None):
        dialled.update(cfg)

    monkeypatch.setattr(email_sender, "_send_blocking", fake_send_blocking)

    tid = await make_tenant(slug="mb4", name="MB Four")
    async with tenant_session(tid) as ts:
        tenant = await ts.session.get(Tenant, tid)
        tenant.email_settings = {"accounts": [_mailbox(owner_user_id="u1")]}
        await ts.flush()
        account = Account(tenant_id=tid, name="Marketjoy")
        ts.add(account)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=account.id, full_name="Curtis Bent",
                          email="curtis@marketjoy.com", email_status="valid")
        ts.add(contact)
        await ts.flush()

        outcome = await send_mod.send_to_contact(
            ts, contact=contact, subject="Quick question", body="Hi Curtis,\n\nShort body.",
            user_id="u1",
        )

    assert outcome.ok, outcome.detail
    assert dialled.get("username") == "rep@acme.com"
    assert dialled.get("password") == "app-password"
    assert dialled.get("host"), "no SMTP host was resolved, so nothing would have been dialled"


def test_the_settings_screen_offers_the_test_and_the_claim():
    """There is no frontend test runner here, so this reads the source, like the nav/route guards."""
    import pathlib

    src = pathlib.Path("frontend/src/pages/SettingsPage.tsx").read_text(encoding="utf-8")
    assert "verifyMailbox" in src, "no Test connection action on a mailbox"
    assert "claimMailbox" in src, "an unowned mailbox cannot be claimed from the screen"
