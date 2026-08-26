# Plan Ladder, Capability Authoring and Panel Access — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the price list to Free / Launch / Accelerate with 20%-off annuals, remove the last thing that needs a deploy (creating a billable capability), meter `ai.tokens`, restrict the Control plane to two IPs, and make "which key is live" and "has this setting taken effect" visible.

**Architecture:** Six independent changes against existing seams. Plans are rows, so the ladder change is data plus a retire pass — **not** deletes, because three subscriptions point at plans being withdrawn. Capability authoring reuses `plan_authoring`'s shape: service function, `extra="forbid"` schema, margin validation. IP restriction wraps `require_platform_permission` rather than replacing it, so every existing gate inherits it. The two indicators are computed server-side from the same ordering the resolver actually uses, so the light cannot disagree with reality.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, Pydantic v2, Alembic, React 18 + TypeScript, CSS Modules.

---

## Why a new billable capability needs a deploy today

`CAPABILITY_SEED` in `nexus/billing/catalog.py` is a Python list. `sync_catalog()` inserts any row it names that does not exist, and never deletes. There is **no write path to `billing_capabilities` anywhere else** — `PUT /admin/billing/rates/{id}` and `PUT /admin/billing/plans/{id}/entitlements/{cap}` both 404 when the capability row is missing, so they can only price and entitle what the seed already created.

So the table is writable, the API is not. A new capability means editing the list, which means a release.

The fix is one endpoint. Two properties make it safe:

* `sync_catalog()` only ever upserts rows named in the seed. An admin-created capability is not in the seed, so a later deploy leaves it entirely alone — no risk of a redeploy silently reverting or clobbering it.
* `_MANAGED_FIELDS` re-asserts `category`/`unit`/`depends_on` from code, but again only for seeded ids.

The one real hazard is the bug already found and fixed this week: **a capability with no rate card is metered and then rated at nothing.** So creation must offer to price it in the same call, and refuse to leave a priced-nothing hole quietly.

---

## Current state that constrains the work

Measured on the live database before starting:

| Plan | Class | Subscribers |
|---|---|---|
| `legacy-unlimited` | unlimited | **13** |
| `growth` | standard | **2** |
| `core` | standard | **1** |
| `custom-dbg4683` | custom | **1** |
| everything else | — | 0 |

**Four plans have live subscribers and none of them may be deleted.** `billing_subscriptions.plan_id` is a foreign key to `billing_plans.id`; deleting a plan with subscribers either violates the constraint or orphans a paying customer. Entitlements also resolve *from the plan row*, so a deleted plan means a subscriber with no entitlements at all — which resolves to permissive catalog defaults and hands them everything.

The ladder change therefore **retires**, it does not delete.

## Target ladder economics

Verified against the measured cost base (blended $0.00192/credit, worst case $0.00400):

| Plan | Price | Credits | cr/$ | GM blended | GM worst |
|---|---|---|---|---|---|
| Free | $0 | 1,000 | — | — | — |
| Launch | $99/mo | 2,500 | 25.3 | 95.2% | 89.9% |
| Accelerate | $199/mo | 8,000 | 40.2 | 92.3% | 83.9% |
| Launch Annual | $950/yr | 30,000 | 31.6 | 93.9% | 87.4% |
| Accelerate Annual | $1,910/yr | 96,000 | 50.3 | 90.4% | 79.9% |

Highest is 50.3 cr/$, half the 100 cr/$ design ceiling and a fifth of the 250 survival limit. **Free costs $1.92 per fully-consuming user at blended cost, $4.00 worst case** — that is the acquisition budget, and it is the number to watch if free signups grow faster than conversions.

---

## File Structure

**Created**

| File | Responsibility |
|---|---|
| `nexus/billing/capability_authoring.py` | Create a billable capability, optionally with its rate card. Validates the id shape and `depends_on`. |
| `nexus/api/deps_ip.py` | The IP allowlist check, as a wrapper the existing platform gates compose with. |
| `tests/test_capability_authoring.py` | Capability creation, the priced-nothing hazard, refusals. |
| `tests/test_admin_ip_allowlist.py` | Allowlist enforcement, the lockout guards. |
| `scripts/restructure_plans.py` | One-shot, idempotent ladder migration. Retires, never deletes. |

**Modified**

| File | Change |
|---|---|
| `nexus/billing/plans.py` | Seed the three new plans; leave existing rows alone. |
| `nexus/api/routers/admin_billing_write.py` | `POST /capabilities`. |
| `nexus/api/deps.py` | `require_platform_permission` consults the IP allowlist. |
| `nexus/runtime_config/catalog.py` | Two new settings: the second allowed IP, and the allowlist on/off. |
| `nexus/runtime_config/service.py` | `current_values` reports `in_effect`. |
| `nexus/api/routers/admin_provider_keys.py` | `in_use` on the key list. |
| `nexus/api/routers/agents.py` | Meter `ai.tokens` after a run. |
| `frontend/src/pages/admin/ProviderKeysTab.tsx` | Green "In use" indicator. |
| `frontend/src/pages/admin/RuntimeConfigTab.tsx` | "Live" vs "pending restart" indicator. |
| `frontend/src/lib/types.ts` | `in_use`, `in_effect` fields. |

---

### Task 1: Seed the new ladder

