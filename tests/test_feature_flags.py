"""Feature-flag evaluation on entitlements (M24).

`BillingPlanEntitlement.feature_flag` has existed since the schema was written, is editable through
the admin API, and is copied by the custom-plan builder — and was **never read**. The same dead
-config class `burst_limit` and `depends_on` were in before they were wired up, and worse than an
absent setting: an operator can change it, nothing happens, and they may build a rollout on it.

The load-bearing rule below is that an **unknown flag is ON**. A plan entitlement naming a flag
nobody created must not silently disable a capability the customer is paying for.
"""
from __future__ import annotations

from nexus.billing.flags import flag_enabled
from tests.conftest import make_tenant, tenant_session


async def _seed(plan_id: str = "growth"):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        await ts.flush()
    return tid


async def _flag(name: str, **kw):
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingFeatureFlag

    async with get_sessionmaker()() as s:
        s.add(BillingFeatureFlag(id=name, **kw))
        await s.commit()


# ---- evaluation ---------------------------------------------------------------------------------

async def test_no_flag_on_the_entitlement_is_always_enabled():
    tid = await _seed()
    async with tenant_session(tid) as ts:
        assert await flag_enabled(ts, None) is True
        assert await flag_enabled(ts, "") is True


async def test_an_unregistered_flag_is_enabled():
    """The rule that matters. A flag named on an entitlement but never created must not disable a
    capability the customer pays for — the same bias as unknown capability → allow."""
    tid = await _seed()
    async with tenant_session(tid) as ts:
        assert await flag_enabled(ts, "never.created") is True


async def test_a_disabled_flag_disables():
    tid = await _seed()
    await _flag("beta.thing", enabled=False)
    async with tenant_session(tid) as ts:
        assert await flag_enabled(ts, "beta.thing") is False


async def test_a_tenant_override_beats_the_default():
    """The narrowest scope wins — that is what an override is for."""
    tid = await _seed()
    await _flag("beta.thing", enabled=False, overrides={f"tenant:{tid}": True})
    async with tenant_session(tid) as ts:
        assert await flag_enabled(ts, "beta.thing") is True


async def test_a_tenant_override_can_also_switch_something_off():
    """A temporary disable for one workspace during an incident."""
    tid = await _seed()
    await _flag("beta.thing", enabled=True, overrides={f"tenant:{tid}": False})
    async with tenant_session(tid) as ts:
        assert await flag_enabled(ts, "beta.thing") is False


async def test_an_environment_override_beats_the_default():
    """Off in prod, on in staging — the normal shape of a staged rollout."""
    tid = await _seed()
    await _flag("beta.thing", enabled=True, overrides={"env:prod": False})
    async with tenant_session(tid) as ts:
        assert await flag_enabled(ts, "beta.thing", env="prod") is False
        assert await flag_enabled(ts, "beta.thing", env="staging") is True


async def test_a_tenant_override_beats_an_environment_override():
    """Narrowest first: a workspace granted beta access keeps it even where the env says off."""
    tid = await _seed()
    await _flag(
        "beta.thing", enabled=True,
        overrides={"env:prod": False, f"tenant:{tid}": True},
    )
    async with tenant_session(tid) as ts:
        assert await flag_enabled(ts, "beta.thing", env="prod") is True


async def test_evaluation_failure_resolves_to_enabled(monkeypatch):
    """A flag that failed closed on a database blip would disable a paid feature mid-incident."""
    tid = await _seed()

    class Boom:
        tenant_id = "t"

        class session:
            @staticmethod
            async def get(*_a, **_kw):
                raise RuntimeError("db down")

    assert await flag_enabled(Boom(), "beta.thing") is True
    assert tid


# ---- it actually gates the entitlement --------------------------------------------------------

async def test_a_disabled_flag_disables_the_capability():
    """The point of the milestone: the field is now read, and the resolved entitlement says why."""
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlanEntitlement

    tid = await _seed("free")
    await _flag("beta.drafts", enabled=False)
    async with get_sessionmaker()() as s:
        ent = (await s.scalars(
            __import__("sqlalchemy").select(BillingPlanEntitlement).where(
                BillingPlanEntitlement.plan_id == "free",
                BillingPlanEntitlement.capability_id == "ai.email_draft",
            )
        )).first()
        ent.feature_flag = "beta.drafts"
        await s.commit()

    async with tenant_session(tid) as ts:
        resolved = await resolve_entitlement(ts, "ai.email_draft")
    assert resolved.mode == "disabled"
    # The source explains it, so the 402 payload and the admin debug view need no special case.
    assert resolved.source == "feature_flag"


async def test_an_enabled_flag_leaves_the_entitlement_alone():
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlanEntitlement

    tid = await _seed("free")
    await _flag("beta.on", enabled=True)
    async with get_sessionmaker()() as s:
        ent = (await s.scalars(
            __import__("sqlalchemy").select(BillingPlanEntitlement).where(
                BillingPlanEntitlement.plan_id == "free",
                BillingPlanEntitlement.capability_id == "ai.email_draft",
            )
        )).first()
        ent.feature_flag = "beta.on"
        await s.commit()

    async with tenant_session(tid) as ts:
        resolved = await resolve_entitlement(ts, "ai.email_draft")
    assert resolved.mode == "metered"       # the Free plan's own setting, untouched
    assert resolved.source == "plan"


async def test_the_flag_registry_is_platform_global():
    """No tenant_id, so apply_rls.py leaves it alone — same posture as billing_capabilities."""
    from nexus.models.billing import BillingFeatureFlag

    assert "tenant_id" not in BillingFeatureFlag.__table__.columns
