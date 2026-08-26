# tests/test_core_plan.py
"""The entry paid tier, sold self-serve, and the module gates that make it cheaper to SERVE.

Retargeted from `core` to `launch` on 2026-08-26 when the ladder collapsed to Free / Launch /
Accelerate. `core` is retired — it keeps its one subscriber and its entitlements, but it is off the
price list, so the "is it buyable" assertions had to move to a tier that is.

Two things have to be true at once, and they are enforced in different places:

* **It has to be BUYABLE.** `_reject_if_admin_managed` returns 409 for `plan_class` in
  ("custom", "enterprise"), so a per-tenant custom plan can never go through Checkout. Core is on
  the price list precisely because it is `standard`. That one field is the whole difference between
  a bespoke deal and a product.
* **It has to be CHEAPER TO SERVE, not just cheaper.** A module gate that only hides a menu item is
  a discount with no cost saving — the pages disappear and the crawler, the classifier and the
  agent runtime keep spending. The `depends_on` wiring is what makes the saving real, and the
  cascade tests below are the ones that would catch it being unwired.

The regression risk of that wiring is the reason for
`test_the_new_dependencies_change_nothing_for_existing_plans`.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def _tenant_on_plan(client, slug: str, plan_id: str) -> str:
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingSubscription
    from nexus.workers.tasks import tenant_session
    from tests.conftest import principal_from_token

    await sync_catalog()
    await sync_plans()
    token = await signup(client, slug=slug, email=f"o@{slug}.com", company=slug.upper())
    async with tenant_session(principal_from_token(token).tenant_id) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        await ts.flush()
    return token


async def _resolve(token: str, capability_id: str):
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.workers.tasks import tenant_session
    from tests.conftest import principal_from_token

    async with tenant_session(principal_from_token(token).tenant_id) as ts:
        return await resolve_entitlement(ts, capability_id)


# ---- it is a product, not a deal ----------------------------------------------------------------

def test_core_is_a_standard_plan_so_it_can_be_bought_self_serve():
    from nexus.api.routers.billing import ADMIN_MANAGED_PLAN_CLASSES
    from nexus.billing.plans import PLAN_SEED

    core = next(p for p in PLAN_SEED if p["id"] == "launch")
    assert core["plan_class"] == "standard"
    assert core["plan_class"] not in ADMIN_MANAGED_PLAN_CLASSES, (
        "a custom/enterprise plan is refused by /billing/checkout with a 409 — Core must not be one"
    )
    assert core["status"] == "active"


def test_the_ladder_climbs_in_both_price_and_position():
    """Free -> Launch -> Accelerate, in sort order, price and seats. A ladder that is not
    monotonic in all three is one a customer can game."""
    from nexus.billing.plans import PLAN_SEED

    by_id = {p["id"]: p for p in PLAN_SEED}
    free, launch, accelerate = by_id["free"], by_id["launch"], by_id["accelerate"]
    assert free["sort_order"] < launch["sort_order"] < accelerate["sort_order"]
    assert (free["base_price_cents"] < launch["base_price_cents"]
            < accelerate["base_price_cents"])
    assert launch["included_credits"] < accelerate["included_credits"]
    assert launch["max_seats"] < accelerate["max_seats"]


async def test_the_entry_tier_is_offered_in_the_api_plan_list(client):
    """It has to actually appear where a customer would pick it."""
    from nexus.billing.plans import sync_plans

    await sync_plans()
    token = await signup(client, slug="corepl", email="o@corepl.com", company="COREPL")
    r = await client.get("/api/billing/plans", headers=auth(token))
    assert r.status_code == 200, r.text
    plans = {p["id"]: p for p in r.json()}
    assert "launch" in plans, "Launch is not on the price list the customer sees"
    assert plans["launch"]["base_price_cents"] == 9900


# ---- what the customer gets ---------------------------------------------------------------------

async def test_core_leaves_the_floor_of_the_product_intact(client):
    """Dashboard, Accounts and Contacts carry no capability, so there is nothing to disable.

    This is what makes Core sellable rather than empty: the plan is defined by what it excludes,
    and the excluded list can never reach those three.
    """
    from nexus.billing.plans import PLAN_SEED

    core = next(p for p in PLAN_SEED if p["id"] == "core")
    disabled = {cap for cap, mode, *_ in core["entitlements"] if mode == "disabled"}
    # Every one of these gates a page Core is supposed to keep. None may appear.
    assert not disabled & {"module.lists", "module.relevance"}


async def test_core_hides_the_modules_it_excludes(client):
    token = await _tenant_on_plan(client, "core1", "core")
    body = (await client.get("/api/billing/entitlements", headers=auth(token))).json()
    modules = {m["capability_id"]: m["included"] for m in body["modules"]}
    for cap in (
        "module.signals", "module.outreach", "module.calling", "module.network",
        "module.discovery", "module.integrations", "module.plays", "module.agents",
    ):
        assert modules[cap] is False, f"{cap} should be excluded on Core"
    # Kept, and deliberately so — see the comment in catalog.py.
    assert modules["module.lists"] is True
    assert modules["module.relevance"] is True


# ---- the saving is real, not cosmetic -----------------------------------------------------------

async def test_a_hidden_module_stops_the_spending_behind_it(client):
    """The point of `depends_on`. Without it Core hides Signals and still pays to collect them."""
    token = await _tenant_on_plan(client, "core2", "core")
    for cap in (
        "signal.news_scan", "signal.rss_scan", "signal.stored", "inbox.task",
        "automation.play_run", "workflow.orchestration_run", "workflow.orchestration_step",
        "ai.chat_turn",
    ):
        ent = await _resolve(token, cap)
        assert ent.mode == "disabled", f"{cap} still bills on Core (mode={ent.mode})"


async def test_relevance_scoring_survives_on_core(client):
    """The deliberate exception, and the reason it is not a cascade.

    Relevance scores are the most useful column on the Accounts page, and Accounts is on every
    plan. Tying `ai.scoring` to `module.relevance` would sell a page with its point removed.
    """
    token = await _tenant_on_plan(client, "core3", "core")
    assert (await _resolve(token, "ai.scoring")).mode != "disabled"
    # Same reasoning: the account record itself must stay current for a plan that sells accounts.
    assert (await _resolve(token, "automation.account_refresh")).mode != "disabled"


async def test_core_still_enriches_contacts(client):
    """It sells contacts, so the things that make a contact useful have to work."""
    token = await _tenant_on_plan(client, "core4", "core")
    for cap in ("enrich.contact", "verify.email", "enrich.phone"):
        assert (await _resolve(token, cap)).mode != "disabled", cap


# ---- the wiring must not touch anyone else ------------------------------------------------------

async def test_the_new_dependencies_change_nothing_for_existing_plans(client):
    """Adding `depends_on` to eight capabilities is only safe because no pre-existing plan
    disables the modules they now hang off.

    `module.signals`, `module.plays` and `module.agents` are attached to no seeded plan except
    `core`, and `resolve_entitlement` falls back to the catalog default (`enabled`) for a
    capability a plan does not list — so nothing cascades. If someone later disables one of those
    modules on Free or Starter, this test is what tells them they also switched off signal
    collection.
    """
    cascaded = (
        "signal.news_scan", "signal.stored", "inbox.task", "automation.play_run",
        "workflow.orchestration_run", "ai.chat_turn",
    )
    for slug, plan in (("coren1", "free"), ("coren2", "starter"), ("coren3", "growth")):
        token = await _tenant_on_plan(client, slug, plan)
        for cap in cascaded:
            ent = await _resolve(token, cap)
            assert ent.mode != "disabled", (
                f"{cap} became disabled on {plan} — the dependency wiring was supposed to be a "
                f"no-op for existing plans (source={ent.source})"
            )


async def test_a_legacy_tenant_is_untouched_by_the_dependency_wiring(client):
    """Unlimited plan classes return before dependencies are applied at all."""
    token = await _tenant_on_plan(client, "coren4", "legacy-unlimited")
    for cap in ("signal.news_scan", "inbox.task", "ai.chat_turn", "automation.play_run"):
        ent = await _resolve(token, cap)
        assert ent.mode == "unlimited", f"{cap} on legacy-unlimited resolved {ent.mode}"


# ---- the price list itself -----------------------------------------------------------------------
#
# `POST /billing/checkout` always took a `plan_id` and nothing said which ids exist, so the only
# way to buy a plan was to already know its id. A locked nav item routes to /settings/billing
# offering to "view upgrade options"; this endpoint is what makes that not a lie.


async def test_the_price_list_omits_what_checkout_would_refuse(client):
    """Listing a plan whose purchase the next click rejects with a 409 is worse than not listing it."""
    from nexus.api.routers.billing import ADMIN_MANAGED_PLAN_CLASSES
    from nexus.billing.plans import sync_plans

    await sync_plans()
    token = await signup(client, slug="pl1", email="o@pl1.com", company="PL1")
    rows = (await client.get("/api/billing/plans", headers=auth(token))).json()
    ids = {p["id"] for p in rows}

    assert ADMIN_MANAGED_PLAN_CLASSES == ("custom", "enterprise")
    assert "enterprise" not in ids
    # Not sellable for their own reasons: a grandfathered tenant "upgrading" off unlimited onto a
    # metered plan is a downgrade wearing the wrong label, and internal is staff-only.
    assert "legacy-unlimited" not in ids
    assert "internal" not in ids
    assert "trial" not in ids, "a trial is entered by signing up, not bought"
    assert {"free", "launch", "accelerate"} <= ids


async def test_the_price_list_is_ordered_and_marks_the_current_plan(client):
    token = await _tenant_on_plan(client, "pl2", "launch")
    rows = (await client.get("/api/billing/plans", headers=auth(token))).json()
    assert [p["sort_order"] for p in rows] == sorted(p["sort_order"] for p in rows)
    current = [p["id"] for p in rows if p["current"]]
    assert current == ["launch"], f"expected launch marked current, got {current}"


async def test_each_plan_reports_what_it_includes_not_what_the_caller_has(client):
    """The picker answers "what would I get if I switched", so modules must resolve against each
    PLAN. Resolving against the caller's subscription would show every row identically."""
    token = await _tenant_on_plan(client, "pl3", "accelerate")
    rows = {p["id"]: p for p in (await client.get("/api/billing/plans", headers=auth(token))).json()}

    assert "Outreach module" in rows["accelerate"]["includes"]
    # ...and Free still reports the restricted modules excluded, even though the CALLER is on
    # Accelerate and has them. Resolving against the caller would show every row identically,
    # which makes the picker useless for the one question it answers.
    assert rows["free"]["excludes"], "the free tier must report what it does not include"
    assert len(rows["free"]["includes"]) < len(rows["accelerate"]["includes"])


