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


def _set_allowlist(value) -> None:
    from nexus.api.deps_ip import set_allowlist

    set_allowlist(str(value))


def _get_allowlist():
    from nexus.api.deps_ip import current_allowlist

    return current_allowlist()


def _validate_allowlist(value) -> None:
    from nexus.api.deps_ip import parse_allowlist

    parse_allowlist(str(value))


# Settings whose value does NOT live on the `Settings` object. Pydantic refuses an attribute it has
# not declared, so anything `config.py` does not know about needs somewhere else to live and its own
# reader. Keep this small: a growing list means the override mechanism is being worked around
# rather than used.
_EXTERNAL_SINKS = {"admin_ip_allowlist": _set_allowlist}
_EXTERNAL_READERS = {"admin_ip_allowlist": _get_allowlist}
# Run BEFORE the row is written. `coerce` only checks the declared kind, and for a string setting
# that is no check at all — the real constraints live in the sink. Without this the row commits and
# then the sink rejects it, leaving a stored override the panel reports as active and
# `apply_overrides` silently skips forever.
_EXTERNAL_VALIDATORS = {"admin_ip_allowlist": _validate_allowlist}


#: Keys this process applied from a stored row. One that later has NO row was cleared — possibly by
#: ANOTHER process — and is put back to its environment value here too; see `apply_overrides`.
_overridden_here: set[str] = set()


def _reset_providers() -> None:
    """Drop every cached provider so the next use rebuilds from the settings just changed.

    The LLM chain, email verifier, research provider, account enricher, registry and agent runtime
    are each built once and cached, and several capture another at construction — the registry holds
    a research provider and a contact searcher, each holding an LLM. Dropping them together is the
    only way a change to any one input reaches all of them. Rebuilding is lazy and cheap; a reset
    that fails is logged and never raised, because a settings change must not take a process down.
    """
    from nexus.agents.llm import set_llm_provider
    from nexus.agents.runtime import reset_agent_runtime
    from nexus.enrichment.account import set_account_enricher
    from nexus.integrations.registry import set_registry
    from nexus.research.provider import set_research_provider
    from nexus.verification.provider import set_email_verifier

    for reset in (
        lambda: set_llm_provider(None),
        reset_agent_runtime,
        lambda: set_account_enricher(None),
        lambda: set_registry(None),
        lambda: set_research_provider(None),
        lambda: set_email_verifier(None),
    ):
        try:
            reset()
        except Exception:
            logger.warning("could not reset a cached provider", exc_info=True)


#: Settings read ONCE into a cached object. Changing one must drop the cache, or the panel reports it
#: "in effect" while nothing changes until a restart — the trap this catalog exists to prevent.
#: `signal_search_provider` is absent on purpose: the dork source re-reads it on every crawl.
_ON_CHANGE = {
    key: _reset_providers
    for key in ("llm_provider", "contact_search_sources", "research_provider",
                "email_verify_provider")
}


def _apply_value(settings, key: str, typed) -> None:
    """Set one Settings field, firing its on-change hook only when the value actually moved.

    The sweep re-applies every override every 30s; rebuilding on each pass would throw away the
    registry's cache and the LLM chain's sticky key rotation for nothing.
    """
    old = getattr(settings, key, None)
    setattr(settings, key, typed)
    if old != typed:
        hook = _ON_CHANGE.get(key)
        if hook is not None:
            hook()


def _environment_value(key: str):
    """What this setting is WITHOUT any override: a fresh read of the environment.

    A fresh `Settings()` rather than the live object, which is the mutated singleton and no longer
    remembers what the environment said.
    """
    from nexus.core.config import Settings

    return getattr(Settings(), key)


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
            if key in _EXTERNAL_SINKS:
                # Not a `Settings` field. Pydantic refuses an attribute it does not declare, so a
                # setting `config.py` does not know about needs its own home — see
                # `api/deps_ip.py` for why the IP allowlist is one of these.
                _EXTERNAL_SINKS[key](typed)
            else:
                _apply_value(settings, key, typed)
                _overridden_here.add(key)
            applied[key] = typed
        except Exception:
            logger.warning("runtime override for %s could not be applied", key, exc_info=True)

    # Overrides CLEARED since this process applied them — by the panel in another process, most
    # often: `clear_override` runs in one API worker, and the worker and the other API process only
    # learn of it here. They used to keep the old value until a restart, whatever the comment on
    # `clear_override` said about the sweep converging it. Only keys this process itself overrode
    # are touched, so a value set any other way (a test's monkeypatch, an operator's shell) is left
    # alone.
    for key in list(_overridden_here - set(raw)):
        _overridden_here.discard(key)
        if key not in CATALOG or key in _EXTERNAL_SINKS:
            continue
        try:
            _apply_value(settings, key, _environment_value(key))
        except Exception:
            logger.warning("could not restore %s to its environment value", key, exc_info=True)
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
    # Validate before writing. A value the sink will reject must never reach the table, or the
    # panel shows an override that is stored, reported as set, and applied by nothing.
    validator = _EXTERNAL_VALIDATORS.get(key)
    if validator is not None:
        validator(typed)

    async with get_platform_sessionmaker()() as s:
        row = (await s.scalars(select(RuntimeSetting).where(RuntimeSetting.key == key))).first()
        if row is None:
            row = RuntimeSetting(key=key)
            s.add(row)
        row.value = str(typed)
        row.note = (note or "")[:500]
        row.updated_by_user_id = user_id or None
        await s.commit()

    if key in _EXTERNAL_SINKS:
        _EXTERNAL_SINKS[key](typed)
    else:
        # Through the on-change hook, so a provider setting rebuilds its cached provider now rather
        # than reading "in effect" while the old one keeps running until a restart.
        _apply_value(get_settings(), key, typed)
        _overridden_here.add(key)
    return typed


async def clear_override(key: str) -> bool:
    """Drop the override so the environment value applies again — in this process NOW, and in
    every other process on its next sweep.

    This used to leave the live value alone, on the claim that "the TTL sweep is what converges
    everything". It did not: the sweep only ever APPLIED stored rows, so a cleared override stayed in
    force everywhere until a restart. The environment value is read from a fresh `Settings()` — not
    from the live object, which is the mutated singleton and no longer remembers it — and the other
    processes restore it in `apply_overrides`.
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
    _overridden_here.discard(spec.key)
    if spec.key in _EXTERNAL_SINKS:
        # An external sink has no environment value to fall back to, so it is reset explicitly.
        # Leaving a stale allowlist installed after clearing it would keep the panel locked to an
        # address the operator believes they just removed.
        _EXTERNAL_SINKS[spec.key]("")
    else:
        try:
            from nexus.core.config import get_settings

            _apply_value(get_settings(), spec.key, _environment_value(spec.key))
        except Exception:
            logger.warning("could not restore %s to its environment value", spec.key, exc_info=True)
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
        live = (
            _EXTERNAL_READERS[spec.key]()
            if spec.key in _EXTERNAL_READERS
            else getattr(settings, spec.key, None)
        )
        stored = raw.get(spec.key)
        # "Saved" and "in force" are different facts. A restart-only setting is stored and pending,
        # and a panel that showed only the first is how an operator concludes a feature is on when
        # it is not.
        in_effect = True
        if stored is not None:
            try:
                in_effect = coerce(spec, stored) == live
            except Exception:
                in_effect = False
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
            "value": live,
            "overridden": spec.key in raw,
            "in_effect": in_effect,
            "note": row.note if row is not None else "",
        })
    out.sort(key=lambda x: (x["group"], x["label"]))
    return out
