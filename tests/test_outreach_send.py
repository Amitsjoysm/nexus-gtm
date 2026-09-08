# tests/test_outreach_send.py
"""A rep sends the draft they just read, from their own mailbox, in one click.

The product could DRAFT a hyper-personalised email and could not SEND it. `EmailComposer` offered
Regenerate, Copy, and a `mailto:` link that dumps the draft into Outlook, so the rep's last step was
copy-and-paste. Every piece needed already existed and was referenced by nothing:

* `Tenant.email_settings["accounts"]` — per-mailbox SMTP a workspace configures in Settings, with a
  working Send-test button. Referenced ONLY by its own CRUD router.
* `integrations/email_sender.send_email()` — a real sender, used only for test messages.
* `outreach.email_send` — catalogued and priced, metered only on the orchestrator's SEP path, so a
  rep sending by hand was free.

Built, stored and unreachable, three times over, in the one place the product is named for.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


def _code(obj) -> str:
    """A function's SOURCE with its docstring and comments stripped.

    These checks look for what the code DOES, and the code here carries long comments explaining
    exactly the thing being asserted against — so a raw `getsource` match finds the explanation and
    not the behaviour. A check that trips on its own rationale is a check nobody can leave a
    comment near.
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(obj))
    tree = ast.parse(src)
    node = tree.body[0]
    if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        node.body = node.body[1:]          # drop the docstring
    return ast.unparse(tree)               # unparse never emits comments


async def _tenant_of(client, token) -> str:
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.identity import Tenant

    async with get_platform_sessionmaker()() as s:
        return (await s.scalars(select(Tenant.id).limit(1))).one()


def _mailbox(owner: str | None, **over) -> dict:
    box = {
        "id": "mb1", "label": "Mine", "provider": "gmail",
        "host": "smtp.example.com", "port": 587, "username": "rep@acme.com",
        "password": "app-password", "from_email": "rep@acme.com", "from_name": "Rep",
        "use_tls": True, "enabled": True, "default": True, "verified_at": None,
    }
    if owner is not None:
        box["owner_user_id"] = owner
    box.update(over)
    return box


class _Tenant:
    """Just enough of a Tenant for the resolver, which only reads `email_settings`."""

    def __init__(self, *boxes):
        self.email_settings = {"accounts": list(boxes)}


# ---- whose mailbox ------------------------------------------------------------------------------

def test_a_rep_sends_from_their_own_mailbox():
    """Attribution is the reason: a reply goes back to whoever sent it, so a shared workspace
    address turns every reply into a triage problem."""
    from nexus.outreach.send import resolve_rep_mailbox

    box = resolve_rep_mailbox(_Tenant(_mailbox("u1"), _mailbox("u2", id="mb2")), "u2")
    assert box["id"] == "mb2"


def test_a_rep_with_no_mailbox_is_told_to_connect_one():
    """The chosen policy: each rep connects their own before they can send. The refusal has to say
    where to go, or it is just a locked door."""
    from nexus.outreach.send import MailboxNotConnected, resolve_rep_mailbox

    with pytest.raises(MailboxNotConnected) as exc:
        resolve_rep_mailbox(_Tenant(_mailbox("someone-else")), "u1")
    assert "Settings" in str(exc.value)


def test_an_unowned_legacy_mailbox_is_not_used():
    """Ownership arrived after mailboxes did, so a workspace that configured one earlier has an
    entry nobody owns. Sending from it would send as an address whose replies go to an unknown
    person — worse than asking the rep to claim it."""
    from nexus.outreach.send import MailboxNotConnected, resolve_rep_mailbox

    with pytest.raises(MailboxNotConnected):
        resolve_rep_mailbox(_Tenant(_mailbox(None)), "u1")


def test_a_mailbox_with_no_password_cannot_send():
    """An entry with no app password cannot send, and reporting "sent" for one would be the silent
    failure this module exists to remove."""
    from nexus.outreach.send import MailboxNotConnected, resolve_rep_mailbox

    with pytest.raises(MailboxNotConnected) as exc:
        resolve_rep_mailbox(_Tenant(_mailbox("u1", password="")), "u1")
    assert "app password" in str(exc.value)


def test_a_disabled_mailbox_is_skipped():
    from nexus.outreach.send import MailboxNotConnected, resolve_rep_mailbox

    with pytest.raises(MailboxNotConnected):
        resolve_rep_mailbox(_Tenant(_mailbox("u1", enabled=False)), "u1")


def test_which_of_a_reps_own_mailboxes_sends_is_stable():
    """Their default first, else the first they connected. Which address a rep sends from must not
    vary run to run."""
    from nexus.outreach.send import resolve_rep_mailbox

    t = _Tenant(_mailbox("u1", id="mbA", default=False), _mailbox("u1", id="mbB", default=True))
    assert resolve_rep_mailbox(t, "u1")["id"] == "mbB"
    assert resolve_rep_mailbox(t, "u1")["id"] == "mbB"