async def test_a_retired_plan_leaves_the_price_list_without_a_deploy(client):
    """Setting `status` in Admin is how a plan is withdrawn."""
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlan

    token = await _tenant_on_plan(client, "pl4", "growth")
    async with get_sessionmaker()() as session:
        plan = await session.get(BillingPlan, "core")
        plan.status = "retired"
        await session.commit()
    try:
        ids = {p["id"] for p in (await client.get("/api/billing/plans", headers=auth(token))).json()}
        assert "core" not in ids
    finally:
        async with get_sessionmaker()() as session:
            plan = await session.get(BillingPlan, "core")
            plan.status = "active"
            await session.commit()


async def test_a_rep_cannot_read_the_price_list(client):
    """Money surfaces are admin-only, matching /checkout and /portal.

    A rep who hits a 402 gets /billing/usage, which is deliberately rep-level — seeing "17 of 20
    used" is what they need. What the workspace could pay instead is their admin's decision.
    """
    from nexus.core.security import create_access_token
    from tests.conftest import principal_from_token

    owner = await signup(client, slug="pl5", email="o@pl5.com", company="PL5")
    tenant_id = principal_from_token(owner).tenant_id
    rep = create_access_token(user_id="rep-user", tenant_id=tenant_id, role="rep")

    assert (await client.get("/api/billing/plans", headers=auth(rep))).status_code == 403
    # ...but the usage page they are pointed at still works.
    assert (await client.get("/api/billing/usage", headers=auth(rep))).status_code == 200


