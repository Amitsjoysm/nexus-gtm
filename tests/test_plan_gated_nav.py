# tests/test_plan_gated_nav.py
"""`GET /billing/entitlements` — the module gates that drive navigation.

The bug this fixes is small: the sidebar was blind to entitlements, so a `free` workspace saw
Network and Campaigns and found out by clicking and getting a 402.

The bug it must not *introduce* is much bigger. `NEXUS_BILLING_ENFORCEMENT` defaults to `shadow`,
which resolves every entitlement and then allows the call anyway. A UI that hid a nav item because
the policy said "disabled" would hide a feature that still works — turning a rollout mode whose
entire promise is "changes nothing" into a visible product regression. Hence `gating_active`, and
hence most of the tests below.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def _seeded(client, slug: str) -> str:
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()
    return await signup(client, slug=slug, email=f"o@{slug}.com", company=slug.upper())


async def test_endpoint_returns_the_module_gates(client):
    token = await _seeded(client, "ent1")
    r = await client.get("/api/billing/entitlements", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {m["capability_id"] for m in body["modules"]}
    # Only module gates; navigation is coarse and per-action quotas stay on the action.
    assert ids
    assert all(i.startswith("module.") for i in ids)
    assert "module.network" in ids


async def test_it_reports_enforcement_and_defaults_to_not_gating(client):
    """Shadow is the default, and in shadow the server allows everything. The UI must be told
    that, or it will hide features that work."""
    token = await _seeded(client, "ent2")
    r = await client.get("/api/billing/entitlements", headers=auth(token))
    body = r.json()
    assert body["enforcement"] == "shadow"
    assert body["gating_active"] is False


async def test_gating_active_is_true_only_under_real_enforcement(client, monkeypatch):
    from nexus.core.config import get_settings

    token = await _seeded(client, "ent3")
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    r = await client.get("/api/billing/entitlements", headers=auth(token))
    assert r.json()["gating_active"] is True

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    r = await client.get("/api/billing/entitlements", headers=auth(token))
    assert r.json()["gating_active"] is False


async def test_a_disabled_module_is_reported_as_not_included(client, monkeypatch):
    """The policy answer is still reported in shadow mode — it is `gating_active` that tells the
    UI whether to act on it. Reporting nothing would make it impossible to preview a plan
    change before enforcing."""
    import nexus.billing.entitlements as ent_mod
    from nexus.billing.entitlements import ResolvedEntitlement

    token = await _seeded(client, "ent4")

    async def _fake_resolve(ts, capability_id, **kw):
        mode = "disabled" if capability_id == "module.network" else "enabled"
        return ResolvedEntitlement(capability_id, mode=mode, source="plan")

    # The router imports `resolve_entitlement` inside the handler, so patching the module
    # attribute is what the call actually resolves.
    monkeypatch.setattr(ent_mod, "resolve_entitlement", _fake_resolve)

    r = await client.get("/api/billing/entitlements", headers=auth(token))
    modules = {m["capability_id"]: m for m in r.json()["modules"]}
    assert modules["module.network"]["included"] is False
    assert modules["module.calling"]["included"] is True


async def test_an_enterprise_module_is_not_advertised_as_included(client, monkeypatch):
    """`enterprise` means 'talk to us', not 'you have it'. A self-serve plan must not advertise a
    module it cannot actually turn on."""
    import nexus.billing.entitlements as ent_mod
    from nexus.billing.entitlements import ResolvedEntitlement

    token = await _seeded(client, "ent5")

    async def _enterprise(ts, capability_id, **kw):
        return ResolvedEntitlement(capability_id, mode="enterprise", source="catalog")

    monkeypatch.setattr(ent_mod, "resolve_entitlement", _enterprise)
    r = await client.get("/api/billing/entitlements", headers=auth(token))
    assert all(m["included"] is False for m in r.json()["modules"])


async def test_a_rep_can_read_it(client):
    """Every member's navigation depends on this. Making it admin-only would mean a rep's
    sidebar could never be gated at all."""
    from nexus.billing.catalog import sync_catalog

    await sync_catalog()
    owner = await signup(client, slug="ent6", email="o@ent6.com", company="ENT6")
    invite = await client.post(
        "/api/workspace/members", headers=auth(owner),
        json={"email": "rep@ent6.com", "full_name": "A Rep", "role": "rep",
              "password": "reppassword123"},
    )
    assert invite.status_code in (200, 201), invite.text
    login = await client.post(
        "/api/auth/login", json={"email": "rep@ent6.com", "password": "reppassword123"}
    )
    rep_token = login.json()["access_token"]
    r = await client.get("/api/billing/entitlements", headers=auth(rep_token))
    assert r.status_code == 200


async def test_it_requires_authentication(client):
    r = await client.get("/api/billing/entitlements")
    assert r.status_code in (401, 403)


async def test_a_workspace_with_no_subscription_keeps_its_product_modules(client):
    """Unknown always means allow — the same bias as the engine itself. A workspace the billing
    system has never heard of must not lose its navigation.

    `module.api` is the deliberate exception: it is catalogued `enterprise`, so it is "talk to
    us" for everyone until a plan says otherwise, subscription or not."""
    token = await _seeded(client, "ent7")
    # Genuinely REMOVE the subscription. Signup now attaches `free`, which explicitly disables
    # these modules — so signing up no longer produces the state this test is about, and asserting
    # against it would silently be testing the free plan instead of the unknown-means-allow bias.
    from sqlalchemy import delete

    from nexus.models.billing import BillingSubscription
    from nexus.workers.tasks import tenant_session
    from tests.conftest import principal_from_token

    async with tenant_session(principal_from_token(token).tenant_id) as ts:
        await ts.session.execute(
            delete(BillingSubscription).where(
                BillingSubscription.tenant_id == ts.tenant_id
            )
        )
        await ts.flush()

    r = await client.get("/api/billing/entitlements", headers=auth(token))
    body = r.json()
    assert body["gating_active"] is False
    modules = {m["capability_id"]: m for m in body["modules"]}
    for cap in ("module.network", "module.calling", "module.outreach", "module.integrations"):
        assert modules[cap]["included"] is True, cap
    assert modules["module.api"]["included"] is False


