"""Offline tests for the Channel & Cadence engine (sub-project C). Zero-network."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from nexus.core.config import get_settings
from tests.conftest import make_tenant, tenant_session


def test_cadence_config_defaults():
    s = get_settings()
    assert s.cadence_enabled is False
    assert s.cadence_tick_interval_s == 60
    assert s.cadence_batch_size == 100
    assert s.cadence_max_duration_days == 30


from nexus.models.cadence import (
    Cadence,
    CadenceStep,
    CadenceEnrollment,
    CadenceTouch,
    ENROLL_ACTIVE,
    ENROLL_TERMINAL,
    ENROLL_COMPLETED,
    ENROLL_STOPPED,
    STOP_REPLIED,
    TOUCH_SENT,
    TOUCH_AWAITING_APPROVAL,
)


async def test_cadence_models_and_uniques():
    tid = await make_tenant(slug="cad-models", name="Cad Models")
    async with tenant_session(tid) as ts:
        cad = Cadence(tenant_id=tid, name="3-touch")
        ts.add(cad)
        await ts.flush()
        assert cad.is_active is True

        ts.add(CadenceStep(tenant_id=tid, cadence_id=cad.id, step_index=0,
                           delay_days=0, angle="intro"))
        await ts.flush()
        # Duplicate (cadence_id, step_index) must violate the unique constraint.
        ts.add(CadenceStep(tenant_id=tid, cadence_id=cad.id, step_index=0,
                           delay_days=2, angle="dupe"))
        with pytest.raises(Exception):
            await ts.flush()
        await ts.session.rollback()


def test_enroll_terminal_set():
    assert ENROLL_TERMINAL == frozenset({ENROLL_COMPLETED, ENROLL_STOPPED})
    assert ENROLL_ACTIVE == "active"
    assert STOP_REPLIED == "replied"
    assert TOUCH_SENT == "sent"
    assert TOUCH_AWAITING_APPROVAL == "awaiting_approval"


from nexus.models.campaign import Campaign


async def test_campaign_cadence_columns_default():
    tid = await make_tenant(slug="cad-camp", name="Cad Camp")
    async with tenant_session(tid) as ts:
        c = Campaign(tenant_id=tid, name="cad", list_id="l1")
        ts.add(c)
        await ts.flush()
        assert c.cadence_id is None          # NULL = backward-compatible single-touch path
        assert c.review_each_touch is False


from nexus.orchestration.planner import get_planner


def test_research_compose_threads_angle():
    plan = get_planner().plan(
        "research_compose", {"account_id": "a1", "angle": "case study follow-up"}
    )
    compose = next(s for s in plan if s["tool"] == "compose_message")
    assert compose["inputs"].get("angle") == "case study follow-up"


def test_research_compose_without_angle_unchanged():
    plan = get_planner().plan("research_compose", {"account_id": "a1"})
    compose = next(s for s in plan if s["tool"] == "compose_message")
    assert "angle" not in compose["inputs"]


from nexus.cadences.service import CadenceError, get_cadence_service
from nexus.models.account import Account
from nexus.models.campaign import Campaign, CampaignTarget, TARGET_DRAFTED


async def _make_cadence(ts, steps):
    return await get_cadence_service().create_cadence(
        ts, name="seq", description=None, steps=steps, created_by_user_id=None
    )


async def test_create_cadence_validates_and_orders():
    tid = await make_tenant(slug="cad-create", name="Cad Create")
    async with tenant_session(tid) as ts:
        cad = await _make_cadence(ts, [
            {"delay_days": 0, "angle": "intro"},
            {"delay_days": 3, "angle": "nudge"},
        ])
        steps = await get_cadence_service().list_steps(ts, cad.id)
        assert [s.step_index for s in steps] == [0, 1]
        assert steps[0].delay_days == 0 and steps[1].delay_days == 3

        with pytest.raises(CadenceError):
            await _make_cadence(ts, [])  # no steps
        with pytest.raises(CadenceError):
            await _make_cadence(ts, [{"delay_days": 0, "channel": "sms"}])  # v1 email-only
        with pytest.raises(CadenceError):
            await _make_cadence(ts, [{"delay_days": -1}])  # negative delay


async def test_enroll_sets_first_due():
    tid = await make_tenant(slug="cad-enroll", name="Cad Enroll")
    async with tenant_session(tid) as ts:
        cad = await _make_cadence(ts, [{"delay_days": 2, "angle": "intro"}])
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        camp = Campaign(tenant_id=tid, name="c", list_id="l1", cadence_id=cad.id)
        ts.add(camp)
        await ts.flush()
        target = CampaignTarget(tenant_id=tid, campaign_id=camp.id, account_id=acc.id,
                                status=TARGET_DRAFTED, draft={"contact_id": "ct1"})
        ts.add(target)
        await ts.flush()

        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        e = await get_cadence_service().enroll(ts, camp, target, now=t0)
        assert e.status == "active"
        assert e.current_step_index == 0
        assert e.account_id == acc.id
        assert e.contact_id == "ct1"
        assert e.started_at == t0
        assert e.next_touch_at == t0 + timedelta(days=2)


from nexus.campaigns.service import get_campaign_service
from nexus.outcomes.service import get_outcome_service
from nexus.models.account import Contact
from nexus.models.cadence import (
    ENROLL_PAUSED,
    STOP_MANUAL,
    STOP_MAX_TOUCHES,
    STOP_UNDELIVERABLE,
    TOUCH_SKIPPED,
)
from nexus.models.outcome import Outcome

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def ts():
    """A TenantSession bound to a fresh tenant. The context commits on exit."""
    tid = await make_tenant(slug="cad-svc", name="Cad Svc")
    async with tenant_session(tid) as session:
        yield session


async def _enrollable(ts, now, *, steps, review_each_touch=False, email="lead@acme.com"):
    """Build an account + contact, a cadence, a campaign wired to it, and one DRAFTED
    target; enroll the target. Returns (campaign, enrollment, account, contact). Pass a
    malformed ``email`` to drive the undeliverable path (the stub verifier rejects bad syntax)."""
    acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
    ts.add(acc)
    await ts.flush()
    contact = Contact(
        tenant_id=ts.tenant_id, account_id=acc.id, full_name="Lead", email=email
    )
    ts.add(contact)
    await ts.flush()
    cadence = await get_cadence_service().create_cadence(
        ts, name="multi-touch", description=None, steps=steps, created_by_user_id="u1"
    )
    campaign = Campaign(
        tenant_id=ts.tenant_id,
        name="Q3",
        list_id="l1",
        icp={"industries": ["SaaS"]},
        sequence="ai-orchestrated-outbound",
        cadence_id=cadence.id,
        review_each_touch=review_each_touch,
        created_by_user_id="u1",
    )
    ts.add(campaign)
    await ts.flush()
    target = CampaignTarget(
        tenant_id=ts.tenant_id,
        campaign_id=campaign.id,
        account_id=acc.id,
        status=TARGET_DRAFTED,
        draft={"contact_id": contact.id},
    )
    ts.add(target)
    await ts.flush()
    enrollment = await get_cadence_service().enroll(ts, campaign, target, now=now)
    return campaign, enrollment, acc, contact


async def test_cadence_happy_path_sends_each_touch(ts):
    svc = get_cadence_service()
    _, e, _, _ = await _enrollable(
        ts,
        NOW,
        steps=[
            {"delay_days": 0, "angle": "introduce the value prop"},
            {"delay_days": 3, "angle": "follow up with a case study"},
        ],
    )
    assert e.status == ENROLL_ACTIVE
    assert e.current_step_index == 0

    # Tick at t0: the first touch sends and the enrollment advances to step 1 (due in 3 days).
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 1
    await ts.refresh(e)
    assert e.current_step_index == 1
    assert e.status == ENROLL_ACTIVE
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    assert [t.status for t in touches] == [TOUCH_SENT]

    # Same instant: step 1 is not due yet — no work, no double send.
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 0

    # t0 + 3 days: the final touch sends and the enrollment completes.
    later = NOW + timedelta(days=3)
    assert await svc.advance_due_for_tenant(ts, now=later, limit=100) == 1
    await ts.refresh(e)
    assert e.status == ENROLL_COMPLETED
    assert e.completed_at is not None
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    assert sorted(t.step_index for t in touches) == [0, 1]
    assert all(t.status == TOUCH_SENT for t in touches)

    # Each send recorded an Outcome("sent") for manager attribution.
    outcomes = await ts.list(Outcome, Outcome.stage == "sent")
    assert len(outcomes) == 2


async def test_cadence_touch_is_idempotent_on_reclaim(ts):
    """Structural idempotency: a crash that sent a touch but never advanced must not double
    send. We simulate it — a TOUCH_SENT row exists for step 0 while the enrollment is still
    parked at step 0 and due. The next tick must NOT re-send or duplicate the touch; it just
    advances. This is the same guarantee the unique (enrollment_id, step_index) gives at the
    DB layer, asserted at the service layer."""
    svc = get_cadence_service()
    _, e, _, _ = await _enrollable(
        ts,
        NOW,
        steps=[
            {"delay_days": 0, "angle": "intro"},
            {"delay_days": 2, "angle": "bump"},
        ],
    )
    # Pre-existing sent touch for step 0, enrollment still at step 0 (the crash window).
    ts.add(CadenceTouch(
        tenant_id=ts.tenant_id, enrollment_id=e.id, step_index=0,
        status=TOUCH_SENT, sent_at=NOW,
    ))
    await ts.flush()

    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 1
    await ts.refresh(e)
    assert e.current_step_index == 1          # advanced past the already-sent step
    assert e.status == ENROLL_ACTIVE
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    assert [t.step_index for t in touches] == [0]   # no duplicate touch for step 0
    assert [t.status for t in touches] == [TOUCH_SENT]
    # No second send happened: still zero Outcome("sent") rows (the existing touch was
    # pre-inserted directly, bypassing _send, so the count proves no re-send).
    assert await ts.list(Outcome, Outcome.stage == "sent") == []


async def test_cadence_stops_on_reply(ts):
    svc = get_cadence_service()
    _, e, acc, contact = await _enrollable(ts, NOW, steps=[{"delay_days": 0, "angle": "intro"}])
    # The prospect replied (a positive outcome). The next tick must stop, not send.
    await get_outcome_service().record(
        ts, stage="replied", account_id=acc.id, contact_id=contact.id
    )
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 1
    await ts.refresh(e)
    assert e.status == ENROLL_STOPPED
    assert e.stop_reason == STOP_REPLIED
    assert await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id) == []


async def test_cadence_stops_on_undeliverable(ts):
    svc = get_cadence_service()
    # A malformed address verifies as invalid -> the send policy holds it as undeliverable,
    # and because every future touch hits the same dead address, the enrollment stops.
    _, e, _, _ = await _enrollable(
        ts, NOW, steps=[{"delay_days": 0, "angle": "intro"}, {"delay_days": 2, "angle": "bump"}],
        email="deadinbox",
    )
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 1
    await ts.refresh(e)
    assert e.status == ENROLL_STOPPED
    assert e.stop_reason == STOP_UNDELIVERABLE
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    assert [t.status for t in touches] == [TOUCH_SKIPPED]


async def test_cadence_pause_and_resume(ts):
    svc = get_cadence_service()
    _, e, _, _ = await _enrollable(ts, NOW, steps=[{"delay_days": 0, "angle": "intro"}])
    await svc.pause(ts, e)
    assert e.status == ENROLL_PAUSED
    # Paused enrollments are not claimed.
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 0

    later = NOW + timedelta(days=1)
    await svc.resume(ts, e, now=later)
    assert e.status == ENROLL_ACTIVE
    assert await svc.advance_due_for_tenant(ts, now=later, limit=100) == 1
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    assert [t.status for t in touches] == [TOUCH_SENT]


async def test_cadence_stops_on_duration_cap(ts):
    svc = get_cadence_service()
    # Default cap is 30 days; a tick past it stops the enrollment before any further send.
    _, e, _, _ = await _enrollable(ts, NOW, steps=[{"delay_days": 0, "angle": "intro"}])
    past_cap = NOW + timedelta(days=31)
    assert await svc.advance_due_for_tenant(ts, now=past_cap, limit=100) == 1
    await ts.refresh(e)
    assert e.status == ENROLL_STOPPED
    assert e.stop_reason == STOP_MAX_TOUCHES
    assert await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id) == []


async def test_manual_stop_is_terminal(ts):
    svc = get_cadence_service()
    _, e, _, _ = await _enrollable(ts, NOW, steps=[{"delay_days": 0, "angle": "intro"}])
    await svc.stop(ts, e)
    assert e.status == ENROLL_STOPPED
    assert e.stop_reason == STOP_MANUAL
    # A stopped enrollment is never claimed again.
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 0
