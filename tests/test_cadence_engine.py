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
