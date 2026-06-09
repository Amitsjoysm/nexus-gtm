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
