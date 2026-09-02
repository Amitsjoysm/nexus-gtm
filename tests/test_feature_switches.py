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


# ---- the engine hook ---------------------------------------------------------------------------
#
# The switch is applied inside `resolve_entitlement`, which is the ONE place all three enforcement
# points already agree on: the nav reads it through `GET /billing/entitlements`, the route guard
# reads the same response, and every endpoint reads it through `check_and_meter`. Hooking anywhere
# else would mean hooking three times and keeping them in agreement.

async def _seed_workspace(plan_id: str = "launch"):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from tests.conftest import make_tenant, put_on_plan

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant()
    await put_on_plan(tid, plan_id)
    return tid


async def _set_switch(capability_id: str, state: str, message: str = ""):
    from nexus.core.db import get_sessionmaker
    from nexus.features.switches import invalidate
    from nexus.models.feature_switch import FeatureSwitch

    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id=capability_id, state=state, message=message))
        await s.commit()
    invalidate()


async def _resolve(tenant_id: str, capability_id: str):
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    async with get_sessionmaker()() as s:
        return await resolve_entitlement(TenantSession(s, tenant_id), capability_id)


async def test_a_switch_disables_the_capability(fresh_db):
    from nexus.features.switches import invalidate

    invalidate()
    tid = await _seed_workspace()
    assert (await _resolve(tid, "module.calling")).mode != "disabled"

    await _set_switch("module.calling", "maintenance", "Back at 14:00 UTC")
    ent = await _resolve(tid, "module.calling")
    assert ent.mode == "disabled"
    assert ent.source == "feature_switch"


async def test_a_switch_beats_an_unlimited_plan(fresh_db):
    """THE placement requirement, and the reason the hook sits where it does.

    `resolve_entitlement` returns early for `unlimited`/`internal`/`partner` plan classes — those
    bypass module gates by definition. A switch checked after that short-circuit would silently not
    apply to `legacy-unlimited`, which is EVERY pre-billing tenant. Taking a broken feature offline
    has to mean everybody, or it has not been taken offline.
    """
    from nexus.features.switches import invalidate

    invalidate()
    tid = await _seed_workspace("legacy-unlimited")
    assert (await _resolve(tid, "module.calling")).mode == "unlimited", "expected the bypass"

    await _set_switch("module.calling", "disabled")
    ent = await _resolve(tid, "module.calling")
    assert ent.mode == "disabled", "an unlimited plan escaped the switch"
    assert ent.source == "feature_switch"


async def test_a_switch_applies_to_a_tenant_with_no_subscription(fresh_db):
    """The other early return. "No subscription -> allow" is a deliberate regression guard, but it
    must not become a way to keep using a feature the platform has taken down."""
    from nexus.billing.catalog import sync_catalog
    from nexus.features.switches import invalidate
    from tests.conftest import make_tenant

    await sync_catalog()
    invalidate()
    tid = await make_tenant()
    await _set_switch("module.calling", "coming_soon")
    assert (await _resolve(tid, "module.calling")).mode == "disabled"


async def test_an_enabled_switch_changes_nothing(fresh_db):
    """A row saying `enabled` must be indistinguishable from no row — otherwise re-enabling a
    feature would leave it in a subtly different state from never having switched it off."""
    from nexus.features.switches import invalidate

    invalidate()
    tid = await _seed_workspace()
    before = await _resolve(tid, "module.calling")
    await _set_switch("module.calling", "enabled")
    after = await _resolve(tid, "module.calling")
    assert (after.mode, after.source) == (before.mode, before.source)


async def test_a_switch_on_one_capability_does_not_touch_another(fresh_db):
    from nexus.features.switches import invalidate

    invalidate()
    tid = await _seed_workspace()
    await _set_switch("module.calling", "disabled")
    assert (await _resolve(tid, "module.campaigns")).mode != "disabled"


async def test_a_switched_off_module_takes_its_dependents_with_it(fresh_db):
    """Endpoint coverage comes free from `depends_on`, and this is the assertion that says so.

    Disabling `module.agents` has to stop the orchestration endpoints, not merely hide the menu
    item — otherwise "disable the feature" means "hide the link", and the API stays wide open to
    anyone with the URL.
    """
    from nexus.features.switches import invalidate

    invalidate()
    tid = await _seed_workspace()
    assert (await _resolve(tid, "ai.chat_turn")).mode != "disabled"

    await _set_switch("module.agents", "maintenance", "Upgrading the agent runtime")
    ent = await _resolve(tid, "ai.chat_turn")
    assert ent.mode == "disabled", "an endpoint behind a switched-off module was still allowed"


async def test_a_switch_never_raises_into_the_engine(fresh_db, monkeypatch):
    """The engine's contract is that it never breaks a call. A switch lookup is inside it now."""
    from nexus.billing import entitlements as ent_mod
    from nexus.features import switches

    async def boom(*a, **k):
        raise RuntimeError("switch table gone")

    tid = await _seed_workspace()
    monkeypatch.setattr(ent_mod, "switch_for", boom, raising=False)
    switches.invalidate()
    assert (await _resolve(tid, "module.calling")).mode != "disabled"


# ---- enforcement is independent of billing enforcement -----------------------------------------

