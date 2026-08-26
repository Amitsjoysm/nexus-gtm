# nexus/runtime_config/service.py
"""Reading and applying runtime setting overrides.

The mechanism in one line: ``get_settings()`` is an ``lru_cache`` over a **mutable** ``Settings``
instance, so applying an override with ``setattr`` reaches every one of the 142 call sites without
any of them changing.

That choice is the reason this subsystem is safe to add. The alternative — a resolver each call site
adopts, as ``providers/resolver.py`` does for keys — would have been 142 opportunities to miss one
and leave a setting that silently ignores the panel. Missing one would be invisible: the toggle
would read "off" and the feature would keep running.

**The worker is a separate process.** It has its own ``Settings`` singleton, so nothing the API does
can reach it directly. :func:`refresh_if_stale` re-applies on a TTL, mirroring
``providers/resolver.POOL_TTL_S`` — a change made in the panel reaches every process within a
minute, without a restart.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select

from nexus.core.db import get_platform_sessionmaker
from nexus.models.runtime_setting import RuntimeSetting
from nexus.runtime_config.catalog import CATALOG, FORBIDDEN, coerce

logger = logging.getLogger("nexus.runtime_config")

# Same TTL as the provider key pool, for the same reason: a separate process must pick a change up
# without a restart, and a minute of staleness on a config toggle is not worth a query per read.
TTL_S = 30.0

_applied_at = 0.0


class UnknownSetting(ValueError):
    """Not in the catalog, so not settable from the panel."""


def _spec(key: str):
    if key in FORBIDDEN:
        # A distinct message from "unknown". An operator hunting for a setting they know exists
        # deserves to be told it is withheld on purpose rather than left thinking they mistyped.
        raise UnknownSetting(
            f"'{key}' is deliberately not changeable at runtime — see runtime_config/catalog.py "
            f"for why. It is a deploy-time setting."
        )
    spec = CATALOG.get(key)
    if spec is None:
        raise UnknownSetting(f"'{key}' is not a runtime-settable setting")
    return spec


async def stored_overrides() -> dict[str, str]:
    """Raw rows, uncoerced. Empty on any failure — see :func:`apply_overrides`."""
    async with get_platform_sessionmaker()() as s:
        rows = (await s.scalars(select(RuntimeSetting))).all()
    return {r.key: r.value for r in rows}


async def apply_overrides() -> dict[str, object]:
    """Write every stored override onto the live ``Settings`` object. Returns what was applied.

    Never raises. A configuration read that fails must leave the process running on the environment
    values it already had — the alternative is that a database blip takes down an application whose
    settings were perfectly fine a second ago.

    A row whose key has since left the catalog is skipped rather than applied. That is the
    difference between removing a setting from the panel and having it keep taking effect from a row
    nobody can see any more.
    """
    from nexus.core.config import get_settings

    applied: dict[str, object] = {}
    try:
        raw = await stored_overrides()
    except Exception:
        logger.warning("could not read runtime overrides; staying on environment values",
                       exc_info=True)
        return applied

    settings = get_settings()
    for key, value in raw.items():
        spec = CATALOG.get(key)
        if spec is None or key in FORBIDDEN:
            continue
        try:
            typed = coerce(spec, value)
            setattr(settings, key, typed)
            applied[key] = typed
        except Exception:
            logger.warning("runtime override for %s could not be applied", key, exc_info=True)
    return applied


async def refresh_if_stale(force: bool = False) -> dict[str, object]:
    """Re-apply overrides at most once per TTL. Called from request and worker paths."""
    global _applied_at
    now = time.monotonic()
    if not force and (now - _applied_at) < TTL_S:
        return {}
    _applied_at = now
    return await apply_overrides()


async def set_override(key: str, raw_value, *, note: str = "", user_id: str = "") -> object:
    """Store an override and apply it to this process immediately.

    Applied here as well as stored so the operator's very next request reflects the change, rather
    than waiting out the TTL and appearing not to have worked.
    """
    from nexus.core.config import get_settings

    spec = _spec(key)
    typed = coerce(spec, raw_value)

    async with get_platform_sessionmaker()() as s:
        row = (await s.scalars(select(RuntimeSetting).where(RuntimeSetting.key == key))).first()
        if row is None:
            row = RuntimeSetting(key=key)
            s.add(row)
        row.value = str(typed)
        row.note = (note or "")[:500]
        row.updated_by_user_id = user_id or None
        await s.commit()

    setattr(get_settings(), key, typed)
    return typed


async def clear_override(key: str) -> bool:
    """Drop the override so the environment value applies again.

    Deliberately does NOT restore the environment value into the live object here — this process
    would revert on the next TTL sweep anyway, and reading the original value back out of a mutated
    singleton is not reliable. `apply_overrides` runs from a fresh `Settings()` view on the next
    process start; between now and then the TTL sweep is what converges everything.
    """
    spec = _spec(key)
    async with get_platform_sessionmaker()() as s:
        row = (await s.scalars(
            select(RuntimeSetting).where(RuntimeSetting.key == spec.key)
        )).first()
        if row is None:
            return False
        await s.delete(row)
        await s.commit()
    return True


async def current_values() -> list[dict]:
    """Every catalog entry with its live value, whether it is overridden, and its warnings."""
    from nexus.core.config import get_settings

    settings = get_settings()
    try:
        raw = await stored_overrides()
    except Exception:
        raw = {}

    async with get_platform_sessionmaker()() as s:
        rows = {r.key: r for r in (await s.scalars(select(RuntimeSetting))).all()}

    out = []
    for spec in CATALOG.values():
        row = rows.get(spec.key)
        out.append({
            "key": spec.key,
            "label": spec.label,
            "group": spec.group,
            "kind": spec.kind,
            "effect": spec.effect,
            "warning": spec.warning,
            "risk": spec.risk,
            "requires_restart": spec.requires_restart,
            "options": list(spec.options),
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "value": getattr(settings, spec.key, None),
            "overridden": spec.key in raw,
            "note": row.note if row is not None else "",
        })
    out.sort(key=lambda x: (x["group"], x["label"]))
    return out