# ---- the address guard ---------------------------------------------------------------------------

async def test_an_invalid_address_is_refused_until_acknowledged(fresh_db, monkeypatch):
    """The chosen policy: warn, do not block — but `invalid` takes an explicit acknowledgement.
    Those bounce, and bounces cost the sending domain its ability to deliver anything at all."""
    from nexus.outreach.send import SendRefused, send_to_contact

    class _C:
        email = "bad@acme.com"
        email_status = "invalid"

    sent: list[dict] = []

    async def _fake_send(settings, *, to, subject, body, html=None):
        sent.append({"to": to})

        class _R:
            ok, detail = True, "sent"
        return _R()

    monkeypatch.setattr("nexus.integrations.email_sender.send_email", _fake_send)

    with pytest.raises(SendRefused) as exc:
        await send_to_contact(None, contact=_C(), subject="s", body="b", user_id="u1")
    assert "invalid" in str(exc.value)
    assert sent == [], "a refused send must not reach the mail server"


def test_only_invalid_needs_the_acknowledgement():
    """`unknown` is the commonest verdict this verifier returns. Blocking it would stop most real
    sends, which is why the rep decides and only a bounce-certain verdict is gated."""
    from nexus.outreach.send import RISKY_STATUSES

    assert RISKY_STATUSES == ("invalid",)


async def test_a_contact_with_no_address_is_refused(fresh_db):
    from nexus.outreach.send import SendRefused, send_to_contact

    class _C:
        email = ""
        email_status = ""

    with pytest.raises(SendRefused):
        await send_to_contact(None, contact=_C(), subject="s", body="b", user_id="u1")


async def test_an_empty_body_is_refused(fresh_db):
    """Mirrors the blank-draft guard in `agents/messaging.py`: an empty message is not a message,
    and sending one is worse than refusing to."""
    from nexus.outreach.send import SendRefused, send_to_contact

    class _C:
        email = "ok@acme.com"
        email_status = "valid"

    with pytest.raises(SendRefused):
        await send_to_contact(None, contact=_C(), subject="s", body="   ", user_id="u1")


# ---- the endpoint ---------------------------------------------------------------------------------

async def test_the_endpoint_exists_and_is_rep_level():
    """Sending outreach is a rep's job. Gating it on `manage_workspace` would mean an SDR could
    draft and not send, which is the gap this closes."""

    from nexus.api.routers import contacts

    src = _code(contacts.send_email_to_contact)
    assert "Permission.manage_accounts" in src
    assert "requires_approval" not in src, "a second approval gate crept onto the one-click send"


async def test_no_mailbox_is_a_409_not_a_400(fresh_db, client):
    """The request is well-formed and it is the STATE that forbids it — the same distinction
    `admin_payment_credentials` draws for an unverified credential."""
    token = await signup(client, slug="sendtest", email="rep@acme.com", company="Acme")
    acc = await client.post(
        "/api/accounts", headers=auth(token), json={"name": "Target", "domain": "target.com"}
    )
    account_id = acc.json()["id"]
    c = await client.post(
        f"/api/accounts/{account_id}/contacts", headers=auth(token),
        json={"full_name": "Dana Vega", "email": "dana@target.com"},
    )
    r = await client.post(
        f"/api/contacts/{c.json()['id']}/send-email",
        headers=auth(token), json={"subject": "Hi", "body": "A real body."},
    )
    assert r.status_code == 409, r.text
    assert "mailbox" in r.text.lower()


# ---- the money ------------------------------------------------------------------------------------

def test_the_send_is_metered_on_the_priced_capability():
    """`outreach.email_send` was catalogued and priced from the billing milestone and metered only
    on the orchestrator's sequence path, so a rep sending by hand was free."""

    from nexus.billing.catalog import CAPABILITY_SEED
    from nexus.outreach import send as mod

    assert any(c.get("id") == "outreach.email_send" for c in CAPABILITY_SEED)
    src = _code(mod._meter_send)
    assert "metered(" in src and "outreach.email_send" in src


def test_metering_never_blocks_a_message_that_already_left():
    """Mirrors `orchestration.tools._meter_send`: a message that has already gone cannot be un-sent
    by a billing error, so metering runs after the send and swallows its own failures."""

    from nexus.outreach import send as mod

    assert "except Exception" in _code(mod._meter_send)
    body = _code(mod.send_to_contact)
    assert body.index("send_email(") < body.index("_meter_send"), (
        "metering must not gate the send"
    )