async def test_a_switch_blocks_even_in_shadow_mode(fresh_db, monkeypatch):
    """THE production requirement, and the one that decides whether this feature works at all.

    `NEXUS_BILLING_ENFORCEMENT` defaults to `shadow`, which resolves every entitlement and then
    ALLOWS anyway. Shadow is a statement about BILLING rollout — "we are not yet refusing people
    over money" — and a feature switch is not about money. "Calling is broken, take it offline" and
    "we have not started enforcing quotas" are unrelated decisions.

    Had the switch ridden on `billing_enforcement`, the control would have done nothing on the
    default deployment: the superadmin flips it, the panel says disabled, and every customer keeps
    using the feature. That is the exact "configured and doing nothing" failure this codebase has
    diagnosed repeatedly — the inert telephony provider, the personalization provider that always
    returned the stub, the monitoring rules pointed at a service that did not exist.
    """
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession
    from nexus.features.switches import invalidate

    invalidate()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    tid = await _seed_workspace()

    async with get_sessionmaker()() as s:
        ts = TenantSession(s, tid)
        assert (await check_and_meter(ts, capability_id="ai.chat_turn",
                                      idempotency_key="a")).allowed is True

    await _set_switch("module.agents", "maintenance", "Upgrading the agent runtime")

    async with get_sessionmaker()() as s:
        res = await check_and_meter(TenantSession(s, tid), capability_id="ai.chat_turn",
                                    idempotency_key="b")
    assert res.allowed is False, "a switched-off feature stayed usable in shadow mode"
    assert res.reason == "feature_switch"


async def test_the_kill_switch_still_disables_a_switch(fresh_db, monkeypatch):
    """`NEXUS_BILLING_ENFORCEMENT=off` is documented as a FULL kill switch for the billing engine.
    It has to stay one: if the engine is misbehaving in production, "turn it all off" must be a
    complete answer, not one that leaves some blocks in place for an operator to hunt down.
    """
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession
    from nexus.features.switches import invalidate

    invalidate()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid = await _seed_workspace()
    await _set_switch("module.agents", "disabled")

    async with get_sessionmaker()() as s:
        res = await check_and_meter(TenantSession(s, tid), capability_id="ai.chat_turn",
                                    idempotency_key="a")
    assert res.allowed is True


async def test_a_switched_off_call_is_not_metered(fresh_db, monkeypatch):
    """A refused call did no work, so charging for it would bill a customer for our own outage."""
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession
    from nexus.features.switches import invalidate

    invalidate()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    tid = await _seed_workspace()
    async with get_sessionmaker()() as s:
        ts = TenantSession(s, tid)
        await grant_credits(ts, 1000, reason="x", idempotency_key="g")
        await s.commit()

    await _set_switch("module.agents", "maintenance")

    async with get_sessionmaker()() as s:
        ts = TenantSession(s, tid)
        await check_and_meter(ts, capability_id="ai.chat_turn", idempotency_key="a")
        await s.commit()
        assert await balance(ts) == 1000


async def test_a_switched_off_module_stops_its_dependents_on_an_unlimited_plan(fresh_db):
    """The escape the first version shipped with, found by running it against the deployment.

    `resolve_entitlement` returns early for `unlimited`/`internal`/`partner` plan classes, and that
    return is BEFORE `_apply_dependencies`. So a switch on `module.agents` disabled the module
    itself — the direct check at the top catches that — while every capability that merely
    DEPENDS on it resolved `mode=unlimited, source=plan_class` and ran normally.

    Measured on the local deployment: `module.agents` switched to maintenance, the entitlements
    endpoint correctly reported `locked=true`, the sidebar hid the page, and
    `POST /api/orchestration/runs` returned 201 ten times out of ten and billed for every one.

    `legacy-unlimited` is that plan class, and it is EVERY pre-billing tenant — so the population
    this escaped for is exactly the one a platform switch most needs to reach.

    `test_a_switch_beats_an_unlimited_plan` did not catch it because it asserts on the switched
    capability itself, which the direct check handles. This one asserts on a dependent.
    """
    from nexus.features.switches import invalidate

    invalidate()
    tid = await _seed_workspace("legacy-unlimited")
    assert (await _resolve(tid, "ai.chat_turn")).mode == "unlimited", "expected the bypass"

    await _set_switch("module.agents", "maintenance", "Upgrading the agent runtime")
    ent = await _resolve(tid, "ai.chat_turn")
    assert ent.mode == "disabled", (
        f"an unlimited plan escaped a switch on its module: mode={ent.mode} source={ent.source}"
    )
    assert ent.source == "feature_switch"
    assert ent.switch_message == "Upgrading the agent runtime"


async def test_a_switched_off_module_stops_its_dependents_with_no_subscription(fresh_db):
    """The other early return, for the same reason. "No subscription -> allow" is a deliberate
    regression guard and must not become a way to keep using a feature the platform took down."""
    from nexus.billing.catalog import sync_catalog
    from nexus.features.switches import invalidate
    from tests.conftest import make_tenant

    await sync_catalog()
    invalidate()
    tid = await make_tenant()
    await _set_switch("module.agents", "disabled")
    assert (await _resolve(tid, "ai.chat_turn")).mode == "disabled"


async def test_a_suspended_workspace_still_sees_the_switch_reason(fresh_db):
    """A suspended subscription also returns early. It resolves to disabled either way, so nothing
    is unlocked — but the customer is told the wrong thing: "your workspace is paused" when the
    truth is that we took the feature down for everyone."""
    from nexus.features.switches import invalidate
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession
    from nexus.models.billing import BillingSubscription

    invalidate()
    tid = await _seed_workspace()
    async with get_sessionmaker()() as s:
        ts = TenantSession(s, tid)
        sub = (await ts.list(BillingSubscription, limit=1))[0]
        sub.status = "suspended"
        await s.commit()

    await _set_switch("module.agents", "maintenance", "Upgrading the agent runtime")
    ent = await _resolve(tid, "ai.chat_turn")
    assert ent.mode == "disabled"
    assert ent.source == "feature_switch", f"reported as {ent.source}, not as the switch"
