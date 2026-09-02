# tests/test_feature_switch_api.py
"""The switch, end to end: what the customer's client sees, and who may flip it."""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


@pytest.fixture(autouse=True)
async def _clean_switch_cache():
    from nexus.features.switches import invalidate

    invalidate()
    yield
    invalidate()


async def _seed_billing():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates

    await sync_catalog()
    await sync_plans()
    await sync_rates()


async def _set(capability_id: str, state: str, message: str = ""):
    from nexus.core.db import get_sessionmaker
    from nexus.features.switches import invalidate
    from nexus.models.feature_switch import FeatureSwitch

    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id=capability_id, state=state, message=message))
        await s.commit()
    invalidate()


async def _superadmin(client, monkeypatch, *, slug: str, email: str) -> str:
    """Same shape as the other platform-admin suites: the env allowlist is the bootstrap path and
    deliberately carries full power."""
    from nexus.core.config import get_settings

    await _seed_billing()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


# ---- what the customer's client sees -----------------------------------------------------------

async def test_entitlements_reports_the_switch_state_and_message(client):
    """The UI needs all three facts. `included=false` alone cannot distinguish "your plan lacks
    this" from "we are fixing it" — and the first sends the customer to a checkout page."""
    await _seed_billing()
    token = await signup(client, slug="fs1", email="o@fs1.com", company="FS1")
    await _set("module.calling", "maintenance", "Back at 14:00 UTC")

    r = await client.get("/api/billing/entitlements", headers=auth(token))
    assert r.status_code == 200
    row = next(m for m in r.json()["modules"] if m["capability_id"] == "module.calling")
    assert row["included"] is False
    assert row["switch_state"] == "maintenance"
    assert row["switch_message"] == "Back at 14:00 UTC"
    assert row["source"] == "feature_switch"


async def test_an_unswitched_module_reports_no_switch_state(client):
    await _seed_billing()
    token = await signup(client, slug="fs2", email="o@fs2.com", company="FS2")
    r = await client.get("/api/billing/entitlements", headers=auth(token))
    row = next(m for m in r.json()["modules"] if m["capability_id"] == "module.calling")
    assert row["switch_state"] is None


async def test_a_switch_locks_the_module_even_in_shadow_mode(client, monkeypatch):
    """`gating_active` is false on a default deployment, and the client is told never to hide a
    feature on `included` alone — that rule is what keeps shadow mode a no-op.

    A switch has to escape it, or the whole control is invisible in production. So the response
    carries a per-module `locked` that already accounts for both rules, rather than leaving the
    client to combine `gating_active`, `included` and `switch_state` and get it subtly wrong in one
    of the two places that read it.
    """
    from nexus.core.config import get_settings

    await _seed_billing()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    token = await signup(client, slug="fs3", email="o@fs3.com", company="FS3")
    await _set("module.calling", "coming_soon", "Landing next month")

    body = (await client.get("/api/billing/entitlements", headers=auth(token))).json()
    assert body["gating_active"] is False
    row = next(m for m in body["modules"] if m["capability_id"] == "module.calling")
    assert row["locked"] is True, "a switched-off module must lock even in shadow mode"


async def test_a_plan_gate_does_not_lock_in_shadow_mode(client, monkeypatch):
    """The other half of the same rule, and the regression this guards.

    A module the plan excludes must NOT lock while enforcement is shadow, because the server will
    still serve it. Hiding it would turn a rollout mode whose entire promise is "changes nothing"
    into a visible product regression.
    """
    from nexus.core.config import get_settings

    await _seed_billing()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    token = await signup(client, slug="fs4", email="o@fs4.com", company="FS4")

    body = (await client.get("/api/billing/entitlements", headers=auth(token))).json()
    excluded = [m for m in body["modules"] if not m["included"]]
    assert all(m["locked"] is False for m in excluded), (
        "shadow mode locked a module the server would still serve"
    )


