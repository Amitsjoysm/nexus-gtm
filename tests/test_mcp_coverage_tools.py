"""The tools added on 2026-10-19 so AI apps reach most of the product (owner decisions: campaign
drafts, Populate, queueing and logging calls, taking members off a list, the reply desk's read,
draft and save). Each runs through the real API on an offline workspace, like test_mcp_tools.py."""
from __future__ import annotations

import pytest

from nexus.mcp import credentials
from nexus.mcp.gateway import Gateway
from nexus.mcp.schema import SchemaError, validate
from nexus.mcp.tools import by_name
from tests.conftest import principal_from_token, signup


def _actor(token: str) -> credentials.Actor:
    p = principal_from_token(token)
    return credentials.Actor(
        user_id=p.user_id, tenant_id=p.tenant_id, role=p.role, token_version=0,
        connection_id="conn-test", kind="api_key", scopes=frozenset(credentials.SCOPES),
        name="test")


async def _call(client, token, tool, **args):
    spec = by_name(tool)
    return await spec.handler(Gateway(client._transport.app, _actor(token)),
                              validate(spec.input_schema, args))


async def _account_and_contact(token: str, *, email: str = "ava@northwind.example"):
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession, apply_rls
    from nexus.models.account import Account, Contact

    p = principal_from_token(token)
    async with get_sessionmaker()() as s:
        await apply_rls(s, p.tenant_id)
        ts = TenantSession(s, p.tenant_id)
        a = Account(name="Northwind Example", domain="northwind.example")
        ts.add(a)
        await s.flush()
        c = Contact(account_id=a.id, full_name="Ava Patel", title="VP Sales", email=email)
        ts.add(c)
        await s.commit()
        return a.id, c.id


async def _mailbox(token: str) -> None:
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession, apply_rls
    from nexus.models.engagement import MailboxConnection

    p = principal_from_token(token)
    async with get_sessionmaker()() as s:
        await apply_rls(s, p.tenant_id)
        TenantSession(s, p.tenant_id).add(MailboxConnection(
            owner_user_id=p.user_id, provider="google", email="rep@seller.example",
            status="connected"))
        await s.commit()


# ---- read ------------------------------------------------------------------------------------

async def test_the_overview_and_every_queue_answer(client):
    token = await signup(client, slug="cv1", email="o@cv1.com")
    overview = await _call(client, token, "get_workspace_overview")
    assert overview.ok, overview.message
    assert set(overview.data) >= {"overview", "today", "ideal_customer_profile"}
    for kind in ("alerts", "replies", "calls"):
        out = await _call(client, token, "list_queue", kind=kind)
        assert out.ok, (kind, out.message)
        assert out.data["kind"] == kind and isinstance(out.data["items"], list)


async def test_network_search_answers_or_says_why_not(client):
    token = await signup(client, slug="cv2", email="o@cv2.com")
    out = await _call(client, token, "search_network", query="CFOs at fintechs")
    assert out.ok or (out.status < 500 and out.message), out.message


# ---- calls -----------------------------------------------------------------------------------

async def test_a_call_is_queued_with_a_script_briefed_and_logged(client):
    token = await signup(client, slug="cv3", email="o@cv3.com")
    account_id, contact_id = await _account_and_contact(token)
    queued = await _call(client, token, "queue_call", account_id=account_id,
                         contact_id=contact_id, reason="Raised a Series B")
    assert queued.ok, queued.message
    assert "Queued, not dialled" in queued.data["note"]
    task_id = queued.data["task"]["id"]
    brief = await _call(client, token, "fetch", id=f"call:{task_id}")
    assert brief.ok and brief.data["type"] == "call", brief.message
    logged = await _call(client, token, "log_call_outcome", task_id=task_id,
                         disposition="meeting_booked", notes="Demo on Thursday")
    assert logged.ok, logged.message
    with pytest.raises(SchemaError):
        validate(by_name("log_call_outcome").input_schema,
                 {"task_id": task_id, "disposition": "hung_up"})


# ---- lists -----------------------------------------------------------------------------------

async def test_removing_from_a_list_keeps_the_record(client):
    token = await signup(client, slug="cv4", email="o@cv4.com")
    account_id, _ = await _account_and_contact(token)
    made = await _call(client, token, "create_list", name="Q4", account_ids=[account_id])
    list_id = made.data["list"]["id"]
    out = await _call(client, token, "remove_from_list", list_id=list_id, account_ids=[account_id])
    assert out.ok and out.data["removed"] == 1 and out.data["members"] == 0, out.message
    still = await _call(client, token, "fetch", id=f"account:{account_id}")
    assert still.ok