**Files:**
- Modify: `nexus/billing/plans.py`
- Test: `tests/test_plan_ladder.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_plan_ladder.py
"""The public ladder: Free, Launch, Accelerate, and the two annuals.

Collapsed from eight tiers on 2026-08-26. The old ones are RETIRED, never deleted: three
subscriptions point at `core` and `growth`, and `billing_subscriptions.plan_id` is a foreign key.
A deleted plan is either a constraint violation or a paying customer with no entitlements — which
resolves to permissive catalog defaults and hands them everything.
"""
from __future__ import annotations


async def test_the_ladder_is_free_launch_accelerate(fresh_db):
    from sqlalchemy import select

    from nexus.billing.plans import sync_plans
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlan

    await sync_plans()
    async with get_sessionmaker()() as s:
        rows = {p.id: p for p in (await s.scalars(select(BillingPlan))).all()}

    for pid, price, credits, interval in (
        ("launch", 9900, 2500, "month"),
        ("accelerate", 19900, 8000, "month"),
        ("launch-annual", 95000, 30000, "year"),
        ("accelerate-annual", 191000, 96000, "year"),
    ):
        plan = rows.get(pid)
        assert plan is not None, f"{pid} missing"
        assert plan.base_price_cents == price
        assert plan.included_credits == credits
        assert plan.interval == interval
        assert plan.plan_class == "standard"

    assert rows["free"].included_credits == 1000, "free must be enough to try every feature"


async def test_the_annuals_are_twenty_percent_off(fresh_db):
    """Stated as a rule, not a number: the discount is the product decision, the price is
    arithmetic. If someone reprices the monthly tier, this catches an annual left behind."""
    from sqlalchemy import select

    from nexus.billing.plans import sync_plans
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlan

    await sync_plans()
    async with get_sessionmaker()() as s:
        rows = {p.id: p for p in (await s.scalars(select(BillingPlan))).all()}

    for monthly, annual in (("launch", "launch-annual"), ("accelerate", "accelerate-annual")):
        full_year = rows[monthly].base_price_cents * 12
        discount = 1 - (rows[annual].base_price_cents / full_year)
        assert 0.19 <= discount <= 0.21, f"{annual} is {discount:.1%} off, expected ~20%"
        assert rows[annual].included_credits == rows[monthly].included_credits * 12


async def test_no_plan_with_subscribers_is_ever_deleted(fresh_db):
    """The safety property of the whole restructure."""
    from sqlalchemy import select

    from nexus.billing.plans import sync_plans
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlan

    await sync_plans()
    async with get_sessionmaker()() as s:
        ids = {p.id for p in (await s.scalars(select(BillingPlan))).all()}
    # Seeding never removes. `legacy-unlimited` is the migration keystone for 13 workspaces.
    assert "legacy-unlimited" in ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_plan_ladder.py -n0 -q`
Expected: FAIL — `launch` missing.

- [ ] **Step 3: Add the three plans to the seed**

In `nexus/billing/plans.py`, add to the plan seed list. Reuse the existing entitlement list that
`growth` uses for Launch and `business`/`professional` uses for Accelerate — do not invent new
entitlement sets, or the two tiers will drift from what was tested.

```python
    {
        "id": "launch", "name": "Launch", "plan_class": "standard", "status": "active",
        "description": "Everything you need to run signal-led outreach.",
        "base_price_cents": 9900, "seat_price_cents": 9900, "included_credits": 2500,
        "interval": "month", "max_seats": None, "trial_days": 14, "sort_order": 20,
        "entitlements": _GROWTH_ENT,
    },
    {
        "id": "accelerate", "name": "Accelerate", "plan_class": "standard", "status": "active",
        "description": "For teams running the full loop at volume.",
        "base_price_cents": 19900, "seat_price_cents": 19900, "included_credits": 8000,
        "interval": "month", "max_seats": None, "trial_days": 14, "sort_order": 30,
        "entitlements": _PRO_ENT,
    },
    {
        "id": "launch-annual", "name": "Launch (annual)", "plan_class": "standard",
        "status": "active",
        "description": "Launch, paid yearly. Two months and a bit free.",
        "base_price_cents": 95000, "seat_price_cents": 95000, "included_credits": 30000,
        "interval": "year", "max_seats": None, "trial_days": 14, "sort_order": 21,
        "entitlements": _GROWTH_ENT,
    },
    {
        "id": "accelerate-annual", "name": "Accelerate (annual)", "plan_class": "standard",
        "status": "active",
        "description": "Accelerate, paid yearly. Two months and a bit free.",
        "base_price_cents": 191000, "seat_price_cents": 191000, "included_credits": 96000,
        "interval": "year", "max_seats": None, "trial_days": 14, "sort_order": 31,
        "entitlements": _PRO_ENT,
    },
```

