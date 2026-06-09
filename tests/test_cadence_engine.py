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
