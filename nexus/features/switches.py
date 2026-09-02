"""Resolve a feature switch, cached for 30 seconds.

The TTL is the requirement, not an optimisation: the worker is a separate container and nothing the
API does can invalidate its memory, so without one a switch would need a redeploy to take effect —
which is exactly the thing this feature exists to avoid. Same idiom as `providers/resolver.py` and
`runtime_config/service.py`.

EVERYTHING HERE FAILS OPEN. A switch is a restriction; failing to read one means applying no
restriction. An unreadable table, an unknown state, a typo — all resolve to `enabled`, matching the
entitlement engine's own unknown-means-allow bias. A database blip taking the whole product offline
is a far worse failure than a switch that briefly does not apply.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from nexus.core.db import get_sessionmaker
from nexus.models.feature_switch import SWITCH_STATES, FeatureSwitch

logger = logging.getLogger("nexus.features.switches")

TTL_S = 30.0

_cache: dict[str, "Switch"] | None = None
_loaded_at = 0.0


@dataclass(frozen=True, slots=True)
class Switch:
    capability_id: str
    state: str = "enabled"
    message: str = ""

    @property
    def blocks(self) -> bool:
        """Whether this switch takes the feature away. Only `enabled` does not."""
        return self.state != "enabled"


def invalidate() -> None:
    """Drop the cache. Immediate for THIS process; others wait out the TTL."""
    global _cache, _loaded_at
    _cache, _loaded_at = None, 0.0


async def _load() -> dict[str, Switch]:
    async with get_sessionmaker()() as session:
        rows = (await session.scalars(select(FeatureSwitch))).all()
    out: dict[str, Switch] = {}
    for r in rows:
        # An unrecognised state resolves to enabled rather than blocking. A value written by a
        # newer release during a rolling deploy, or a typo, must not take a working feature down.
        state = r.state if r.state in SWITCH_STATES else "enabled"
        out[r.capability_id] = Switch(r.capability_id, state, r.message or "")
    return out


async def all_switches() -> dict[str, Switch]:
    """Every stored switch, TTL-cached. ``{}`` on any failure."""
    global _cache, _loaded_at
    now = time.monotonic()
    if _cache is not None and (now - _loaded_at) < TTL_S:
        return _cache
    try:
        loaded = await _load()
    except Exception:
        logger.warning("feature switch load failed; treating everything as enabled", exc_info=True)
        return {}
    _cache, _loaded_at = loaded, now
    return loaded


async def switch_for(capability_id: str) -> Switch:
    """The switch for one capability. ``enabled`` when there is no row."""
    return (await all_switches()).get(capability_id) or Switch(capability_id)
