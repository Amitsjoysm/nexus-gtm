"""Platform health says whether campaigns are moving, and if not, which stage stopped.

Everything after a person approves an opening email happens in the worker: the first send, every
follow-up, the "later" re-engagements, and reading replies. When any of it stops, the campaign
screen shows the same thing as a quiet week, nothing. These read what the worker leaves behind
(due times it has passed, mailboxes it has not read) so an operator can tell a dead worker from a
switch left off from a mailbox Google refuses to read.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.core.config import get_settings
from nexus.core.db import utcnow
from tests.conftest import tenant_session
from tests.test_engagement_sequences import _launched


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


async def _set(tid, **changes):
    """Apply changes to the one mailbox and the one enrollment in a launched workspace."""
    from nexus.models.engagement import EngagementEnrollment, MailboxConnection

    async with tenant_session(tid) as ts:
        mailbox = await ts.first(MailboxConnection)
        enrollment = await ts.first(EngagementEnrollment)
        for key, value in changes.items():
            target, _, field = key.partition("__")
            setattr(mailbox if target == "mailbox" else enrollment, field, value)


async def test_a_moving_campaign_reads_healthy(engine_on, monkeypatch):
    from nexus.engagement.health import engine_health

    now = utcnow()
    tid, _ = await _launched("healthy", monkeypatch)
    await _set(tid, mailbox__last_synced_at=now - timedelta(minutes=3),
               enrollment__next_action_at=now + timedelta(days=2))
    status, detail = await engine_health(now=now)
    assert status == "ok", detail
    assert "1 running campaign" in detail and "nothing overdue" in detail


async def test_a_step_long_past_due_means_the_worker_is_not_sending(engine_on, monkeypatch):
    from nexus.engagement.health import engine_health

    now = utcnow()
    tid, _ = await _launched("overdue", monkeypatch)
    await _set(tid, mailbox__last_synced_at=now - timedelta(minutes=3),
               enrollment__next_action_at=now - timedelta(hours=5))
    status, detail = await engine_health(now=now)
    assert status == "error"
    assert "1 email overdue" in detail and "5h" in detail and "worker" in detail


async def test_a_later_reply_whose_date_has_passed_counts_as_overdue(engine_on, monkeypatch):
    from nexus.engagement.health import engine_health

    now = utcnow()
    tid, _ = await _launched("snoozed", monkeypatch)
    await _set(tid, mailbox__last_synced_at=now - timedelta(minutes=3),
               enrollment__status="snoozed", enrollment__next_action_at=None,
               enrollment__snoozed_until=now - timedelta(days=2))
    status, detail = await engine_health(now=now)
    assert status == "error" and "overdue" in detail


async def test_a_mailbox_not_read_for_hours_means_replies_are_not_picked_up(engine_on,
                                                                              monkeypatch):
    from nexus.engagement.health import engine_health

    now = utcnow()
    tid, _ = await _launched("unread", monkeypatch)
    await _set(tid, mailbox__last_synced_at=now - timedelta(hours=3),
               enrollment__next_action_at=now + timedelta(days=2))
    status, detail = await engine_health(now=now)
    assert status == "error"
    assert "not read for 3h" in detail and "replies" in detail


async def test_a_mailbox_fault_is_named(engine_on, monkeypatch):
    from nexus.engagement.health import engine_health

    now = utcnow()
    tid, _ = await _launched("fault", monkeypatch)
    await _set(tid, mailbox__last_synced_at=now - timedelta(minutes=3),
               mailbox__last_error="Replies could not be read: Gmail API has not been used",
               enrollment__next_action_at=now + timedelta(days=2))
    status, detail = await engine_health(now=now)
    assert status == "degraded" and "Gmail API has not been used" in detail


async def test_the_switch_off_with_campaigns_running_is_an_error(monkeypatch):
    from nexus.engagement.health import engine_health

    now = utcnow()
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    await _launched("switchedoff", monkeypatch)
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    status, detail = await engine_health(now=now)
    assert status == "error"
    assert "engagement_campaigns_enabled" in detail and "1 running campaign" in detail


async def test_nothing_connected_is_unconfigured_not_broken(engine_on):
    from nexus.engagement.health import engine_health

    status, detail = await engine_health(now=utcnow())
    assert status == "unconfigured" and "no mailbox" in detail


def test_the_review_queue_says_why_an_opening_email_could_not_be_written():
    """The draft endpoint returns each failure's reason; the screen used to drop it and say only
    "try again", which reads the same for a model outage and a contact with nothing to write from."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1]
              / "frontend/src/pages/engagement/ReviewQueue.tsx").read_text(encoding="utf-8")
    failure = source[source.index("could not be written") - 400:source.index("could not be written") + 300]
    assert "pass.errors" in failure and "reason" in failure


def test_platform_health_runs_it():
    from nexus.api.routers.admin_health import _PROBES

    assert "campaigns" in dict(_PROBES)