Also change `free`'s `included_credits` to `1000` in the same seed. Note `sync_plans` does not
overwrite an existing row's price, so the live `free` row needs the script in Task 2 as well.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_plan_ladder.py -n0 -q`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/plans.py tests/test_plan_ladder.py
git commit -m "feat(billing): Launch and Accelerate, with 20%-off annuals"
```

---

### Task 2: Retire the old tiers on the live database

**Files:**
- Create: `scripts/restructure_plans.py`

- [ ] **Step 1: Write the script**

```python
# scripts/restructure_plans.py
"""Collapse the public ladder to Free / Launch / Accelerate. Idempotent.

RETIRES the superseded tiers rather than deleting them. Three subscriptions point at `core` and
`growth`, and `billing_subscriptions.plan_id` is a foreign key — a delete is either a constraint
violation or a paying customer left with no entitlements, which resolves to permissive catalog
defaults and hands them everything.

`status="retired"` takes a plan off `GET /billing/plans` (which filters on `active`) while leaving
every existing subscriber exactly where they are: entitlements resolve from the plan ROW, not from
whether it is on sale. That is the difference between withdrawing a tier and cancelling its
customers.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from nexus.core.db import get_platform_sessionmaker
from nexus.models.billing import BillingPlan, BillingSubscription

# Superseded by Launch / Accelerate. `payg` and `payg-annual` go too: the new ladder leads with a
# free tier that does the same job of letting someone try the product without committing.
RETIRE = (
    "core", "starter", "growth", "professional", "business",
    "scale", "scale-annual", "payg", "payg-annual",
)
KEEP_ACTIVE = ("free", "launch", "accelerate", "launch-annual", "accelerate-annual")


async def main(apply: bool = False) -> None:
    async with get_platform_sessionmaker()() as s:
        plans = {p.id: p for p in (await s.scalars(select(BillingPlan))).all()}
        counts = dict(
            (await s.execute(
                select(BillingSubscription.plan_id, func.count())
                .group_by(BillingSubscription.plan_id)
            )).all()
        )

        print(f"{'plan':22}{'status':14}{'subs':>6}  action")
        for pid in RETIRE:
            plan = plans.get(pid)
            if plan is None:
                continue
            n = counts.get(pid, 0)
            action = "already retired" if plan.status == "retired" else "RETIRE"
            print(f"  {pid:20}{plan.status:14}{n:>6}  {action}"
                  f"{'  (keeps its subscribers)' if n else ''}")
            if apply and plan.status != "retired":
                plan.status = "retired"

        for pid in KEEP_ACTIVE:
            plan = plans.get(pid)
            if plan is None:
                print(f"  {pid:20}{'MISSING':14}{'':>6}  run sync_plans() first")
                continue
            if apply:
                plan.status = "active"
            print(f"  {pid:20}{plan.status:14}{counts.get(pid, 0):>6}  keep active")

        # `free` predates the new sizing and `sync_plans` never overwrites a live row.
        free = plans.get("free")
        if free is not None and free.included_credits != 1000:
            print(f"  free: {free.included_credits} -> 1000 credits")
            if apply:
                free.included_credits = 1000

        if apply:
            await s.commit()
            print("\napplied")
        else:
            print("\ndry run — pass --apply to write")


if __name__ == "__main__":
    import sys

    asyncio.run(main(apply="--apply" in sys.argv))
```

- [ ] **Step 2: Dry run against the live database**

Run: `docker compose -f deploy/docker-compose.prod.yml exec -T app python scripts/restructure_plans.py`
Expected: lists 9 plans to retire, shows `core` with 1 subscriber and `growth` with 2, both marked
"keeps its subscribers". Nothing written.

- [ ] **Step 3: Apply**

Run: `docker compose -f deploy/docker-compose.prod.yml exec -T app python scripts/restructure_plans.py --apply`
Expected: `applied`.

- [ ] **Step 4: Verify the customer price list**

Run:
```bash
curl -s localhost:8080/api/billing/plans -H "Authorization: Bearer $TOK" | python -m json.tool
```
Expected: exactly five entries — free, launch, launch-annual, accelerate, accelerate-annual.

- [ ] **Step 5: Verify the three subscribers are untouched**

Run:
```bash
docker compose -f deploy/docker-compose.prod.yml exec -T app python -c "
import asyncio
from sqlalchemy import select
from nexus.core.db import get_platform_sessionmaker
from nexus.models.billing import BillingSubscription
async def m():
    async with get_platform_sessionmaker()() as s:
        for x in (await s.scalars(select(BillingSubscription))).all():
            print(x.tenant_id[:12], x.plan_id, x.status)
asyncio.run(m())"
```
Expected: the `core` and `growth` subscribers still show `active` on their original plans.

- [ ] **Step 6: Commit**

```bash
git add scripts/restructure_plans.py
git commit -m "feat(billing): retire the superseded tiers without touching their subscribers"
```

---

### Task 3: Create a billable capability without a deploy

**Files:**
- Create: `nexus/billing/capability_authoring.py`
- Modify: `nexus/api/routers/admin_billing_write.py`
- Test: `tests/test_capability_authoring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capability_authoring.py
"""Creating a billable capability from the Control plane.

`CAPABILITY_SEED` is a Python list and `sync_catalog()` only inserts what it names, so a new
capability meant a code edit and a release. The table was always writable; nothing else was.

Safe to add because `sync_catalog` never deletes and only re-asserts managed fields for ids it
knows — an admin-created capability is invisible to it, so no deploy can revert one.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def _admin(client, monkeypatch, *, slug: str, email: str) -> str:
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


async def test_a_capability_can_be_created_and_priced_in_one_call(client, monkeypatch):
    """Priced in the SAME call on purpose. A capability with no rate card is metered and then
    rated at nothing — usage accumulates, quotas count down, no revenue line ever appears. That
    shipped once already: `ai.scoring`, 4,090 runs, free."""
    token = await _admin(client, monkeypatch, slug="ca1", email="boss@ca1.com")
    r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "enrich.company_news", "name": "Company news lookup", "category": "enrich",
        "unit": "lookup", "credits_per_unit": 3, "unit_cost_usd": 0.009,
        "description": "Recent news for one company.",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["capability_id"] == "enrich.company_news"
    assert body["priced"] is True
    assert body["gross_margin"] >= 0.5

    rates = (await client.get("/api/admin/billing/rates", headers=auth(token))).json()
    assert any(x["capability_id"] == "enrich.company_news" for x in rates)


async def test_creating_without_a_price_warns_loudly(client, monkeypatch):
    """Allowed — a module gate has no unit price — but it must not be silent."""
    token = await _admin(client, monkeypatch, slug="ca2", email="boss@ca2.com")
    r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "module.reporting", "name": "Reporting module", "category": "module",
        "unit": "flag",
    })
    assert r.status_code == 201, r.text
    assert r.json()["priced"] is False
    assert "rated at nothing" in r.json()["warning"]


async def test_a_below_floor_price_is_refused(client, monkeypatch):
    """Same guard as the rate endpoint. There must be no path — seed, rate endpoint, or this one —
    that lands an underwater price."""
    token = await _admin(client, monkeypatch, slug="ca3", email="boss@ca3.com")
    r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "enrich.expensive", "name": "Expensive", "category": "enrich", "unit": "call",
        "credits_per_unit": 1, "unit_cost_usd": 0.05,
    })
    assert r.status_code == 422
    assert "margin" in r.text.lower()


async def test_a_duplicate_id_is_refused(client, monkeypatch):
    token = await _admin(client, monkeypatch, slug="ca4", email="boss@ca4.com")
    r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "ai.email_draft", "name": "Dup", "category": "ai", "unit": "action",
    })
    assert r.status_code == 409


