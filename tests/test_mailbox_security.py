"""Connected mailboxes cannot be taken, borrowed or aimed at strangers (security fixes, 2026-11-20).

An audit of master 6dae3145 asked "can anyone gain mail access to send bulk email through a mailbox
connected to the app?" and proved five ways in with tests that PASSED while the hole existed. These
are those proofs inverted, plus a guard for each fix:

1. Connecting a mailbox is bound to the browser that pressed Connect. The OAuth ``state`` alone was
   the credential, so an attacker could start "connect Gmail", send Google's consent link to a
   victim, and the victim's mailbox (read all mail, send as them) joined the attacker's workspace.
2. An app-password mailbox is changed only by its owner, its password is sent only to the server it
   was saved for, sealed at rest, and never to a private or metadata address. Another admin could
   point a colleague's mailbox at their own server, press Verify, and read the password.
3. Removing a member disconnects their mailboxes and pauses their campaigns.
4. One contact address is one recipient. ``a@x.com, b@y.com`` was stored, put every address in To,
   and slipped the do-not-contact check, which looked the whole string up.
5. The app-password composer obeys do-not-contact, as the connected-mailbox path always did.
6. A manager may pause and stop a rep's campaign or reply, never send, draft, approve or resume as
   them (owner decision 2026-11-20: "Pause/stop only").
7. A reply that fails its sender checks (DMARC) is shown to a person and acted on by nobody else.

Offline: Google is replaced at the two seams the callback calls, and the "attacker's SMTP server" is
a socket on 127.0.0.1.
"""
from __future__ import annotations

import base64
import socket
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session


async def _configure_google(monkeypatch):
    from nexus.providers import resolver
    from nexus.providers.service import add_key

    monkeypatch.setattr(get_settings(), "engagement_public_base_url", "https://app.example.com")
    monkeypatch.setattr(get_settings(), "engagement_google_client_id",
                        "123456789012-abc123.apps.googleusercontent.com")
    await add_key("google_oauth", "", "google-client-secret-value")
    resolver.invalidate()


def _google_answers(monkeypatch, *, email: str, exchanged: list | None = None):
    """What Google returns once somebody signs in and clicks Allow."""
    from nexus.engagement.config import GOOGLE_SCOPES
    from nexus.engagement.mailboxes import oauth, registry
    from nexus.engagement.mailboxes.provider import MailboxProfile

    async def _exchange(app, *, code, verifier):
        if exchanged is not None:
            exchanged.append((code, verifier))
        return {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600,
                "token_type": "Bearer", "scope": " ".join(GOOGLE_SCOPES)}

    async def _profile():
        return MailboxProfile(email=email, display_name="")

    monkeypatch.setattr(oauth, "exchange_code", _exchange)
    monkeypatch.setattr(registry, "make_provider",
                        lambda provider, access_token: SimpleNamespace(profile=_profile))


async def _start(client, token) -> str:
    started = await client.post("/api/engagement/mailboxes/oauth/google/start",
                                json={"timezone": "UTC"}, headers=auth(token))
    assert started.status_code == 200, started.text
    return parse_qs(urlparse(started.json()["authorize_url"]).query)["state"][0]


# ---- 1. connecting is bound to the browser that started it -------------------------------------

async def test_a_consent_link_opened_in_another_browser_connects_nothing(client, monkeypatch):
    """The attack: the attacker starts, the VICTIM's browser follows Google's redirect."""
    from nexus.models.engagement import MailboxConnection

    await _configure_google(monkeypatch)
    exchanged: list = []
    _google_answers(monkeypatch, email="ceo@victimco.com", exchanged=exchanged)
    attacker_token = await signup(client, slug="attk", email="me@attacker.io", company="Attk")
    attacker = principal_from_token(attacker_token)
    state = await _start(client, attacker_token)

    client.cookies.clear()  # the victim's browser holds nothing of the attacker's
    r = await client.get("/api/engagement/mailboxes/oauth/google/callback",
                         params={"code": "victim-code", "state": state})
    assert r.status_code == 302
    assert r.headers["location"] == "https://app.example.com/settings/mailboxes?error=wrong_browser"
    assert exchanged == [], "the code is never exchanged, so no token for the victim exists"
    async with tenant_session(attacker.tenant_id) as ts:
        assert await ts.list(MailboxConnection) == []