async def test_a_switched_off_endpoint_refuses_with_the_message(client, monkeypatch):
    """The endpoint half. Hiding the nav item is presentation; this is the access control, and the
    customer must be told which kind of off they hit."""
    from nexus.core.config import get_settings

    await _seed_billing()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    token = await signup(client, slug="fs5", email="o@fs5.com", company="FS5")
    await _set("module.agents", "maintenance", "Upgrading the agent runtime")

    r = await client.post("/api/orchestration/runs", headers=auth(token),
                          json={"goal": "discover"})
    assert r.status_code == 402, r.text
    body = r.json()          # `main.py` returns `to_payload()` as the body, not under `detail`
    assert body["reason"] == "feature_switch"
    assert body["switch_state"] == "maintenance"
    assert body["switch_message"] == "Upgrading the agent runtime"
    # A SWITCH IS NOT AN UPSELL, and the payload must not offer one. No plan re-enables a
    # switched-off module, so a checkout link here invites the customer to pay to fix our
    # maintenance window. Omitted server-side rather than filtered in our UI, because a customer's
    # own integration reads this too.
    assert "upgrade_url" not in body, "the 402 offered a checkout link for a feature we took down"


# ---- who may flip it ---------------------------------------------------------------------------

async def test_a_tenant_user_cannot_see_the_switch_console(client):
    """404, not 403. A 403 confirms the route exists and turns any authenticated user into a
    scanner for the admin surface."""
    token = await signup(client, slug="fs6", email="o@fs6.com", company="FS6")
    assert (await client.get("/api/admin/features",
                             headers=auth(token))).status_code == 404


async def test_a_tenant_user_cannot_flip_a_switch(client):
    token = await signup(client, slug="fs7", email="o@fs7.com", company="FS7")
    r = await client.put("/api/admin/features/module.calling", headers=auth(token),
                         json={"state": "disabled", "message": "nope"})
    assert r.status_code == 404