async def test_usage_reports_the_plan_class_so_the_ui_need_not_guess(client):
    """The picker decides between "here is the price list" and "talk to your account team".

    It used to answer that by testing whether the plan id started with `custom-` — a naming
    convention doing load-bearing work in the UI, which breaks silently the first time a plan is
    named anything else. The server knows; it now says.
    """
    token = await _tenant_on_plan(client, "pc1", "core")
    body = (await client.get("/api/billing/usage", headers=auth(token))).json()
    assert body["plan_class"] == "standard"

    from nexus.api.routers.billing import ADMIN_MANAGED_PLAN_CLASSES

    token2 = await _tenant_on_plan(client, "pc2", "enterprise")
    body2 = (await client.get("/api/billing/usage", headers=auth(token2))).json()
    assert body2["plan_class"] in ADMIN_MANAGED_PLAN_CLASSES


async def test_the_price_list_is_one_query_per_table_not_one_per_cell(client):
    """Resolving module inclusion per (plan x capability) is 5 x 11 = 55 round-trips to render one
    page. The counter here is crude but it is the difference between a constant and a product."""
    from sqlalchemy import event

    from nexus.core.db import get_engine

    token = await _tenant_on_plan(client, "pq1", "core")
    engine = get_engine().sync_engine
    seen: list[str] = []

    def before(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        r = await client.get("/api/billing/plans", headers=auth(token))
        assert r.status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", before)

    selects = [s for s in seen if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) < 15, (
        f"{len(selects)} SELECTs to render the price list — the per-cell resolve is back"
    )