async def test_the_browser_that_pressed_connect_connects_once(client, monkeypatch):
    from nexus.models.engagement import MailboxConnection

    await _configure_google(monkeypatch)
    exchanged: list = []
    _google_answers(monkeypatch, email="sdr@ownco.com", exchanged=exchanged)
    token = await signup(client, slug="ownco", email="sdr@ownco.com", company="Own")
    me = principal_from_token(token)
    state = await _start(client, token)

    r = await client.get("/api/engagement/mailboxes/oauth/google/callback",
                         params={"code": "my-code", "state": state})
    assert r.headers["location"] == "https://app.example.com/settings/mailboxes?connected=google"
    assert len(exchanged) == 1 and exchanged[0][1], "PKCE still runs, with the browser's verifier"
    async with tenant_session(me.tenant_id) as ts:
        rows = await ts.list(MailboxConnection)
    assert [(m.email, m.owner_user_id) for m in rows] == [("sdr@ownco.com", me.user_id)]

    # The binding is spent: the same link again, even in the same browser, connects nothing new.
    again = await client.get("/api/engagement/mailboxes/oauth/google/callback",
                             params={"code": "my-code", "state": state})
    assert again.headers["location"].endswith("error=wrong_browser")
    assert len(exchanged) == 1


async def test_the_state_in_the_consent_link_carries_no_pkce_verifier(client, monkeypatch):
    """The link travels through Google, browser history and anyone it is pasted to. The verifier
    stays in an HttpOnly cookie on our own address."""
    from jose import jwt

    await _configure_google(monkeypatch)
    token = await signup(client, slug="pkce", email="sdr@pkceco.com", company="Pkce")
    state = await _start(client, token)
    claims = jwt.get_unverified_claims(state)
    assert "pkce" not in claims and claims.get("nh")
    cookie = next(c for c in client.cookies.jar if c.name == "nexus_mailbox_google")
    assert cookie.path == "/api/engagement/mailboxes/oauth"
    assert cookie.has_nonstandard_attr("HttpOnly")


# ---- 2. app-password mailboxes -------------------------------------------------------------------