# ---- campaigns -------------------------------------------------------------------------------

async def test_a_campaign_draft_needs_the_users_own_mailbox(client):
    token = await signup(client, slug="cv5", email="o@cv5.com")
    _, contact_id = await _account_and_contact(token)
    out = await _call(client, token, "create_campaign_draft", name="Q4", contact_ids=[contact_id])
    assert not out.ok and out.status == 409 and "mailbox" in out.message


async def test_a_campaign_draft_needs_someone_to_go_to(client):
    token = await signup(client, slug="cv6", email="o@cv6.com")
    out = await _call(client, token, "create_campaign_draft", name="Q4")
    assert not out.ok and "list_id" in out.message


async def test_a_campaign_draft_from_a_list_adds_its_people_and_sends_nothing(client):
    token = await signup(client, slug="cv7", email="o@cv7.com")
    await _mailbox(token)
    _, contact_id = await _account_and_contact(token)
    plist = (await _call(client, token, "create_list", name="VPs", kind="contact",
                         contact_ids=[contact_id])).data["list"]["id"]
    out = await _call(client, token, "create_campaign_draft", name="Q4 VPs", list_id=plist,
                      draft_first_emails=False)
    assert out.ok, out.message
    assert out.data["added"] == 1 and out.data["drafted"] == 0
    assert "nothing was sent" in out.data["note"]
    campaign = await _call(client, token, "fetch", id=f"campaign:{out.data['campaign_id']}")
    assert campaign.ok and campaign.data["campaign"]["status"] not in ("active", "completed")
    # The list is recorded as the campaign's source, as the list page's own button does.
    from nexus.core.db import get_sessionmaker
    from nexus.models.engagement import EngagementCampaign

    async with get_sessionmaker()() as s:
        row = await s.get(EngagementCampaign, out.data["campaign_id"])
    assert row.source_list_id == plist


async def test_drafting_opening_emails_reports_what_it_wrote(client):
    from tests.conftest import seed_relevance_profile
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession, apply_rls

    token = await signup(client, slug="cv8", email="o@cv8.com")
    p = principal_from_token(token)
    async with get_sessionmaker()() as s:
        await apply_rls(s, p.tenant_id)
        await seed_relevance_profile(TenantSession(s, p.tenant_id))
        await s.commit()
    await _mailbox(token)
    _, contact_id = await _account_and_contact(token)
    out = await _call(client, token, "create_campaign_draft", name="Q4", contact_ids=[contact_id])
    assert out.ok, out.message
    assert out.data["added"] == 1 and (out.data["drafted"] == 1 or out.data.get("draft_note"))


# ---- the reply desk and alerts ------------------------------------------------------------------

async def test_an_unknown_reply_or_alert_is_a_message_not_a_crash(client):
    token = await signup(client, slug="cv9", email="o@cv9.com")
    for tool, args in (("draft_reply", {"reply_id": "nope123"}),
                       ("save_reply_draft", {"reply_id": "nope123", "body": "Hi"}),
                       ("acknowledge_alert", {"alert_id": "nope123"}),
                       ("fetch", {"id": "reply:nope123"})):
        out = await _call(client, token, tool, **args)
        assert not out.ok and out.status == 404, (tool, out.status, out.message)


# ---- research on existing records ----------------------------------------------------------------

async def test_research_on_existing_records_answers_without_crashing(client):
    token = await signup(client, slug="cv10", email="o@cv10.com")
    account_id, contact_id = await _account_and_contact(token)
    for tool, args in (("enrich_account", {"account_id": account_id}),
                       ("verify_email", {"contact_id": contact_id}),
                       ("find_phone", {"contact_id": contact_id}),
                       ("find_similar_people", {"contact_id": contact_id, "mode": "existing"}),
                       ("refresh_account", {"account_id": account_id})):
        out = await _call(client, token, tool, **args)
        assert out.ok or (out.status < 500 and out.message), (tool, out.status, out.message)


async def test_populate_without_a_profile_is_refused_in_words(client):
    token = await signup(client, slug="cv11", email="o@cv11.com")
    out = await _call(client, token, "add_icp_companies", count=5)
    assert out.ok or (out.status < 500 and out.message), (out.status, out.message)