async def test_the_id_must_be_dotted_and_lowercase(client, monkeypatch):
    """Ids appear in URLs, entitlement rows, usage events and invoice lines. `category.name` is
    the shape every existing one uses and the shape the UI groups on."""
    token = await _admin(client, monkeypatch, slug="ca5", email="boss@ca5.com")
    for bad in ("NoDot", "Has Space.x", "UPPER.CASE", "trailing."):
        r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
            "id": bad, "name": "X", "category": "ai", "unit": "action",
        })
        assert r.status_code == 400, bad


async def test_an_unknown_dependency_is_refused(client, monkeypatch):
    """`depends_on` gates this capability behind another. Naming one that does not exist produces
    a capability that can never resolve — permanently unusable, silently."""
    token = await _admin(client, monkeypatch, slug="ca6", email="boss@ca6.com")
    r = await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "ai.thing", "name": "Thing", "category": "ai", "unit": "action",
        "depends_on": ["module.nonexistent"],
    })
    assert r.status_code == 400


async def test_a_redeploy_does_not_disturb_an_admin_created_capability(client, monkeypatch):
    """`sync_catalog` re-asserts managed fields for SEEDED ids only. This is what makes the
    endpoint safe to add rather than a race with the next release."""
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingCapability

    token = await _admin(client, monkeypatch, slug="ca7", email="boss@ca7.com")
    await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "enrich.custom_thing", "name": "Custom thing", "category": "enrich",
        "unit": "call", "credits_per_unit": 2, "unit_cost_usd": 0.005,
    })
    await sync_catalog()          # a deploy

    async with get_sessionmaker()() as s:
        row = (await s.scalars(select(BillingCapability).where(
            BillingCapability.id == "enrich.custom_thing"))).first()
    assert row is not None and row.name == "Custom thing"


async def test_a_tenant_owner_cannot_create_a_capability(client):
    token = await signup(client, slug="ca8", email="o@ca8.com", company="CA8")
    assert (await client.post("/api/admin/billing/capabilities", headers=auth(token), json={
        "id": "ai.x", "name": "X", "category": "ai", "unit": "action",
    })).status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_capability_authoring.py -n0 -q`
Expected: FAIL, 404 — no such route.

- [ ] **Step 3: Write the service**

```python
# nexus/billing/capability_authoring.py
"""Create a billable capability without a deploy.

`CAPABILITY_SEED` is a Python list and `sync_catalog()` inserts only what it names, so adding a
capability meant editing code and shipping a release. The table was always writable; the API was
the missing half.

Safe because `sync_catalog` never deletes, and re-asserts `_MANAGED_FIELDS` only for ids it knows.
An admin-created capability is invisible to the seed, so no later deploy can revert or clobber it.

**Pricing is offered in the same call, and its absence is warned about.** A capability with no rate
card is metered and then rated at nothing: usage events accumulate, quotas count down, and no
revenue line ever appears. It looks handled. That shipped once — `ai.scoring`, 4,090 runs, free.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.models.billing import BillingCapability, BillingCostRate, BillingRateCard

# `category.name`. Every existing id uses it, the Admin UI groups on the prefix, and it is what
# appears in URLs, entitlement rows, usage events and invoice lines.
_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

VALID_METER_KINDS = ("counter", "gauge")
VALID_MODES = ("enabled", "metered", "shadow", "disabled", "enterprise")


class CapabilityError(ValueError):
    """The capability cannot be created as asked."""


class DuplicateCapability(CapabilityError):
    """That id already exists."""


async def create_capability(
    session: AsyncSession, *, capability_id: str, name: str, category: str,
    unit: str = "action", description: str = "", sub_category: str = "",
    meter_kind: str = "counter", default_mode: str = "metered",
    depends_on: list[str] | None = None,
    credits_per_unit: float | None = None, unit_cost_usd: float | None = None,
) -> dict:
    """Create the capability, and its rate card when a price is given."""
    capability_id = (capability_id or "").strip().lower()
    if not _ID_RE.match(capability_id):
        raise CapabilityError(
            f"'{capability_id}' is not a valid id — use lowercase category.name, "
            f"for example 'enrich.company_news'"
        )
    if meter_kind not in VALID_METER_KINDS:
        raise CapabilityError(f"meter_kind must be one of {VALID_METER_KINDS}")
    if default_mode not in VALID_MODES:
        raise CapabilityError(f"default_mode must be one of {VALID_MODES}")

    if await session.get(BillingCapability, capability_id) is not None:
        raise DuplicateCapability(f"'{capability_id}' already exists")

    deps = list(depends_on or [])
    if deps:
        known = set((await session.scalars(
            select(BillingCapability.id).where(BillingCapability.id.in_(deps))
        )).all())
        missing = [d for d in deps if d not in known]
        if missing:
            # A dependency that does not exist gates the capability behind nothing resolvable, so
            # it can never be granted. Permanently unusable, and silently.
            raise CapabilityError(f"depends_on names capabilities that do not exist: {missing}")

    session.add(BillingCapability(
        id=capability_id, name=name or capability_id, description=description,
        category=category or capability_id.split(".")[0], sub_category=sub_category,
        unit=unit, meter_kind=meter_kind, default_mode=default_mode,
        depends_on=deps, active=True,
    ))
    await session.flush()

    priced = False
    margin = 0.0
    warning = ""
    if credits_per_unit is not None:
        from nexus.billing.rates import validate_rate

        cost = float(unit_cost_usd or 0.0)
        # Raises MarginFloorError below the floor. The caller maps it to 422, exactly as the rate
        # endpoint does — there must be no path that lands an underwater price.
        margin = validate_rate(
            capability_id, credits_per_unit=float(credits_per_unit), unit_cost_usd=cost,
        )
        session.add(BillingRateCard(
            capability_id=capability_id, credits_per_unit=float(credits_per_unit), active=True,
        ))
        session.add(BillingCostRate(capability_id=capability_id, unit_cost_usd=cost))
        await session.flush()
        priced = True
    elif not capability_id.startswith("module."):
        warning = (
            f"'{capability_id}' has no rate card, so anything metered against it is rated at "
            f"nothing — usage will accumulate and no revenue line will ever appear. Add a price "
            f"on the Rate cards tab."
        )

    return {
        "capability_id": capability_id,
        "priced": priced,
        "gross_margin": round(margin, 4),
        "warning": warning,
    }
```

- [ ] **Step 4: Add the endpoint**

Append to `nexus/api/routers/admin_billing_write.py`:

```python
class CapabilityIn(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    name: str
    category: str
    unit: str = "action"
    description: str = ""
    sub_category: str = ""
    meter_kind: str = "counter"
    default_mode: str = "metered"
    depends_on: list[str] = Field(default_factory=list)
    # Optional, and its absence is warned about rather than refused: a module gate is a real
    # capability with no unit price.
    credits_per_unit: float | None = None
    unit_cost_usd: float | None = None


@router.post("/capabilities", status_code=status.HTTP_201_CREATED)
async def create_capability_endpoint(
    body: CapabilityIn,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    """Add a billable capability without a deploy.

    Until this existed, `CAPABILITY_SEED` in `catalog.py` was the only way to create one, so a new
    billable action meant a code change and a release. `sync_catalog` never deletes and re-asserts
    managed fields only for ids it seeds, so an admin-created capability is untouched by any later
    deploy.
    """
    from nexus.billing.capability_authoring import (
        CapabilityError, DuplicateCapability, create_capability,
    )
    from nexus.billing.rates import MarginFloorError

    async with get_sessionmaker()() as session:
        try:
            result = await create_capability(
                session, capability_id=body.id, name=body.name, category=body.category,
                unit=body.unit, description=body.description, sub_category=body.sub_category,
                meter_kind=body.meter_kind, default_mode=body.default_mode,
                depends_on=body.depends_on,
                credits_per_unit=body.credits_per_unit, unit_cost_usd=body.unit_cost_usd,
            )
        except DuplicateCapability as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except MarginFloorError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        except CapabilityError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

        await record_admin_action(
            session, actor=principal.user_id, action="capability.create",
            target=result["capability_id"], after=result,
        )
        await session.commit()
    return result
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_capability_authoring.py -n0 -q`
Expected: PASS, 8 tests.

- [ ] **Step 6: Commit**

```bash
git add nexus/billing/capability_authoring.py nexus/api/routers/admin_billing_write.py tests/test_capability_authoring.py
git commit -m "feat(billing): create a billable capability without a deploy"
```

---

### Task 4: Meter `ai.tokens`

**Files:**
- Modify: `nexus/api/routers/agents.py`
- Test: `tests/test_token_metering.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_token_metering.py
"""`ai.tokens` was priced and metered at no call site.