class _FakeSmtp(threading.Thread):
    """A server that accepts any AUTH PLAIN and keeps what it was sent."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]
        self.captured: list[list[bytes]] = []

    def run(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        f = conn.makefile("rwb")

        def say(line: str):
            f.write(line.encode() + b"\r\n")
            f.flush()

        say("220 attacker.example ESMTP")
        while True:
            raw = f.readline()
            if not raw:
                break
            cmd = raw.decode().strip()
            upper = cmd.upper()
            if upper.startswith(("EHLO", "HELO")):
                f.write(b"250-attacker.example\r\n250 AUTH PLAIN LOGIN\r\n")
                f.flush()
            elif upper.startswith("AUTH PLAIN"):
                parts = cmd.split(" ", 2)
                blob = parts[2] if len(parts) == 3 else ""
                if not blob:
                    say("334 ")
                    blob = f.readline().decode().strip()
                self.captured.append(base64.b64decode(blob).split(b"\0"))
                say("235 2.7.0 Authentication successful")
            elif upper.startswith("QUIT"):
                say("221 bye")
                break
            else:
                say("250 ok")
        conn.close()

    def stop(self):
        self.sock.close()


async def _rep_with_app_password(client, slug):
    token = await signup(client, slug=slug, email=f"rep@{slug}.com", company=slug.title())
    me = principal_from_token(token)
    added = await client.post("/api/workspace/email/accounts", headers=auth(token), json={
        "label": "My Gmail", "provider": "gmail", "username": f"rep@{slug}.com",
        "password": "victim-app-password", "from_email": f"rep@{slug}.com"})
    assert added.status_code == 201, added.text
    return token, me, added.json()["id"]


def _admin(tenant_id: str) -> str:
    from nexus.core.security import create_access_token

    return create_access_token(user_id="admin-2", tenant_id=tenant_id, role="admin")


async def test_another_admin_cannot_aim_a_colleagues_mailbox_at_their_own_server(client):
    _token, me, account_id = await _rep_with_app_password(client, "smtpv")
    admin = _admin(me.tenant_id)
    server = _FakeSmtp()
    server.start()
    try:
        edited = await client.put(f"/api/workspace/email/accounts/{account_id}",
                                  headers=auth(admin),
                                  json={"label": "My Gmail", "provider": "smtp",
                                        "host": "127.0.0.1", "port": server.port,
                                        "username": "rep@smtpv.com", "password": None,
                                        "from_email": "rep@smtpv.com", "use_tls": False,
                                        "use_ssl": False})
        assert edited.status_code == 403, edited.text
        assert "owner" in edited.json()["detail"]
        for path in ("verify", "test"):
            r = await client.post(f"/api/workspace/email/accounts/{account_id}/{path}",
                                  headers=auth(admin), json={})
            assert r.status_code == 403, (path, r.text)
    finally:
        server.stop()
        server.join(timeout=5)
    assert server.captured == []


async def test_another_admin_can_still_switch_a_colleagues_mailbox_off_or_remove_it(client):
    _token, me, account_id = await _rep_with_app_password(client, "smtpoff")
    admin = _admin(me.tenant_id)
    listed = (await client.get("/api/workspace/email/accounts", headers=auth(admin))).json()
    row = next(a for a in listed if a["id"] == account_id)
    off = await client.put(f"/api/workspace/email/accounts/{account_id}", headers=auth(admin),
                           json={**{k: row[k] for k in (
                               "label", "provider", "host", "port", "username", "from_email",
                               "from_name", "use_tls", "use_ssl", "imap_host", "imap_port",
                               "drafts_folder", "signature")}, "enabled": False})
    assert off.status_code == 200, off.text
    assert off.json()["enabled"] is False and off.json()["has_password"] is True
    removed = await client.delete(f"/api/workspace/email/accounts/{account_id}",
                                  headers=auth(admin))
    assert removed.status_code == 204


async def test_even_the_owner_retypes_the_password_to_send_it_to_another_server(client):
    token, _me, account_id = await _rep_with_app_password(client, "smtpmove")
    moved = await client.put(f"/api/workspace/email/accounts/{account_id}", headers=auth(token),
                             json={"label": "My Gmail", "provider": "smtp",
                                   "host": "mail.elsewhere.example", "port": 587,
                                   "username": "rep@smtpmove.com", "password": None,
                                   "from_email": "rep@smtpmove.com"})
    assert moved.status_code == 422, moved.text
    assert "password" in moved.json()["detail"].lower()
    # Typed again, it is saved; an edit that keeps the server keeps the stored password.
    typed = await client.put(f"/api/workspace/email/accounts/{account_id}", headers=auth(token),
                             json={"label": "Renamed", "provider": "smtp",
                                   "host": "mail.elsewhere.example", "port": 587,
                                   "username": "rep@smtpmove.com", "password": "new-app-password",
                                   "from_email": "rep@smtpmove.com"})
    assert typed.status_code == 200, typed.text
    renamed = await client.put(f"/api/workspace/email/accounts/{account_id}",
                               headers=auth(token),
                               json={"label": "Renamed again", "provider": "smtp",
                                     "host": "mail.elsewhere.example", "port": 587,
                                     "username": "rep@smtpmove.com", "password": None,
                                     "from_email": "rep@smtpmove.com"})
    assert renamed.status_code == 200 and renamed.json()["has_password"] is True


async def test_app_passwords_are_sealed_at_rest_and_still_used(client):
    from nexus.integrations.email_sender import resolve_smtp
    from nexus.models.identity import Tenant

    _token, me, _account_id = await _rep_with_app_password(client, "sealed")
    async with tenant_session(me.tenant_id) as ts:
        tenant = await ts.session.get(Tenant, me.tenant_id)
        stored = tenant.email_settings["accounts"][0]
    assert "victim-app-password" not in str(tenant.email_settings)
    assert not stored.get("password") and stored.get("password_sealed")
    assert resolve_smtp(stored)["password"] == "victim-app-password"


async def test_a_password_stored_before_sealing_still_works_and_the_sweep_seals_it(client):
    from nexus.integrations.email_sender import resolve_smtp
    from nexus.integrations.mailbox_secrets import seal_workspace_passwords
    from nexus.models.identity import Tenant

    token = await signup(client, slug="legacypw", email="rep@legacypw.com", company="Legacy")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        tenant = await ts.session.get(Tenant, me.tenant_id)
        tenant.email_settings = {"accounts": [{
            "id": "old1", "provider": "gmail", "username": "rep@legacypw.com",
            "password": "plain-old", "enabled": True, "owner_user_id": me.user_id}]}
        await ts.flush()
        assert resolve_smtp(tenant.email_settings["accounts"][0])["password"] == "plain-old"
        assert seal_workspace_passwords(tenant) == 1
        await ts.flush()
    async with tenant_session(me.tenant_id) as ts:
        tenant = await ts.session.get(Tenant, me.tenant_id)
        account = tenant.email_settings["accounts"][0]
        assert "plain-old" not in str(tenant.email_settings)
        assert resolve_smtp(account)["password"] == "plain-old"
        assert seal_workspace_passwords(tenant) == 0, "idempotent"
    listed = (await client.get("/api/workspace/email/accounts", headers=auth(token))).json()
    assert listed[0]["has_password"] is True


_INSIDE = ["127.0.0.1", "10.0.0.5", "169.254.169.254", "metadata.google.internal"]


async def test_the_sweep_dry_runs_then_seals_every_workspace(client):
    from nexus.integrations.mailbox_secrets import seal_all_workspaces
    from nexus.models.identity import Tenant

    token = await signup(client, slug="sweepall", email="rep@sweepall.com", company="Sweep")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        tenant = await ts.session.get(Tenant, me.tenant_id)
        tenant.email_settings = {"password": "legacy-top", "username": "rep@sweepall.com",
                                 "accounts": [{"id": "a1", "password": "in-list"}],
                                 "style": {"tone": "warm"}}
        await ts.flush()
    assert (await seal_all_workspaces(dry_run=True))["passwords"] >= 2
    async with tenant_session(me.tenant_id) as ts:
        assert "in-list" in str((await ts.session.get(Tenant, me.tenant_id)).email_settings)
    assert (await seal_all_workspaces())["passwords"] >= 2
    async with tenant_session(me.tenant_id) as ts:
        settings = (await ts.session.get(Tenant, me.tenant_id)).email_settings
    assert "in-list" not in str(settings) and "legacy-top" not in str(settings)
    assert settings["style"] == {"tone": "warm"}, "only the passwords change"
    assert (await seal_all_workspaces())["passwords"] == 0


@pytest.mark.parametrize("host", _INSIDE)
async def test_a_mail_server_on_a_private_or_metadata_address_is_refused(client, monkeypatch,
                                                                         host):
    slug = f"ssrf{_INSIDE.index(host)}"
    token = await signup(client, slug=slug, email=f"rep@{slug}.com", company="Ssrf")
    monkeypatch.setattr(get_settings(), "env", "prod")
    r = await client.post("/api/workspace/email/accounts", headers=auth(token), json={
        "label": "x", "provider": "smtp", "host": host, "port": 25, "username": "u",
        "password": "p", "from_email": "rep@example.com"})
    assert r.status_code == 422, r.text
    r = await client.post("/api/workspace/email/accounts", headers=auth(token), json={
        "label": "x", "provider": "smtp", "host": "smtp.example.com", "port": 587,
        "username": "u", "password": "p", "from_email": "rep@example.com", "imap_host": host})
    assert r.status_code == 422, r.text


async def test_the_sender_refuses_a_private_host_at_connect_time(monkeypatch):
    """A name that resolved somewhere public when saved can point inside later."""
    from nexus.integrations import email_sender

    monkeypatch.setattr(get_settings(), "env", "prod")
    account = {"provider": "smtp", "host": "127.0.0.1", "port": 2525, "username": "u",
               "password": "p", "from_email": "rep@example.com", "use_tls": False}
    ok, detail = await email_sender.verify_smtp(account, guard_host=True)
    assert ok is False and "refusing" in detail
    sent = await email_sender.send_email(account, to="buyer@example.com", subject="s", body="b",
                                         guard_host=True)
    assert sent.ok is False and "refusing" in sent.detail


async def test_a_test_email_goes_only_to_the_caller_or_the_mailbox_itself(client, monkeypatch):
    calls: list[str] = []

    async def _fake(settings, *, to, subject, body, html=None, guard_host=False):
        calls.append(to)
        return SimpleNamespace(ok=True, detail="sent")

    monkeypatch.setattr("nexus.api.routers.workspace._send_email", _fake)
    token, _me, account_id = await _rep_with_app_password(client, "testto")
    stranger = await client.post(f"/api/workspace/email/accounts/{account_id}/test",
                                 headers=auth(token), json={"to": "anyone@elsewhere.com"})
    assert stranger.status_code == 422, stranger.text
    mine = await client.post(f"/api/workspace/email/accounts/{account_id}/test",
                             headers=auth(token), json={})
    assert mine.status_code == 200, mine.text
    assert calls == ["rep@testto.com"]


async def test_the_graph_handshake_echoes_only_a_bounded_token(client):
    ok = await client.post("/api/engagement/webhooks/graph",
                           params={"validationToken": "Validation: reachability 1234"})
    assert ok.status_code == 200 and ok.text == "Validation: reachability 1234"
    assert ok.headers["x-content-type-options"] == "nosniff"
    assert ok.headers["content-type"].startswith("text/plain")
    long = await client.post("/api/engagement/webhooks/graph",
                             params={"validationToken": "x" * 5000})
    assert long.status_code == 400 and long.text == ""


# ---- 3. a member who leaves takes their mailboxes with them --------------------------------------

async def test_removing_a_member_disconnects_their_mailboxes_and_pauses_their_campaigns(client):
    from nexus.engagement.mailboxes.service import upsert_connection
    from nexus.engagement.sequences.service import create_campaign
    from nexus.models.engagement import EngagementCampaign, MailboxConnection
    from nexus.models.identity import Tenant

    owner_token = await signup(client, slug="leave", email="boss@leaveco.com", company="Leave")
    owner = principal_from_token(owner_token)
    invited = await client.post("/api/workspace/members", headers=auth(owner_token), json={
        "email": "leaver@leaveco.com", "full_name": "Leaver", "password": "password123",
        "role": "rep"})
    assert invited.status_code == 201, invited.text
    leaver_id = invited.json()["user_id"]
    async with tenant_session(owner.tenant_id) as ts:
        box = await upsert_connection(ts, owner_user_id=leaver_id, provider="microsoft",
                                      email="leaver@leaveco.com", display_name="Leaver",
                                      bundle={"access_token": "a", "refresh_token": "r",
                                              "expires_at": 1_900_000_000, "refreshed_at": 1},
                                      scopes=[], timezone="UTC")
        campaign = await create_campaign(ts, name="Theirs", owner_user_id=leaver_id,
                                         mailbox_id=box.id,
                                         steps=[{"channel": "email", "angle": "Hello"}])
        campaign.status = "active"
        tenant = await ts.session.get(Tenant, owner.tenant_id)
        tenant.email_settings = {"accounts": [
            {"id": "smtp1", "provider": "gmail", "username": "leaver@leaveco.com",
             "password": "their-pw", "enabled": True, "owner_user_id": leaver_id},
            {"id": "smtp2", "provider": "gmail", "username": "boss@leaveco.com",
             "password": "boss-pw", "enabled": True, "owner_user_id": owner.user_id}]}
        await ts.flush()

    removed = await client.delete(f"/api/workspace/members/{invited.json()['membership_id']}",
                                  headers=auth(owner_token))
    assert removed.status_code == 204, removed.text
    async with tenant_session(owner.tenant_id) as ts:
        box = (await ts.list(MailboxConnection))[0]
        assert (box.status, box.tokens) == ("revoked", {})
        assert (await ts.list(EngagementCampaign))[0].status == "paused"
        tenant = await ts.session.get(Tenant, owner.tenant_id)
        accounts = {a["id"]: a for a in tenant.email_settings["accounts"]}
    assert accounts["smtp1"]["enabled"] is False
    assert not accounts["smtp1"].get("password") and not accounts["smtp1"].get("password_sealed")
    assert accounts["smtp2"]["enabled"] is True, "only the leaver's mailboxes"


# ---- 4. one address is one recipient --------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("jane@acme.io", "jane@acme.io"),
    ("  Jane@Acme.io ", "Jane@Acme.io"),
    ("Jane Buyer <jane@acme.io>", "jane@acme.io"),
    ("a@x.com, b@y.com", None),
    ("a@x.com;b@y.com", None),
    ("a@x.com b@y.com", None),
    ("a@x.com\nBcc: b@y.com", None),
    ("<a@x.com>, <b@y.com>", None),
    ("not an address", None),
    ("a@b@c.com", None),
    ("a@localhost", None),
    ("", None),
])
def test_single_address(raw, expected):
    from nexus.contacts.addresses import single_address

    assert single_address(raw) == expected


async def test_a_contact_address_holding_several_recipients_is_refused(client):
    token = await signup(client, slug="multi", email="sdr@multico.com", company="Multi")
    account = await client.post("/api/accounts", headers=auth(token),
                                json={"name": "Target", "domain": "target.com"})
    assert account.status_code in (200, 201), account.text
    many = "blocked@target.com, a@other.org, b@other.org"
    r = await client.post(f"/api/accounts/{account.json()['id']}/contacts", headers=auth(token),
                          json={"full_name": "Many", "email": many})
    assert r.status_code == 422, r.text
    assert "one email address" in r.json()["detail"]
    r = await client.post("/api/accounts/contacts", headers=auth(token),
                          json={"contact": {"full_name": "Many", "email": many},
                                "account_id": account.json()["id"]})
    assert r.status_code == 422, r.text
    assert "one email address" in r.json()["detail"]
    # One address, written the way some exports write it, is kept as the bare address.
    r = await client.post(f"/api/accounts/{account.json()['id']}/contacts", headers=auth(token),
                          json={"full_name": "Jane", "email": "Jane Buyer <jane@target.com>"})
    assert r.status_code == 201 and r.json()["email"] == "jane@target.com"


async def test_a_stored_multi_recipient_address_is_never_sent(folder, monkeypatch):
    """Rows written before the check, or by a path that does not validate, stop at the send."""
    from nexus.engagement.sending.service import send
    from nexus.models.account import Contact
    from nexus.models.engagement import MailboxConnection
    from tests.test_engagement_sequences import _world

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid, _user_id, mailbox_id, contact_ids = await _world("multisend", contacts=1)
    async with tenant_session(tid) as ts:
        contact = await ts.get(Contact, contact_ids[0])
        contact.email = "jane0@acme.io, a@other.org"
        mailbox = await ts.get(MailboxConnection, mailbox_id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="hi", body="Hello",
                            kind="response")
    assert result.outcome == "stopped" and "address" in result.reason
    assert folder.delivered == []


async def test_a_block_on_the_address_holds_however_the_field_is_written(folder, monkeypatch):
    from nexus.engagement.sending.service import send
    from nexus.engagement.suppression.service import suppress
    from nexus.models.account import Contact
    from nexus.models.engagement import MailboxConnection
    from tests.test_engagement_sequences import _world

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid, _user_id, mailbox_id, contact_ids = await _world("dncname", contacts=1)
    async with tenant_session(tid) as ts:
        await suppress(ts, email="jane0@acme.io", reason="unsubscribed")
        contact = await ts.get(Contact, contact_ids[0])
        contact.email = "Jane Zero <JANE0@acme.io>"
        mailbox = await ts.get(MailboxConnection, mailbox_id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="hi", body="Hello",
                            kind="response")
    assert result.outcome == "stopped"
    assert folder.delivered == []


async def test_the_smtp_sender_refuses_several_recipients():
    from nexus.integrations.email_sender import send_email

    account = {"provider": "gmail", "username": "u@example.com", "password": "p"}
    result = await send_email(account, to="a@x.com, b@y.com", subject="s", body="b")
    assert result.ok is False and "one address" in result.detail


# ---- 5. the app-password composer obeys do-not-contact ------------------------------------------

async def test_an_app_password_mailbox_does_not_send_to_an_unsubscribed_address(client,
                                                                                monkeypatch):
    from nexus.engagement.suppression.service import suppress
    from nexus.models.identity import Tenant
    from nexus.outreach.send import SendRefused, send_to_contact

    token = await signup(client, slug="dnc", email="sdr@dncco.com", company="Dnc")
    me = principal_from_token(token)
    sent: list[str] = []

    async def _fake_send(settings, *, to, subject, body, html=None, guard_host=False):
        sent.append(to)
        return SimpleNamespace(ok=True, detail="sent")

    monkeypatch.setattr("nexus.integrations.email_sender.send_email", _fake_send)
    async with tenant_session(me.tenant_id) as ts:
        tenant = await ts.session.get(Tenant, me.tenant_id)
        tenant.email_settings = {"accounts": [{
            "id": "mb1", "provider": "gmail", "username": "sdr@dncco.com",
            "password": "app-pw", "from_email": "sdr@dncco.com", "enabled": True,
            "owner_user_id": me.user_id, "default": True}]}
        await ts.flush()
        await suppress(ts, email="optout@buyer.com", reason="unsubscribed")
        contact = SimpleNamespace(id="c1", email="optout@buyer.com", email_status="valid",
                                  account_id=None)
        # Refused exactly as the connected-mailbox path refuses it.
        with pytest.raises(SendRefused, match="do-not-contact"):
            await send_to_contact(ts, contact=contact, subject="hi", body="Hello",
                                  user_id=me.user_id)
        many = SimpleNamespace(id="c3", email="buyer@buyer.com, optout@buyer.com",
                               email_status="valid", account_id=None)
        with pytest.raises(SendRefused, match="one address"):
            await send_to_contact(ts, contact=many, subject="hi", body="Hello",
                                  user_id=me.user_id)
        assert sent == []
        allowed = SimpleNamespace(id="c2", email="buyer@buyer.com", email_status="valid",
                                  account_id=None)
        assert (await send_to_contact(ts, contact=allowed, subject="hi", body="Hello",
                                      user_id=me.user_id)).ok
    assert sent == ["buyer@buyer.com"]


# ---- 6. a manager pauses and stops; only the owner sends ------------------------------------------

async def _team(client, slug):
    from tests.test_engagement_desk import _member

    owner_token = await signup(client, slug=slug, email=f"boss@{slug}.com", company=slug.title())
    owner = principal_from_token(owner_token)
    rep = await _member(client, owner_token, f"ann@{slug}.com", "rep")
    manager = await _member(client, owner_token, f"max@{slug}.com", "manager")
    return owner, rep, manager


async def test_a_manager_can_pause_and_stop_a_reps_campaign_but_not_send_as_them(
        client, engine_on, monkeypatch):
    from nexus.engagement.sequences.service import create_campaign, enroll
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementEnrollment, MailboxConnection

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    owner, rep, manager = await _team(client, "mgrcamp")
    rep_id = principal_from_token(rep).user_id
    async with tenant_session(owner.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=rep_id, provider="google",
                                    email="ann@mgrcamp.com", status="connected")
        account = Account(name="Acme", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io")
        ts.add(contact)
        await ts.flush()
        campaign = await create_campaign(ts, name="Ann's", owner_user_id=rep_id,
                                         mailbox_id=mailbox.id,
                                         steps=[{"channel": "email", "angle": "Hello"}])
        await enroll(ts, campaign, [contact.id])
        enrollment = await ts.first(EngagementEnrollment)
        campaign_id, enrollment_id, contact_id = campaign.id, enrollment.id, contact.id

    base = f"/api/engagement/campaigns/{campaign_id}"
    # Seeing it is still a manager's job.
    assert (await client.get(base, headers=auth(manager))).status_code == 200
    refused = [
        ("put", f"{base}/steps", [{"channel": "email", "angle": "Mine now"}]),
        ("post", f"{base}/draft", None),
        ("post", f"{base}/approve-all", None),
        ("post", f"{base}/launch", None),
        ("post", f"{base}/resume", None),
        ("post", f"{base}/contacts", {"contact_ids": [contact_id]}),
        ("post", f"/api/engagement/enrollments/{enrollment_id}/resume", None),
        ("post", f"/api/engagement/enrollments/{enrollment_id}/send-now", None),
    ]
    for method, path, body in refused:
        r = await getattr(client, method)(path, headers=auth(manager),
                                          **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (path, r.status_code, r.text)
        assert "owner" in r.json()["detail"], path
    for path in (f"/api/engagement/enrollments/{enrollment_id}/pause", f"{base}/stop"):
        r = await client.post(path, headers=auth(manager))
        assert r.status_code in (204, 409), (path, r.text)
        assert r.status_code != 403


async def test_a_manager_can_close_a_reps_reply_but_not_answer_it(
        client, engine_on, mailbox_double, monkeypatch):
    from nexus.models.engagement import MailboxConnection, ReplyClassification
    from tests.test_engagement_desk import _replied

    tid, classification_id, mailbox_id = await _replied("mgrdesk", monkeypatch, mailbox_double)
    from nexus.core.security import create_access_token

    manager = create_access_token(user_id="max-the-manager", tenant_id=tid, role="manager")
    async with tenant_session(tid) as ts:
        assert (await ts.get(MailboxConnection, mailbox_id)).owner_user_id != "max-the-manager"
    base = f"/api/engagement/desk/{classification_id}"
    assert (await client.get(base, headers=auth(manager))).status_code == 200
    for path, body in ((f"{base}/draft", None),
                       (f"{base}/send", {"subject": "Re: hi", "body": "Hi Jane,\n\nYes.\n\nMax"}),
                       (f"{base}/save-draft", {"subject": "Re: hi", "body": "Hi"}),
                       (f"{base}/decide", {"decision": "reengage",
                                           "reengage_on": "2026-12-01"})):
        r = await client.post(path, headers=auth(manager),
                              **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (path, r.text)
    closed = await client.post(f"{base}/decide", headers=auth(manager),
                               json={"decision": "close"})
    assert closed.status_code == 204, closed.text
    async with tenant_session(tid) as ts:
        assert (await ts.get(ReplyClassification, classification_id)).status == "done"
    assert len(mailbox_double.delivered) == 1, "only the campaign's own first email"


def test_the_screens_offer_a_manager_only_what_the_server_allows():
    """There is no frontend test runner: these read the source, as the other UI tests do."""
    from pathlib import Path

    pages = Path(__file__).resolve().parent.parent / "frontend" / "src" / "pages"
    desk = (pages / "engagement" / "ReplyDeskPage.tsx").read_text(encoding="utf-8")
    assert "const canAnswer = d.can_answer !== false;" in desk
    assert "{openItem && canAnswer && (" in desk, "the answer box is the owner's"
    assert "c.can_resume !== false" in desk and "r.can_reschedule !== false" in desk
    campaign = (pages / "engagement" / "CampaignDetailPage.tsx").read_text(encoding="utf-8")
    assert "const isOwner = c ? c.owner_user_id === currentUserId : false;" in campaign
    assert "canAct={isOwner}" in campaign and "editable={settingUp && isOwner}" in campaign
    review = (pages / "engagement" / "ReviewQueue.tsx").read_text(encoding="utf-8")
    assert "const editable = canAct && item.status" in review
    settings = (pages / "SettingsPage.tsx").read_text(encoding="utf-8")
    assert "(a.mine || a.unassigned) ?" in settings and "Switch off" in settings
    mailboxes = (pages / "engagement" / "MailboxesPage.tsx").read_text(encoding="utf-8")
    assert "wrong_browser:" in mailboxes


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


@pytest.fixture
def folder():
    from nexus.engagement.mailboxes import registry
    from tests.test_engagement_sending import SentFolder

    box = SentFolder()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


@pytest.fixture
def mailbox_double():
    from nexus.engagement.mailboxes import registry
    from tests.test_engagement_replies import Mailbox

    box = Mailbox()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


# ---- 7. a reply that fails its sender checks --------------------------------------------------------

_FAILED = ("mx.google.com; dkim=none; spf=fail (google.com: domain of jane0@acme.io does not "
           "designate 203.0.113.9 as permitted sender) smtp.mailfrom=jane0@acme.io; "
           "dmarc=fail (p=REJECT sp=REJECT dis=NONE) header.from=acme.io")
_PASSED = ("mx.google.com; dkim=pass header.i=@acme.io; spf=pass smtp.mailfrom=jane0@acme.io; "
           "dmarc=pass (p=REJECT) header.from=acme.io")


@pytest.mark.parametrize("header,verdict", [
    (_FAILED, "fail"),
    (_PASSED, "pass"),
    ("spf=fail smtp.mailfrom=x.com; dkim=none", "fail"),
    ("spf=softfail smtp.mailfrom=x.com; dkim=pass header.d=x.com", "pass"),
    ("spf=pass; dkim=pass; dmarc=fail action=quarantine; compauth=fail reason=000", "fail"),
    ("spf=none; dkim=none; dmarc=bestguesspass action=none; compauth=pass", "pass"),
    (None, ""),
])
def test_the_receiving_servers_sender_check_is_read(header, verdict):
    from nexus.engagement.replies.parse import parse
    from tests.test_engagement_replies import _mail

    raw = _mail(sender="jane0@acme.io", body="Remove me",
                headers={"Authentication-Results": header} if header else None)
    assert parse(raw).sender_auth == verdict


def test_only_the_topmost_sender_check_counts():
    """A sender can write their own Authentication-Results; the receiving server's is added above."""
    from email.message import EmailMessage

    from nexus.engagement.replies.parse import parse

    message = EmailMessage()
    message["Authentication-Results"] = _FAILED
    message["From"] = "jane0@acme.io"
    message["To"] = "sam@seq.com"
    message["Subject"] = "Re: hi"
    message["Authentication-Results"] = _PASSED   # forged, lower down
    message.set_content("Remove me")
    assert parse(message.as_bytes()).sender_auth == "fail"


