"""Engagement tables: the constraints that make a double send or a double block impossible."""
from __future__ import annotations

import importlib.util
import pathlib

import pytest
from sqlalchemy.exc import IntegrityError

from nexus.core.db import get_sessionmaker, utcnow
from nexus.models.account import Account, Contact
from nexus.models.engagement import (
    DoNotContact,
    EngagementCampaign,
    EngagementEnrollment,
    EngagementMessage,
    MailboxConnection,
)
from nexus.models.identity import User
from tests.conftest import make_tenant, tenant_session


async def _user(email: str = "sdr@acme.com") -> str:
    async with get_sessionmaker()() as s:
        user = User(email=email, full_name="Sam Rep", password_hash="x")
        s.add(user)
        await s.commit()
        return user.id


async def _world(ts, user_id: str) -> tuple[MailboxConnection, EngagementCampaign, Contact]:
    account = Account(name="Acme", domain="acme.io")
    ts.add(account)
    await ts.flush()
    contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io")
    mailbox = MailboxConnection(owner_user_id=user_id, provider="google", email="sdr@acme.com")
    ts.add_all([contact, mailbox])
    await ts.flush()
    campaign = EngagementCampaign(
        name="Q4 outbound", owner_user_id=user_id, mailbox_connection_id=mailbox.id
    )
    ts.add(campaign)
    await ts.flush()
    return mailbox, campaign, contact


async def test_a_contact_is_enrolled_in_a_campaign_once():
    tid = await make_tenant()
    uid = await _user()
    with pytest.raises(IntegrityError):
        async with tenant_session(tid) as ts:
            mailbox, campaign, contact = await _world(ts, uid)
            for _ in range(2):
                ts.add(EngagementEnrollment(
                    campaign_id=campaign.id, contact_id=contact.id,
                    account_id=contact.account_id, mailbox_connection_id=mailbox.id,
                ))
            await ts.flush()


async def test_one_outbound_message_per_step_while_inbound_is_unlimited():
    tid = await make_tenant()
    uid = await _user()
    async with tenant_session(tid) as ts:
        mailbox, campaign, contact = await _world(ts, uid)
        enrollment = EngagementEnrollment(
            campaign_id=campaign.id, contact_id=contact.id, account_id=contact.account_id,
            mailbox_connection_id=mailbox.id,
        )
        ts.add(enrollment)
        await ts.flush()
        for n in range(2):
            ts.add(EngagementMessage(
                mailbox_connection_id=mailbox.id, enrollment_id=enrollment.id, direction="in",
                status="received", step_index=0, provider_message_id=f"in-{n}",
            ))
        ts.add(EngagementMessage(
            mailbox_connection_id=mailbox.id, enrollment_id=enrollment.id, direction="out",
            status="queued", step_index=0,
        ))
        await ts.flush()
        enrollment_id, mailbox_id = enrollment.id, mailbox.id

    with pytest.raises(IntegrityError):
        async with tenant_session(tid) as ts:
            ts.add(EngagementMessage(
                mailbox_connection_id=mailbox_id, enrollment_id=enrollment_id, direction="out",
                status="queued", step_index=0,
            ))
            await ts.flush()


async def test_an_idempotency_key_is_used_once_per_workspace():
    tid = await make_tenant()
    uid = await _user()
    async with tenant_session(tid) as ts:
        mailbox, _campaign, _contact = await _world(ts, uid)
        mailbox_id = mailbox.id
        ts.add(EngagementMessage(
            mailbox_connection_id=mailbox_id, direction="out", kind="reengage", status="queued",
            idempotency_key="reengage:enr1:2027-06-01",
        ))
    with pytest.raises(IntegrityError):
        async with tenant_session(tid) as ts:
            ts.add(EngagementMessage(
                mailbox_connection_id=mailbox_id, direction="out", kind="reengage",
                status="queued", idempotency_key="reengage:enr1:2027-06-01",
            ))
            await ts.flush()


async def test_an_address_has_one_active_block_and_can_be_blocked_again_after_a_lift():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        first = DoNotContact(email="jane@acme.io", reason="declined")
        ts.add(first)
        await ts.flush()
        first.lifted_at = utcnow()
        await ts.flush()
        ts.add(DoNotContact(email="jane@acme.io", reason="unsubscribed"))
        await ts.flush()
    with pytest.raises(IntegrityError):
        async with tenant_session(tid) as ts:
            ts.add(DoNotContact(email="jane@acme.io", reason="manual"))
            await ts.flush()


def test_every_engagement_table_is_enrolled_for_row_level_security():
    script = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "apply_rls.py"
    spec = importlib.util.spec_from_file_location("apply_rls", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    enrolled = set(module._tenant_tables())
    for table in (
        "mailbox_connections", "sequence_templates", "engagement_campaigns", "engagement_steps",
        "engagement_enrollments", "engagement_threads", "engagement_messages",
        "reply_classifications", "do_not_contact", "training_consents", "ledger_outbox",
    ):
        assert table in enrolled, f"{table} would get no row-level security policy"