The per-action capabilities charge a flat rate, which is right for a predictable bill — measured
token spread within an agent is ~4x median-to-max, which these margins absorb. `ai.tokens` exists
for the tail: it records what was actually consumed ALONGSIDE the flat charge, so the flat rate can
be checked against reality instead of assumed.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def test_an_agent_run_records_the_tokens_it_used(client, fresh_db):
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingUsageEvent

    token = await signup(client, slug="tk1", email="o@tk1.com", company="TK1")
    r = await client.post("/api/agents/research/run", headers=auth(token),
                          json={"account_id": None, "inputs": {}})
    assert r.status_code in (200, 404), r.text

    async with get_platform_sessionmaker()() as s:
        caps = [e.capability_id for e in (await s.scalars(select(BillingUsageEvent))).all()]
    if "ai.research_brief" in caps:
        assert "ai.tokens" in caps, "the flat charge landed but the token record did not"


async def test_a_run_with_no_tokens_records_nothing(fresh_db):
    """Nothing consumed, nothing recorded — the same rule that keeps an unconfigured phone lookup
    off the bill. A zero-quantity event is noise in the usage stream."""
    from nexus.billing.tokens import meter_tokens

    assert await meter_tokens(None, tokens=0, agent="research") is False


async def test_token_metering_never_breaks_the_run(fresh_db):
    """It is bookkeeping attached to work the customer is waiting on. The flat charge is the bill;
    this is the evidence."""
    from nexus.billing.tokens import meter_tokens

    class Broken:
        tenant_id = "t"

        def __getattr__(self, name):
            raise RuntimeError("session is gone")

    assert await meter_tokens(Broken(), tokens=500, agent="research") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_token_metering.py -n0 -q`
Expected: FAIL — no module `nexus.billing.tokens`.

- [ ] **Step 3: Write the helper**

```python
# nexus/billing/tokens.py
"""Record LLM token consumption against `ai.tokens`.

Priced at 0.01 credits per 1,000 tokens and metered nowhere until now: the hook existed and no call
site used it.

**This runs alongside the flat per-action charge, not instead of it.** The flat rate is what the
customer pays and what makes their bill predictable; this is the measurement that lets the flat
rate be checked against reality rather than assumed. Measured spread within an agent is ~4x
median-to-max, which these margins absorb — but that is a fact about today's prompts, and prompts
change.

Never raises. It is bookkeeping attached to work the customer is already waiting on.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.billing.tokens")

CAPABILITY = "ai.tokens"
UNIT_TOKENS = 1000.0


async def meter_tokens(ts, *, tokens: int, agent: str = "") -> bool:
    """Record ``tokens`` against the tenant. Returns whether anything was written."""
    if not tokens or tokens <= 0 or ts is None:
        # Nothing consumed, nothing recorded. A zero-quantity event is noise in the usage stream,
        # and the same rule keeps an unconfigured phone lookup off the bill.
        return False
    try:
        from nexus.billing.meter import metered

        async with metered(
            ts, CAPABILITY,
            quantity=tokens / UNIT_TOKENS,
            attrs={"agent": agent, "tokens": int(tokens)},
        ):
            return True
    except Exception:
        logger.debug("token metering skipped for %s", agent, exc_info=True)
        return False
```

- [ ] **Step 4: Call it from the agent runner**

In `nexus/api/routers/agents.py`, immediately after the `async with metered(...)` block that wraps
the agent run and before building `AgentRunResponse`:

```python
    # Record what the model actually consumed, alongside the flat per-action charge above. The
    # flat rate is the bill; this is the evidence that the flat rate is still the right one.
    from nexus.billing.tokens import meter_tokens

    await meter_tokens(ts, tokens=getattr(result, "tokens", 0) or 0, agent=agent_name)
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_token_metering.py tests/test_agents_api.py -n0 -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add nexus/billing/tokens.py nexus/api/routers/agents.py tests/test_token_metering.py
git commit -m "feat(billing): meter ai.tokens alongside the flat per-action charge"
```

---

### Task 5: Restrict the Control plane to two IPs

**Files:**
- Create: `nexus/api/deps_ip.py`
- Modify: `nexus/api/deps.py`, `nexus/runtime_config/catalog.py`
- Test: `tests/test_admin_ip_allowlist.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_ip_allowlist.py
"""Restricting the Control plane to named IPs.

