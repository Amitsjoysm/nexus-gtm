"""My mailboxes as one table row per mailbox, expanding in place (6.2, 2026-10-29).

The table needed three facts the list did not carry: whose mailbox it is in words (the team view
showed an id's worth of nothing), the daily figure `sent_today` is read against (advice, never a
cap: `sending/limits.py`), and the writing style drafts from the mailbox follow, which is the
workspace's and readable through an admin-only endpoint until now. The screen is read as source.
"""
from __future__ import annotations

import re
from pathlib import Path

from nexus.engagement.sending.limits import VOLUME_WARNING_THRESHOLD
from tests.conftest import auth, principal_from_token, signup, tenant_session

PAGE = Path(__file__).resolve().parent.parent / "frontend" / "src" / "pages" / "engagement"


def _bundle():
    return {"access_token": "at-plaintext", "refresh_token": "rt-plaintext",
            "expires_at": 1_900_000_000, "refreshed_at": 1_800_000_000}


async def _mailbox(client, slug: str, *, style: dict | None = None):
    from nexus.engagement.mailboxes.service import upsert_connection
    from nexus.models.identity import Tenant

    token = await signup(client, slug=slug, email=f"sdr@{slug}co.com", company=slug.title())
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        await upsert_connection(ts, owner_user_id=me.user_id, provider="google",
                                email=f"sdr@{slug}co.com", display_name="Sam", bundle=_bundle(),
                                scopes=[], timezone="UTC")
        if style is not None:
            tenant = await ts.session.get(Tenant, me.tenant_id)
            tenant.email_settings = {**dict(tenant.email_settings or {}), "style": style}
    return token


async def test_each_row_names_its_owner_and_the_advised_daily_figure(client):
    token = await _mailbox(client, "tbl")
    [row] = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()
    assert row["owner_name"] == "Rep"            # the signup's full name, not an id
    assert row["daily_advised"] == VOLUME_WARNING_THRESHOLD and row["sent_today"] == 0
    assert "tokens" not in row and "at-plaintext" not in str(row)


async def test_the_row_says_which_writing_style_its_drafts_follow(client):
    token = await _mailbox(client, "sty", style={"tone": "warm and direct", "length_words": 90,
                                                 "first_ask": "meeting", "samples": ["secret"]})
    [row] = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()
    assert row["writing_style"] == {"tone": "warm and direct", "length_words": 90,
                                    "first_ask": "meeting", "ps_line": False, "sample_count": 1}
    # Samples are the admin's working material; a mailbox row is not where they are read.
    assert "secret" not in str(row)


async def test_a_workspace_with_no_style_reads_the_defaults(client):
    token = await _mailbox(client, "nosty")
    [row] = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()
    assert row["writing_style"] == {"tone": "", "length_words": None, "first_ask": "interest",
                                    "ps_line": False, "sample_count": 0}


async def test_a_stored_length_that_is_not_a_number_is_not_passed_on(client):
    token = await _mailbox(client, "badlen", style={"length_words": "lots"})
    [row] = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()
    assert row["writing_style"]["length_words"] is None


# ---- the screen ------------------------------------------------------------------------------------

def _page() -> str:
    return (PAGE / "MailboxesPage.tsx").read_text(encoding="utf-8")


def test_one_table_row_per_mailbox_with_the_columns_asked_for():
    page = _page()
    # The connected mailboxes' table; the app-password one below it has its own four columns.
    main = page.split("function AppPasswordTable", 1)[0]
    headers = re.findall(r'<th scope="col"[^>]*>([^<]+)</th>', main)
    assert headers == ["Mailbox", "Owner", "Provider", "Status", "Last read", "Sent today"]
    # The detail row spans every column, the actions column included.
    assert main.count('<th scope="col"') == 7 and "colSpan={7}" in page
    assert "<MailboxCard" not in page


def test_a_row_opens_in_place_and_starts_closed():
    page = _page()
    assert "useState<ReadonlySet<string>>(() => new Set())" in page
    assert "aria-expanded={expanded}" in page and "aria-controls={panelId}" in page
    detail = page[page.index("{expanded && ("):page.index("<Modal")]
    for part in ('label="Signature"', 'label="Your timezone"', 'label="Reply confidence bar"',
                 "<WritingStyle", 'label="Act on clear replies by itself"',
                 'label="Let the AI send replies"', "Check connection"):
        assert part in detail, part


def test_the_status_carries_its_reason_and_disconnect_waits_in_the_detail():
    page = _page()
    row = page[page.index("<tr className={cn(styles.row"):page.index("{expanded && (")]
    assert "mailbox.last_error" in row and "Paused by the provider until" in row
    assert "setConfirming(true)" not in row and "Reconnect" in row
    detail = page[page.index("{expanded && ("):page.index("<Modal")]
    assert "setConfirming(true)" in detail   # still confirmed by the modal before anything happens


def test_the_style_is_read_here_and_edited_by_an_admin():
    page = _page()
    style = page[page.index("function WritingStyle"):]
    assert 'to="/settings/email"' in style and "canEdit" in style
    assert "api.emailStyle(" not in page   # admin-only; the row's own field carries it


async def test_app_password_mailboxes_are_listed_beside_the_others_without_their_secrets(client):
    """2026-11-04, "multiple mailboxes not visible": mailboxes added with an app password lived only
    in Email defaults. They are listed in My mailboxes too, marked for one-off emails, with nothing
    of the password or the server."""
    from nexus.models.identity import Tenant

    token = await _mailbox(client, "apw")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        tenant = await ts.session.get(Tenant, me.tenant_id)
        tenant.email_settings = {**dict(tenant.email_settings or {}), "accounts": [
            {"id": "a1", "label": "My Gmail", "provider": "gmail", "username": "me@apwco.com",
             "from_email": "me@apwco.com", "password": "app-pass-secret", "enabled": True,
             "owner_user_id": me.user_id},
            {"id": "a2", "label": "Colleague", "provider": "smtp", "username": "c@apwco.com",
             "from_email": "c@apwco.com", "password": "", "enabled": True,
             "owner_user_id": "someone-else"},
        ]}
    r = await client.get("/api/engagement/mailboxes/app-password", headers=auth(token))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [(m["email"], m["mine"], m["ready"]) for m in rows] == [("me@apwco.com", True, True)]
    assert "app-pass-secret" not in r.text and "host" not in rows[0]
    team = (await client.get("/api/engagement/mailboxes/app-password", headers=auth(token),
                             params={"team": "true"})).json()
    assert {m["email"]: m["ready"] for m in team} == {"me@apwco.com": True, "c@apwco.com": False}
