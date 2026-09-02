# tests/test_feature_switches.py
"""A superadmin can take a feature offline with a message.

Keyed on the EXISTING `module.*` capability ids rather than a new page registry, because those ids
already drive all three enforcement points — the nav item (`nav.tsx` carries `capability`), the
route (`RequireCapability`) and the endpoints behind them (`depends_on` in the capability catalog).
A separate registry would be a fourth source of truth to keep in agreement, and the first thing to
drift would be which pages it covers.

THE ABSENCE OF A ROW MEANS ENABLED. That is what makes this additive: a deployment with no switches
behaves exactly as it did before the table existed.

Everything here FAILS OPEN. A switch is a restriction, so failing to read one means applying no
restriction — an unreadable table or an unknown state resolves to `enabled`, matching the
entitlement engine's own unknown-means-allow bias. A database blip taking the whole product offline
is a far worse failure than a switch that briefly does not apply.
"""
from __future__ import annotations


# ---- the row -----------------------------------------------------------------------------------

async def test_a_switch_row_defaults_to_enabled(fresh_db):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.feature_switch import FeatureSwitch

    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id="module.calling"))
        await s.commit()
        row = (await s.scalars(select(FeatureSwitch))).one()
        assert row.state == "enabled"
        assert row.message == ""


async def test_the_four_states_round_trip(fresh_db):
    """Four states, not a boolean. "We turned this off", "this is not built yet" and "this is
    broken right now" are three different conversations, and one flag makes a support agent guess
    which one they are having."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.feature_switch import SWITCH_STATES, FeatureSwitch

    assert SWITCH_STATES == ("enabled", "disabled", "coming_soon", "maintenance")
    async with get_sessionmaker()() as s:
        for i, state in enumerate(SWITCH_STATES):
            s.add(FeatureSwitch(capability_id=f"module.x{i}", state=state, message=f"m{i}"))
        await s.commit()
        rows = {r.capability_id: r for r in (await s.scalars(select(FeatureSwitch))).all()}
    assert rows["module.x1"].state == "disabled"
    assert rows["module.x2"].message == "m2"


# ---- resolution --------------------------------------------------------------------------------

async def test_no_row_means_enabled(fresh_db):
    """THE compatibility line. A deployment with no switches behaves exactly as before."""
    from nexus.features.switches import invalidate, switch_for

    invalidate()
    assert (await switch_for("module.calling")).state == "enabled"
    assert (await switch_for("module.calling")).blocks is False


async def test_a_stored_switch_is_returned(fresh_db):
    from nexus.core.db import get_sessionmaker
    from nexus.features.switches import invalidate, switch_for
    from nexus.models.feature_switch import FeatureSwitch

    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id="module.calling", state="maintenance",
                            message="Back at 14:00 UTC"))
        await s.commit()
    invalidate()

    got = await switch_for("module.calling")
    assert got.state == "maintenance"
    assert got.message == "Back at 14:00 UTC"
    assert got.blocks is True


async def test_an_enabled_row_does_not_block(fresh_db):
    """A row saying enabled is not a restriction — it is the absence of one."""
    from nexus.core.db import get_sessionmaker
    from nexus.features.switches import invalidate, switch_for
    from nexus.models.feature_switch import FeatureSwitch

    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id="module.calling", state="enabled"))
        await s.commit()
    invalidate()
    assert (await switch_for("module.calling")).blocks is False


async def test_an_unknown_state_resolves_to_enabled(fresh_db):
    """Fail OPEN. A typo, or a value written by a newer release during a rolling deploy, must not
    take a working feature down."""
    from nexus.core.db import get_sessionmaker
    from nexus.features.switches import invalidate, switch_for
    from nexus.models.feature_switch import FeatureSwitch

    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id="module.calling", state="banana"))
        await s.commit()
    invalidate()
    assert (await switch_for("module.calling")).state == "enabled"


async def test_a_database_failure_resolves_to_enabled(fresh_db, monkeypatch):
    """A switch lookup that fails must not take the product down with it."""
    from nexus.features import switches

    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(switches, "_load", boom)
    switches.invalidate()
    assert (await switches.switch_for("module.calling")).state == "enabled"


async def test_the_cache_is_reused_within_the_ttl(fresh_db, monkeypatch):
    """The TTL is the requirement, not an optimisation: the worker is a separate container, so
    without one a switch would need a redeploy to take effect — the thing this exists to avoid."""
    from nexus.features import switches

    switches.invalidate()
    calls = {"n": 0}
    real = switches._load

    async def counting():
        calls["n"] += 1
        return await real()

    monkeypatch.setattr(switches, "_load", counting)
    await switches.all_switches()
    await switches.all_switches()
    assert calls["n"] == 1, "the second lookup hit the database inside the TTL"