The panel grants power over pricing, credentials and other people's workspaces, so origin is worth
checking on top of authentication. Two properties keep this from becoming a lockout:

* **Empty means open.** An allowlist that defaults to closed would lock every existing deployment
  out of its own admin panel on upgrade.
* **At most two entries.** Not a technical limit — a policy one, so "just add one more" is a
  decision someone makes rather than a list that grows until it means nothing.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def _admin(client, monkeypatch, *, slug: str, email: str) -> str:
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


async def test_an_empty_allowlist_lets_everyone_through(client, monkeypatch):
    """The compatibility line. A default-closed allowlist would lock every existing deployment out
    of its own panel the moment it upgraded."""
    from nexus.core.config import get_settings

    token = await _admin(client, monkeypatch, slug="ip1", email="boss@ip1.com")
    monkeypatch.setattr(get_settings(), "admin_ip_allowlist", "")
    assert (await client.get("/api/admin/billing/rates",
                             headers=auth(token))).status_code == 200


async def test_an_ip_outside_the_allowlist_is_refused(client, monkeypatch):
    from nexus.core.config import get_settings

    token = await _admin(client, monkeypatch, slug="ip2", email="boss@ip2.com")
    monkeypatch.setattr(get_settings(), "admin_ip_allowlist", "203.0.113.5")
    r = await client.get("/api/admin/billing/rates", headers=auth(token))
    assert r.status_code == 403
    # Says WHY, and shows the IP as seen. Behind a proxy the address we observe is often not the
    # one the operator expects, and without it they cannot fix their own lockout.
    assert "not permitted" in r.text.lower()


async def test_the_allowlist_admits_a_listed_ip(client, monkeypatch):
    from nexus.core.config import get_settings

    token = await _admin(client, monkeypatch, slug="ip3", email="boss@ip3.com")
    # The test client presents this as its origin.
    monkeypatch.setattr(get_settings(), "admin_ip_allowlist", "127.0.0.1,203.0.113.5")
    assert (await client.get("/api/admin/billing/rates",
                             headers=auth(token))).status_code == 200


async def test_more_than_two_entries_is_refused_at_startup(monkeypatch):
    """A policy limit, not a technical one. Three is where a list stops being a restriction."""
    import pytest

    from nexus.api.deps_ip import parse_allowlist

    with pytest.raises(ValueError):
        parse_allowlist("1.1.1.1, 2.2.2.2, 3.3.3.3")


async def test_a_malformed_entry_is_refused_rather_than_ignored(monkeypatch):
    """Silently dropping an unparseable entry turns a typo into an open panel."""
    import pytest

    from nexus.api.deps_ip import parse_allowlist

    with pytest.raises(ValueError):
        parse_allowlist("not-an-ip")


async def test_a_cidr_range_is_accepted(monkeypatch):
    """An office network is one entry, not two hundred."""
    from nexus.api.deps_ip import ip_allowed, parse_allowlist

    nets = parse_allowlist("10.0.0.0/24")
    assert ip_allowed("10.0.0.7", nets) is True
    assert ip_allowed("10.0.1.7", nets) is False


async def test_the_tenant_api_is_unaffected(client, monkeypatch):
    """This gates the CONTROL PLANE only. A restriction that also locked customers out of the
    product would be an outage wearing a security label."""
    from nexus.core.config import get_settings

    token = await signup(client, slug="ip7", email="o@ip7.com", company="IP7")
    monkeypatch.setattr(get_settings(), "admin_ip_allowlist", "203.0.113.5")
    assert (await client.get("/api/accounts", headers=auth(token))).status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_admin_ip_allowlist.py -n0 -q`
Expected: FAIL — no module `nexus.api.deps_ip`.

- [ ] **Step 3: Write the check**

```python
# nexus/api/deps_ip.py
"""Restrict the Control plane to named origins.

The panel grants power over pricing, provider credentials and other people's workspaces. Origin is
worth checking on top of authentication — a stolen admin token is worth much less if it also has to
arrive from the right network.

Two properties stop this becoming a lockout, and both are deliberate:

* **Empty means open.** Default-closed would lock every existing deployment out of its own admin
  panel the moment it upgraded. Security that ships as an outage does not get kept.
* **At most two entries.** A policy limit rather than a technical one: three is where an allowlist
  stops being a restriction and starts being a list. Use a CIDR range for an office.

The refusal names the address we actually observed. Behind a proxy that is frequently not the one
the operator expects, and without seeing it they cannot fix their own lockout.
"""
from __future__ import annotations

import ipaddress
import logging

logger = logging.getLogger("nexus.api.deps_ip")

MAX_ENTRIES = 2


def parse_allowlist(raw: str) -> list:
    """Parse a comma-separated list of addresses or CIDR ranges. Raises on anything malformed.

    Refuses rather than skipping: silently dropping an unparseable entry turns a typo into an open
    panel, which is the opposite of what the operator was trying to do.
    """
    entries = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if not entries:
        return []
    if len(entries) > MAX_ENTRIES:
        raise ValueError(
            f"the admin IP allowlist takes at most {MAX_ENTRIES} entries, got {len(entries)}. "
            f"Use a CIDR range for a network."
        )
    nets = []
    for entry in entries:
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise ValueError(f"'{entry}' is not an IP address or CIDR range") from exc
    return nets


