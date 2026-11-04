"""Each mailbox sets its own writing style, over the workspace's (2026-11-04).

Asked by the SDR team: "signatures and style examples in the mailbox, set independently for each
mailbox". Decided with the owner: per mailbox, with the workspace's as the default. The merge is
per FIELD (`email_style.merged_settings`), and every draft from a mailbox, with the check of that
draft, reads the merged style.
"""
from __future__ import annotations

from nexus.agents.email_style import (
    draft_options,
    merged_settings,
    ps_line_on,
    samples,
    style_prompt,
)
from tests.conftest import auth, principal_from_token
from tests.test_email_playbook import _draft, _world, capturing  # noqa: F401  (fixture)
from tests.test_mailboxes_table import _mailbox

WORKSPACE = {"style": {"tone": "formal", "length_words": 120, "first_ask": "interest",
                       "samples": ["Workspace sample."]}}


def test_a_mailbox_with_no_style_gets_the_workspaces_exactly():
    assert merged_settings(WORKSPACE, None)["style"] == WORKSPACE["style"]
    assert merged_settings(WORKSPACE, {})["style"] == WORKSPACE["style"]


def test_each_field_falls_back_on_its_own():
    merged = merged_settings(WORKSPACE, {"tone": "warm", "samples": ["Mine."], "tone_extra": 1})
    assert merged["style"]["tone"] == "warm" and samples(merged) == ["Mine."]
    assert draft_options(merged) == {"word_cap": 120, "first_ask": "interest"}
    assert "tone_extra" not in merged["style"]
    # An empty field is "not set": it does not blank the workspace's.
    assert merged_settings(WORKSPACE, {"tone": "", "samples": []})["style"]["tone"] == "formal"
    assert ps_line_on(merged_settings(WORKSPACE, {"ps_line": True}))
    assert "warm" in style_prompt(merged) and "Workspace sample." not in style_prompt(merged)


def test_a_broken_blob_costs_the_mailboxs_style_not_the_draft():
    assert merged_settings(None, "nonsense") == {"style": {}}
    assert merged_settings({"style": "broken"}, {"tone": "x"})["style"] == {"tone": "x"}


async def test_the_owner_sets_it_on_their_mailbox_and_the_row_says_both(client):
    token = await _mailbox(client, "mst1", style=WORKSPACE["style"])
    [row] = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()
    assert row["own_style"] == {"tone": None, "length_words": None, "first_ask": None,
                                "ps_line": None, "samples": []}
    r = await client.patch(f"/api/engagement/mailboxes/{row['id']}", headers=auth(token),
                           json={"style": {"tone": "warm", "first_ask": "meeting",
                                           "samples": ["  Hi Jane,\n\nQuick one.  ", ""]}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["own_style"]["tone"] == "warm" and out["own_style"]["samples"] == [
        "Hi Jane,\n\nQuick one."]
    # In force: its own fields, the workspace's for the rest.
    assert out["writing_style"] == {"tone": "warm", "length_words": 120, "first_ask": "meeting",
                                    "ps_line": False, "sample_count": 1}
    assert out["workspace_style"]["tone"] == "formal"
    # Clearing it goes back to the workspace's.
    cleared = await client.patch(f"/api/engagement/mailboxes/{row['id']}", headers=auth(token),
                                 json={"style": {}})
    assert cleared.json()["writing_style"]["tone"] == "formal"


async def test_a_bad_style_is_refused_and_nothing_is_written(client):
    token = await _mailbox(client, "mst2")
    [row] = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()
    url = f"/api/engagement/mailboxes/{row['id']}"
    assert (await client.patch(url, headers=auth(token),
                               json={"style": {"length_words": 10}})).status_code == 422
    assert (await client.patch(url, headers=auth(token),
                               json={"style": {"first_ask": "demo"}})).status_code == 422
    markup = await client.patch(url, headers=auth(token),
                                json={"style": {"samples": ["<b>Hi</b> there"]}})
    assert markup.status_code == 422 and "plain text" in markup.json()["detail"]
    [after] = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()
    assert after["own_style"]["samples"] == []


async def test_only_the_owner_sets_a_mailboxs_style(client):
    from tests.conftest import signup

    token = await _mailbox(client, "mst3")
    [row] = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()
    other = await signup(client, slug="mst3b", email="o@mst3b.io", company="Mst3b")
    r = await client.patch(f"/api/engagement/mailboxes/{row['id']}", headers=auth(other),
                           json={"style": {"tone": "mine now"}})
    assert r.status_code in (403, 404)
    assert principal_from_token(token).tenant_id != principal_from_token(other).tenant_id


async def test_a_campaign_draft_is_written_in_its_mailboxs_style(capturing):  # noqa: F811
    from nexus.models.engagement import MailboxConnection
    from nexus.models.identity import Tenant
    from tests.conftest import tenant_session

    tid, *ids = await _world()
    async with tenant_session(tid) as ts:
        tenant = await ts.session.get(Tenant, tid)
        tenant.email_settings = {**dict(tenant.email_settings or {}),
                                 "style": {"tone": "formal", "samples": ["Workspace sample."]}}
        mailbox = await ts.get(MailboxConnection, ids[-1])
        mailbox.style = {"tone": "warm and brief", "samples": ["Mailbox sample, short."]}
    await _draft(tid, ids, kind="first", step_number=1)
    prompt = capturing.prompts[-1]
    assert "warm and brief" in prompt and "Mailbox sample, short." in prompt
    assert "Workspace sample." not in prompt and "formal" not in prompt


# ---- the screen ------------------------------------------------------------------------------------

def test_the_mailbox_row_edits_its_own_style_beside_the_signature():
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "frontend" / "src" / "pages" / "engagement"
           / "MailboxesPage.tsx").read_text(encoding="utf-8")
    assert "<StyleFields mailbox={mailbox} value={own} onChange={setOwn} />" in src
    assert "style: styleOf(own)" in src, "Save does not send the mailbox's style"
    for label in ('"Tone"', '"Length in words"', '"The first email asks for"', '"Sample emails"'):
        assert label in src
    assert "Workspace default (" in src, "a blank field must say which default it uses"
    tabs = (Path(__file__).resolve().parent.parent / "frontend" / "src" / "app"
            / "settingsTabs.tsx").read_text(encoding="utf-8")
    assert 'label: "Email defaults"' in tabs