async def test_a_spoofed_unsubscribe_does_not_block_the_prospect(mailbox_double, monkeypatch):
    from nexus.engagement.suppression.service import active_block
    from tests.test_engagement_replies import _classification, _mail, _sent_first, _sync
    from tests.test_engagement_sequences import _enrollment

    tid, enrollment, our_id = await _sent_first("spoofunsub", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Please remove me from your list.",
                                headers={"Authentication-Results": _FAILED}))
    await _sync(tid, enrollment.mailbox_connection_id)
    classification = await _classification(tid)
    assert classification.category == "unclear" and classification.status == "open"
    assert "could not be verified" in classification.reasoning
    assert (await _enrollment(tid)).status == "paused"
    async with tenant_session(tid) as ts:
        assert await active_block(ts, "jane0@acme.io") is None


async def test_a_spoofed_interested_reply_gets_no_automatic_answer(mailbox_double, monkeypatch):
    from tests.test_auto_reply import _sweep, _switches
    from tests.test_engagement_replies import _classification, _mail, _sent_first, _sync

    tid, enrollment, our_id = await _sent_first("spoofsend", monkeypatch, mailbox_double)
    await _switches(tid, allow_send=True, send=True)
    mailbox_double.arrive(_mail(sender="Jane0 Buyer <jane0@acme.io>", in_reply_to=our_id,
                                body="Sounds interesting, let's talk next week.",
                                headers={"Authentication-Results": _FAILED}))
    await _sync(tid, enrollment.mailbox_connection_id)
    assert (await _classification(tid)).auto_reply is None
    assert await _sweep(tid) == {}
    assert len(mailbox_double.delivered) == 1


async def test_a_verified_reply_is_still_acted_on(mailbox_double, monkeypatch):
    from tests.test_engagement_replies import _classification, _mail, _sent_first, _sync

    tid, enrollment, our_id = await _sent_first("realunsub", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Not interested, thanks.",
                                headers={"Authentication-Results": _PASSED}))
    await _sync(tid, enrollment.mailbox_connection_id)
    assert (await _classification(tid)).category == "declined"