def ip_allowed(client_ip: str, nets: list) -> bool:
    """Empty allowlist admits everyone. See the module docstring."""
    if not nets:
        return True
    if not client_ip:
        # No observable origin and a list that says otherwise: refuse. An unknown address must not
        # pass a check whose whole purpose is knowing where the request came from.
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def client_ip_of(request) -> str:
    """The caller's address, honouring one hop of X-Forwarded-For.

    Takes the FIRST entry, which is the original client. Behind our own reverse proxy that is the
    value to trust; a deployment that exposes the app directly to the internet must not enable the
    allowlist and rely on this header, because a client can set it.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or ""
```

- [ ] **Step 4: Wire it into the platform gate**

In `nexus/api/deps.py`, change `require_platform_permission`'s inner dependency to take the request
and check origin before the permission:

```python
def require_platform_permission(permission: str):
    """Gate a staff endpoint on ONE named permission, and on origin.

    The IP check runs FIRST and is deliberately cheap: it needs no database read, so an unlisted
    origin is refused before it can spend a query.
    """

    async def _dep(
        request: Request,
        principal: Principal = Depends(get_principal),
    ) -> Principal:
        from nexus.api.deps_ip import client_ip_of, ip_allowed, parse_allowlist
        from nexus.core.config import get_settings

        try:
            nets = parse_allowlist(getattr(get_settings(), "admin_ip_allowlist", "") or "")
        except ValueError:
            # A malformed allowlist must not lock the panel. Log it and fall open: the operator
            # can only fix a bad list through the panel the list would have closed.
            logger.warning("admin_ip_allowlist is malformed; ignoring it", exc_info=True)
            nets = []
        observed = client_ip_of(request)
        if not ip_allowed(observed, nets):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"the control plane is not permitted from {observed or 'an unknown address'}",
            )

        held = await platform_permissions(principal)
        if permission not in held:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "platform access denied")
        return principal

    return _dep
```

Add `from fastapi import Request` to the imports if it is not already there.

- [ ] **Step 5: Add the setting to the runtime catalog**

In `nexus/runtime_config/catalog.py`, add to `_SPECS`:

```python
    SettingSpec(
        key="admin_ip_allowlist", label="Control plane IP allowlist", group="Access", kind="str",
        effect="Comma-separated IP addresses or CIDR ranges that may reach the Control plane. "
               "Empty means any address.",
        warning="At most two entries. Get this wrong and you lock yourself out of the panel you "
                "would use to fix it — the refusal shows the address we actually observed, so "
                "check that first. Behind a proxy it is often not the one you expect. A "
                "malformed list is ignored rather than enforced, which is the deliberate escape "
                "hatch.",
        risk="high",
    ),
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_admin_ip_allowlist.py tests/test_platform_permissions.py -n0 -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add nexus/api/deps_ip.py nexus/api/deps.py nexus/runtime_config/catalog.py tests/test_admin_ip_allowlist.py
git commit -m "feat(admin): restrict the control plane to at most two origins"
```

---

### Task 6: Show which provider key is live, and whether a setting has taken effect

**Files:**
- Modify: `nexus/api/routers/admin_provider_keys.py`, `nexus/runtime_config/service.py`
- Modify: `frontend/src/lib/types.ts`, `frontend/src/pages/admin/ProviderKeysTab.tsx`, `frontend/src/pages/admin/RuntimeConfigTab.tsx`
- Test: `tests/test_provider_keys_api.py` (append), `tests/test_runtime_config.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_keys_api.py`:

```python
async def test_the_key_list_says_which_one_is_actually_in_use(client, monkeypatch):
    """A pool of five keys and no indication which one is serving traffic is a panel that shows
    state without showing the state that matters.

    Computed from the SAME ordering the resolver uses — pinned first, then oldest — so the light
    cannot disagree with what is really happening.
    """
    token = await _superadmin(client, monkeypatch, slug="iu1", email="boss@iu1.com")
    first = (await client.post("/api/admin/provider-keys", headers=auth(token),
                               json={"provider": "brave", "label": "a",
                                     "key": "sk-a-1111"})).json()
    second = (await client.post("/api/admin/provider-keys", headers=auth(token),
                                json={"provider": "brave", "label": "b",
                                      "key": "sk-b-2222"})).json()

    rows = (await client.get("/api/admin/provider-keys?provider=brave",
                             headers=auth(token))).json()
    by_id = {r["id"]: r for r in rows}
    assert by_id[first["id"]]["in_use"] is True, "oldest enabled key serves by default"
    assert by_id[second["id"]]["in_use"] is False

    # Pinning moves the light.
    await client.post(f"/api/admin/provider-keys/{second['id']}/prefer", headers=auth(token))
    rows = (await client.get("/api/admin/provider-keys?provider=brave",
                             headers=auth(token))).json()
    by_id = {r["id"]: r for r in rows}
    assert by_id[second["id"]]["in_use"] is True
    assert by_id[first["id"]]["in_use"] is False


async def test_a_disabled_key_is_never_shown_as_in_use(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="iu2", email="boss@iu2.com")
    only = (await client.post("/api/admin/provider-keys", headers=auth(token),
                              json={"provider": "serper", "label": "x",
                                    "key": "sk-x-3333"})).json()
    await client.post(f"/api/admin/provider-keys/{only['id']}/enabled/false",
                      headers=auth(token))
    rows = (await client.get("/api/admin/provider-keys?provider=serper",
                             headers=auth(token))).json()
    assert rows[0]["in_use"] is False
```

Append to `tests/test_runtime_config.py`:

```python
async def test_a_setting_reports_whether_it_has_taken_effect(client, monkeypatch):
    """"Saved" and "in force" are different facts, and a panel that shows only the first is how an
    operator concludes a feature is on when it is not."""
    token = await _superadmin(client, monkeypatch, slug="rc20", email="boss@rc20.com")
    await client.put("/api/admin/runtime/settings/cadence_enabled", headers=auth(token),
                     json={"value": True})

    rows = (await client.get("/api/admin/runtime/settings", headers=auth(token))).json()
    row = next(r for r in rows if r["key"] == "cadence_enabled")
    assert row["overridden"] is True
    assert row["in_effect"] is True, "set on this process, so it is live here"


async def test_a_restart_only_setting_reports_stored_but_not_live(client, monkeypatch):
    """`metrics_enabled` is read once at startup. Saying it is live would be a lie the operator
    only discovers when the thing they enabled is still missing."""
    from nexus.core.config import get_settings

    token = await _superadmin(client, monkeypatch, slug="rc21", email="boss@rc21.com")
    await client.put("/api/admin/runtime/settings/metrics_enabled", headers=auth(token),
                     json={"value": False})
    # Simulate a process that read the value at boot and holds the old one.
    monkeypatch.setattr(get_settings(), "metrics_enabled", True)

    rows = (await client.get("/api/admin/runtime/settings", headers=auth(token))).json()
    row = next(r for r in rows if r["key"] == "metrics_enabled")
    assert row["overridden"] is True
    assert row["in_effect"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_provider_keys_api.py tests/test_runtime_config.py -n0 -q -k "in_use or in_effect or taken_effect or actually_in_use"`
Expected: FAIL — `KeyError: 'in_use'`.

- [ ] **Step 3: Add `in_use` to the key list**

In `nexus/api/routers/admin_provider_keys.py`, add `in_use: bool = False` to `ProviderKeyOut`, and
in `list_provider_keys` compute it after loading rows:

```python
    # Which key is actually serving. Computed from the SAME ordering the resolver uses —
    # `preferred` first, then `created_at` — so the indicator cannot disagree with reality. A
    # panel showing five keys and no sign of which one is live shows state without showing the
    # state that matters.
    live: dict[str, str] = {}
    for row in sorted(rows, key=lambda r: (not r.preferred, r.created_at)):
        if row.enabled and row.provider not in live:
            live[row.provider] = row.id
```

Then set `in_use=(live.get(row.provider) == row.id)` when building each `ProviderKeyOut`.

- [ ] **Step 4: Add `in_effect` to the settings list**

In `nexus/runtime_config/service.py`, inside `current_values`, replace the appended dict's tail:

```python
        stored = raw.get(spec.key)
        live = getattr(settings, spec.key, None)
        # "Saved" and "in force" are different facts. A restart-only setting is stored and pending;
        # saying it is live would be a lie the operator only discovers when the thing they enabled
        # is still missing.
        in_effect = True
        if stored is not None:
            try:
                in_effect = coerce(spec, stored) == live
            except Exception:
                in_effect = False
        out.append({
            ...
            "value": live,
            "overridden": spec.key in raw,
            "in_effect": in_effect,
            "note": row.note if row is not None else "",
        })
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_provider_keys_api.py tests/test_runtime_config.py -n0 -q`
Expected: PASS.

- [ ] **Step 6: Add the indicators to the UI**

In `frontend/src/lib/types.ts`, add `in_use: boolean;` to `ProviderKey` and `in_effect: boolean;`
to `RuntimeSetting`.

In `ProviderKeysTab.tsx`, inside `.keyHead` before the status badge:

```tsx
                  {row.in_use && (
                    <Badge tone="success" dot>
                      In use
                    </Badge>
                  )}
```

In `RuntimeConfigTab.tsx`, inside `.head` after the `overridden` badge:

```tsx
        {row.overridden && !row.in_effect && (
          <Badge tone="warning" dot>
            Saved, not yet live
          </Badge>
        )}
```

- [ ] **Step 7: Typecheck and build**

Run: `cd frontend && npx tsc --noEmit && npm run build`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add nexus/api/routers/admin_provider_keys.py nexus/runtime_config/service.py frontend/src/lib/types.ts frontend/src/pages/admin/ProviderKeysTab.tsx frontend/src/pages/admin/RuntimeConfigTab.tsx tests/
git commit -m "feat(admin): show which key is live and whether a setting has taken effect"
```

---

### Task 7: Full suite, deploy, verify

- [ ] **Step 1: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: all pass. Roughly 45 minutes.

- [ ] **Step 2: Build and deploy**

```bash
cd frontend && npm run build && cd ../deploy
docker compose -f docker-compose.prod.yml build app worker
docker compose -f docker-compose.prod.yml up -d --force-recreate app worker
docker start nexus-direct
```

- [ ] **Step 3: Apply the ladder restructure**

Run: `docker compose -f deploy/docker-compose.prod.yml exec -T app python scripts/restructure_plans.py --apply`

- [ ] **Step 4: Verify the price list**

Expected: exactly Free, Launch, Launch (annual), Accelerate, Accelerate (annual).

- [ ] **Step 5: Verify the three existing subscribers still resolve**

Expected: `core` and `growth` subscribers still `active`, entitlements still resolving.

- [ ] **Step 6: Commit and report**

---

## Self-Review

**Spec coverage.** Free/Launch/Accelerate + 20% annuals → Tasks 1–2. Why a capability needs a deploy,
and the fix → Task 3 plus the section above. `ai.tokens` metering → Task 4. Two-IP restriction →
Task 5. Live-key indicator and setting-in-effect indicator → Task 6. Deployable build → Task 7.

**Placeholder scan.** No TBDs; every code step carries the code.

**Type consistency.** `create_capability` returns `{capability_id, priced, gross_margin, warning}` and
the endpoint returns it unchanged; the tests assert those four keys. `parse_allowlist` returns a list
of networks consumed by `ip_allowed(client_ip, nets)`; both tests and `deps.py` use that signature.
`meter_tokens(ts, *, tokens, agent)` returns `bool` in the helper, the tests and the call site.

**Risk called out.** Task 2 is the only step that touches live customer data. It retires and never
deletes, has a dry run, and is idempotent.