# ---- end-to-end through the REAL seeded plans ------------------------------------------------
#
# The `_fake_resolve` test above proves the endpoint reports whatever the engine says. These prove
# the SEED actually says the right thing — that `free` and `starter` really do disable the modules
# the sidebar keys on. Without these, the seed could stop disabling a module and every test would
# still pass while the nav quietly stopped gating.


async def _tenant_on_plan(client, slug: str, plan_id: str) -> str:
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingSubscription
    from nexus.workers.tasks import tenant_session
    from tests.conftest import principal_from_token

    await sync_catalog()
    await sync_plans()
    token = await signup(client, slug=slug, email=f"o@{slug}.com", company=slug.upper())
        # SWITCH the subscription signup created rather than adding a second: a new workspace
        # starts on `free`, and two live rows make entitlement resolution ambiguous.
    from tests.conftest import put_on_plan

    await put_on_plan(principal_from_token(token).tenant_id, plan_id)
    return token


async def test_the_free_plan_really_disables_the_modules_the_nav_keys_on(client):
    """Free disables all five product modules in the seed. If that ever stops being true, the
    sidebar silently stops gating and nobody finds out."""
    token = await _tenant_on_plan(client, "entf", "free")
    r = await client.get("/api/billing/entitlements", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"] == "free"
    modules = {m["capability_id"]: m for m in body["modules"]}
    for cap in ("module.network", "module.outreach", "module.calling",
                "module.discovery", "module.integrations"):
        assert modules[cap]["included"] is False, cap
        assert modules[cap]["source"] == "plan", cap


async def test_the_starter_plan_disables_only_network_and_calling(client):
    """Starter is the partial case, and it is the one that would catch an over-broad gate:
    outreach must stay included, or Campaigns/Cadences vanish for a paying customer."""
    token = await _tenant_on_plan(client, "ents", "starter")
    modules = {
        m["capability_id"]: m
        for m in (await client.get("/api/billing/entitlements",
                                   headers=auth(token))).json()["modules"]
    }
    assert modules["module.network"]["included"] is False
    assert modules["module.calling"]["included"] is False
    # Still included — these drive Campaigns, Cadences and Integrations in the sidebar.
    assert modules["module.outreach"]["included"] is True
    assert modules["module.integrations"]["included"] is True
    assert modules["module.discovery"]["included"] is True


async def test_free_is_still_not_gated_while_enforcement_is_shadow(client):
    """The whole safety property, end to end on real seed data: a Free workspace resolves five
    disabled modules AND is told not to act on it, because the server would still serve them."""
    token = await _tenant_on_plan(client, "entg", "free")
    body = (await client.get("/api/billing/entitlements", headers=auth(token))).json()
    assert any(m["included"] is False for m in body["modules"])
    assert body["gating_active"] is False


async def test_free_is_gated_once_enforcement_is_on(client, monkeypatch):
    from nexus.core.config import get_settings

    token = await _tenant_on_plan(client, "enth", "free")
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    body = (await client.get("/api/billing/entitlements", headers=auth(token))).json()
    assert body["gating_active"] is True
    modules = {m["capability_id"]: m for m in body["modules"]}
    assert modules["module.network"]["included"] is False


# ---- the nav and the catalog must agree ---------------------------------------------------------
#
# `isLocked` looks a capability up in the entitlements response and returns FALSE when it finds
# nothing ("unknown means allow", matching the engine). That bias is right, and it makes a typo in
# nav.tsx completely silent: the item simply never gates, forever, and no test or log says so. The
# two tests below are structural — they read the source — because there is no runtime moment at
# which the mismatch is observable.

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_NAV = _ROOT / "frontend" / "src" / "app" / "nav.tsx"
_APP = _ROOT / "frontend" / "src" / "App.tsx"


def _nav_capabilities() -> set[str]:
    return set(re.findall(r'capability:\s*"([^"]+)"', _NAV.read_text(encoding="utf-8")))


def test_every_capability_the_nav_keys_on_exists_in_the_catalog():
    from nexus.billing.catalog import CAPABILITY_SEED

    known = {c["id"] for c in CAPABILITY_SEED}
    referenced = _nav_capabilities()
    assert referenced, "nav.tsx should gate at least some items"
    missing = referenced - known
    assert not missing, (
        f"nav.tsx gates on capabilities that do not exist: {sorted(missing)}. "
        "isLocked would silently never lock these."
    )


def test_the_routes_guard_the_same_capabilities_the_nav_hides():
    """Hiding a link is presentation; the route guard is the access control.

    A gated nav item whose route is unguarded is reachable by typing the URL, which is exactly how
    a customer on a restricted plan finds the page they did not buy.
    """
    guarded = set(
        re.findall(r'RequireCapability capability="([^"]+)"', _APP.read_text(encoding="utf-8"))
    )
    missing = _nav_capabilities() - guarded
    assert not missing, (
        f"these capabilities gate a nav item but no route: {sorted(missing)} — "
        "the pages are still reachable by URL."
    )


def test_the_floor_of_the_product_is_never_gated():
    """Dashboard, Accounts, Contacts, Members, Settings and Billing carry no capability.

    Billing is the load-bearing one: gating it behind a plan locks the customer out of the only
    page where they could change the plan that locked them out. Dashboard is the landing route, so
    gating it means a redirect loop into an empty shell.
    """
    source = _NAV.read_text(encoding="utf-8")
    for route in ("/dashboard", "/accounts", "/contacts", "/members", "/settings/billing"):
        block = re.search(r'\{[^{}]*to:\s*"' + re.escape(route) + r'"[^{}]*\}', source, re.S)
        assert block, f"{route} is no longer in NAV_ITEMS"
        assert "capability" not in block.group(0), (
            f"{route} must never be plan-gated — see the comment above NAV_ITEMS."
        )


async def test_the_new_module_gates_change_nothing_for_existing_plans(client):
    """Adding module.signals/lists/plays/relevance/agents must be a strict no-op on rollout.

    They default to `enabled` in the catalog, and `resolve_entitlement` falls back to the catalog
    default when a plan does not list a capability. So every tenant on every existing plan keeps
    seeing Inbox, Signals, Alerts, Lists, Plays, Relevance, Orchestrator, Runs and Approvals until
    an operator deliberately turns one off.
    """
    new = ["module.signals", "module.lists", "module.plays", "module.relevance", "module.agents"]
    for slug, plan in (("entnew1", "free"), ("entnew2", "starter"), ("entnew3", "growth")):
        token = await _tenant_on_plan(client, slug, plan)
        body = (await client.get("/api/billing/entitlements", headers=auth(token))).json()
        modules = {m["capability_id"]: m for m in body["modules"]}
        for cap in new:
            assert cap in modules, f"{cap} missing from the entitlements response"
            assert modules[cap]["included"] is True, (
                f"{cap} is excluded on {plan} — adding it was supposed to change nothing"
            )


# ---- the page title is derived, not hand-maintained ----------------------------------------------


def test_no_second_hand_maintained_list_of_page_titles():
    """`AppShell` used to hold a literal map of seven titles for twenty-two destinations, so Calls,
    Contacts, Network, Lists, AI Runs and eight others rendered as "InfoJoy GTM · InfoJoy GTM".

    A second list of every page will always drift from the first. This asserts the derivation is
    still in place rather than re-checking each title, because re-listing them here would just be
    the same duplicate one layer further out.
    """
    shell = (_ROOT / "frontend" / "src" / "components" / "layout" / "AppShell.tsx").read_text(
        encoding="utf-8"
    )
    assert "NAV_ITEMS.map" in shell, "page titles are no longer derived from the nav"
    assert "const TITLES: Record<string, string> = {" not in shell, (
        "a hand-maintained title map is back — it will drift again"
    )


def test_every_nav_destination_can_produce_a_title():
    """The derivation only works if each nav item has a usable label."""
    source = _NAV.read_text(encoding="utf-8")
    labels = re.findall(r'label:\s*"([^"]+)"', source)
    routes = re.findall(r'to:\s*"([^"]+)"', source)
    assert len(labels) == len(routes), "every nav item needs both a route and a label"
    assert all(label.strip() for label in labels)
    # The bug in one line: these five were the ones reported as showing the app name.
    for route in ("/calls", "/contacts", "/network", "/lists", "/runs"):
        assert route in routes, f"{route} left NAV_ITEMS — its title would fall back to the app name"


# ---- the API client must not double-encode a request body ----------------------------------------


def test_no_api_method_pre_stringifies_its_body():
    """`request()` serializes `body` itself. Passing an already-stringified string double-encodes
    it, so the wire carries a JSON *string* instead of an object and FastAPI answers 422.

    That is exactly what happened to `billingCheckout` and `billingPortal`: both money actions
    failed the first time anyone clicked them. Every other method on the client passes a plain
    object, so this asserts the convention rather than the two symptoms.
    """
    api = (_ROOT / "frontend" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    offenders = re.findall(r"body:\s*JSON\.stringify\(", api)
    assert not offenders, (
        f"{len(offenders)} request(s) pre-stringify their body — `request()` already does that, "
        "and the result is a 422 with no useful detail."
    )


def test_a_validation_error_renders_as_text_not_object_object():
    """FastAPI returns 422 `detail` as an ARRAY of {loc, msg, type}. `String(detail)` on that
    yields "[object Object]", which is indistinguishable from a rendering fault — so a real
    backend rejection reads to the user as "the button does nothing"."""
    api = (_ROOT / "frontend" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "export function errorDetail(" in api, "the 422 detail formatter is gone"
    assert "Array.isArray(detail)" in api, "the 422 array shape is no longer handled"