async def test_a_platform_admin_can_list_and_flip(client, monkeypatch):
    """The console lists every `module.*` capability, switched or not — an operator opening it
    during an incident needs the whole board, not the subset somebody has already touched."""
    admin = await _superadmin(client, monkeypatch, slug="fsa1", email="b@fsa1.com")
    listing = await client.get("/api/admin/features", headers=auth(admin))
    assert listing.status_code == 200, listing.text
    ids = {row["capability_id"] for row in listing.json()["features"]}
    assert "module.calling" in ids
    assert all(row["state"] == "enabled" for row in listing.json()["features"])

    r = await client.put("/api/admin/features/module.calling", headers=auth(admin),
                         json={"state": "maintenance", "message": "Back at 14:00 UTC"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "maintenance"

    again = await client.get("/api/admin/features", headers=auth(admin))
    row = next(f for f in again.json()["features"] if f["capability_id"] == "module.calling")
    assert row["state"] == "maintenance"
    assert row["message"] == "Back at 14:00 UTC"


async def test_flipping_takes_effect_immediately_for_the_writer(client, monkeypatch):
    """The 30s TTL must not make the console feel broken. The process that writes the row drops its
    own cache, so an operator who flips a switch and reloads sees the result — the TTL exists for
    the OTHER processes (the worker, the second uvicorn worker), which have no way to be told."""
    admin = await _superadmin(client, monkeypatch, slug="fsa2", email="b@fsa2.com")
    token = await signup(client, slug="fs8", email="o@fs8.com", company="FS8")

    await client.put("/api/admin/features/module.calling", headers=auth(admin),
                     json={"state": "disabled", "message": ""})

    body = (await client.get("/api/billing/entitlements", headers=auth(token))).json()
    row = next(m for m in body["modules"] if m["capability_id"] == "module.calling")
    assert row["locked"] is True


async def test_re_enabling_clears_the_message(client, monkeypatch):
    """A stale "back at 14:00" left on a working feature is worse than no message."""
    admin = await _superadmin(client, monkeypatch, slug="fsa3", email="b@fsa3.com")
    await client.put("/api/admin/features/module.calling", headers=auth(admin),
                     json={"state": "maintenance", "message": "Back at 14:00 UTC"})
    r = await client.put("/api/admin/features/module.calling", headers=auth(admin),
                         json={"state": "enabled", "message": ""})
    assert r.status_code == 200
    assert r.json()["message"] == ""


async def test_an_unknown_state_is_refused(client, monkeypatch):
    """Coerce at the edge. The resolver treats an unrecognised state as `enabled`, so a typo
    accepted here would read as "switched off" in the console and do nothing to the product —
    which is the worst of both."""
    admin = await _superadmin(client, monkeypatch, slug="fsa4", email="b@fsa4.com")
    r = await client.put("/api/admin/features/module.calling", headers=auth(admin),
                         json={"state": "of", "message": ""})
    assert r.status_code == 422, r.text


async def test_a_switch_on_an_unknown_capability_is_refused(client, monkeypatch):
    """A switch on a capability that does not exist gates nothing and reads in the console as a
    feature that is off. Same argument as `depends_on` validation in capability authoring."""
    admin = await _superadmin(client, monkeypatch, slug="fsa5", email="b@fsa5.com")
    r = await client.put("/api/admin/features/module.nonexistent", headers=auth(admin),
                         json={"state": "disabled", "message": ""})
    assert r.status_code == 400, r.text


async def test_flipping_a_switch_is_audited(client, monkeypatch):
    """It takes a feature away from every customer at once. That is the last mutation that should
    be untraceable."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingAuditLog
    from sqlalchemy import select

    admin = await _superadmin(client, monkeypatch, slug="fsa6", email="b@fsa6.com")
    await client.put("/api/admin/features/module.calling", headers=auth(admin),
                     json={"state": "disabled", "message": "off"})

    async with get_platform_sessionmaker()() as s:
        rows = (await s.scalars(
            select(BillingAuditLog).where(BillingAuditLog.action.like("feature%"))
        )).all()
    assert rows, "flipping a feature switch wrote no audit row"


# ---- the client surface ------------------------------------------------------------------------
#
# There is no frontend test runner, so these read the source, exactly like
# `test_plan_gated_nav.py`. They pin the three decisions that are invisible at runtime until a
# customer hits them.

import pathlib

FRONTEND = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"


def _read(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


def test_the_client_gates_on_the_servers_locked_flag():
    """`isLocked` must not re-derive the rule from `gating_active` and `included`.

    The server folds both rules into `locked` — a plan gate locks only under enforcement, a
    platform switch locks always — precisely because the client has TWO readers of it (the sidebar
    and `RequireCapability`) and a hand-rolled condition in each is how they drift.
    """
    src = _read("app/EntitlementsContext.tsx")
    assert "module.locked" in src, "isLocked no longer reads the server's `locked`"


def test_a_switched_off_route_does_not_redirect_to_billing():
    """A plan gate is an upsell and billing is a real destination. A switch is OUR decision, no
    plan re-enables it, and sending someone to a checkout page to fix our maintenance window is
    the confusion the separate `switch_state` exists to prevent."""
    src = _read("App.tsx")
    assert "switchNotice" in src, "RequireCapability does not distinguish a switch from a plan gate"
    assert "FeatureUnavailable" in src, "no in-place notice for a switched-off route"
    # The plan-gate redirect must survive: removing it would strand a customer who genuinely
    # could buy their way in.
    assert '/settings/billing' in src


def test_a_switched_off_nav_item_is_shown_to_everyone():
    """`navState` hides a plan-locked item from anyone who cannot change the plan, because a
    padlock they can do nothing about is permanent clutter. A switch is the opposite: it is a
    status message, and the person who most needs it is the rep whose daily driver went quiet.
    Hiding it turns "Calls is down until 14:00" into "the app lost my Calls tab"."""
    src = _read("app/nav.tsx")
    assert "unavailable" in src, "navState has no state for a platform switch"
    idx_switch = src.index("if (switchedOff) return \"unavailable\";")
    idx_hidden = src.index("CAN_UPGRADE.includes(role)")
    assert idx_switch < idx_hidden, (
        "the switch check runs after the hide-from-reps rule, so a rep would not see the notice"
    )


def test_every_switch_state_has_customer_facing_copy():
    """Three blocking states exist BECAUSE they are three different sentences. A state with no
    wording of its own would render as whichever one came first, which is worse than having one
    state — it looks specific and is wrong."""
    src = _read("components/FeatureUnavailable.tsx")
    for state in ("coming_soon", "maintenance", "disabled"):
        assert f"{state}:" in src, f"no copy for the `{state}` state"


async def test_a_plan_gate_still_offers_the_upgrade_link(client, monkeypatch):
    """The other half. A capability the PLAN excludes is a real upsell and must keep its link —
    removing it for everyone would turn a working sales path into a dead end."""
    from nexus.billing.errors import QuotaExceeded

    payload = QuotaExceeded("ai.email_draft", reason="quota_exhausted", plan_id="free").to_payload()
    assert payload["upgrade_url"] == "/settings/billing"
    assert "switch_state" not in payload
