# tests/test_plan_families.py
"""Which listed plans are the same tier billed monthly and yearly.

An annual plan is its own row, because `interval` lives on the plan, so nothing in the schema said
`launch-annual` is Launch paid yearly and the price list rendered five cards for three tiers. The
picker now shows one card per tier behind an Annual | Monthly switch, and needs the SERVER to say
which rows belong together: guessing it in the browser from ids or names would put a pricing rule
where no test reaches, and admins author plans with arbitrary ids (`yr-lite`).

Derived on read (`interval_pairs`), not stored: `sync_plans` never mutates an existing row, so a
seeded column would reach fresh installs and never the deployments already selling these plans.
"""
from __future__ import annotations

from types import SimpleNamespace

from tests.conftest import auth, principal_from_token, put_on_plan, signup


async def _workspace(client, slug: str, *, email: str | None = None) -> str:
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()
    return await signup(client, slug=slug, email=email or f"o@{slug}.com", company=slug.upper())


async def _superadmin(client, monkeypatch, slug: str) -> str:
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    email = f"boss@{slug}.com"
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    token = await _workspace(client, slug, email=email)
    await sync_rates()
    return token


async def _author(client, token: str, **body) -> None:
    plan = {"name": body["plan_id"].title(), "base_plan_id": "accelerate",
            "included_credits": 12000, "status": "active", **body}
    r = await client.post("/api/admin/billing/plans", headers=auth(token), json=plan)
    assert r.status_code == 201, r.text


async def _price_list(client, token: str) -> dict[str, dict]:
    r = await client.get("/api/billing/plans", headers=auth(token))
    assert r.status_code == 200, r.text
    return {p["id"]: p for p in r.json()}


# ---- the seeded ladder ---------------------------------------------------------------------------

async def test_the_seeded_annuals_pair_with_their_monthly_tier_both_ways(client):
    rows = await _price_list(client, await _workspace(client, "pf1"))

    for monthly, annual in (("launch", "launch-annual"), ("accelerate", "accelerate-annual")):
        assert rows[monthly]["family"] == monthly
        assert rows[annual]["family"] == monthly, f"{annual} is not grouped with {monthly}"
        assert rows[monthly]["counterpart_id"] == annual
        assert rows[annual]["counterpart_id"] == monthly


async def test_free_is_its_own_family_with_no_pair(client):
    """Free is shown in both views unchanged, so it must not borrow a counterpart."""
    rows = await _price_list(client, await _workspace(client, "pf2"))
    assert rows["free"]["family"] == "free"
    assert rows["free"]["counterpart_id"] is None


async def test_current_stays_per_plan_within_a_family(client):
    """The picker tells "on Launch, billed monthly" apart from "on Launch, billed yearly" from
    `current`, so grouping must not smear it across the family."""
    token = await _workspace(client, "pf3")
    await put_on_plan(principal_from_token(token).tenant_id, "launch")
    rows = await _price_list(client, token)

    assert rows["launch"]["current"] is True
    assert rows["launch-annual"]["current"] is False
    assert rows["launch"]["family"] == rows["launch-annual"]["family"]


# ---- plans an admin authors ----------------------------------------------------------------------

async def test_an_unpaired_admin_authored_annual_plan_still_lists(client, monkeypatch):
    """A yearly-only tier is a legitimate thing to sell. It must stay on the price list as its own
    family rather than vanish for want of a monthly sibling."""
    token = await _superadmin(client, monkeypatch, "pf4")
    await _author(client, token, plan_id="yr-lite", interval="year", base_price_cents=49000)

    rows = await _price_list(client, token)
    assert "yr-lite" in rows
    assert rows["yr-lite"]["interval"] == "year"
    assert rows["yr-lite"]["family"] == "yr-lite"
    assert rows["yr-lite"]["counterpart_id"] is None


async def test_an_admin_authored_pair_follows_the_same_rule_as_the_seed(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, "pf5")
    await _author(client, token, plan_id="scale", base_price_cents=24900)
    await _author(client, token, plan_id="scale-annual", interval="year", base_price_cents=239000)

    rows = await _price_list(client, token)
    assert rows["scale"]["counterpart_id"] == "scale-annual"
    assert rows["scale-annual"]["counterpart_id"] == "scale"
    assert rows["scale-annual"]["family"] == "scale"


async def test_holding_the_monthly_tier_leaves_its_annual_unpaired(client, monkeypatch):
    """Pairing is computed over what is on sale. Pointing a card at a held plan would put a
    checkout button in front of a 409."""
    token = await _superadmin(client, monkeypatch, "pf6")
    await _author(client, token, plan_id="scale", base_price_cents=24900)
    await _author(client, token, plan_id="scale-annual", interval="year", base_price_cents=239000)
    r = await client.put("/api/admin/billing/plans/scale/status", headers=auth(token),
                         json={"status": "draft"})
    assert r.status_code == 200, r.text

    rows = await _price_list(client, token)
    assert "scale" not in rows
    assert rows["scale-annual"]["family"] == "scale-annual"
    assert rows["scale-annual"]["counterpart_id"] is None


# ---- the rule itself -----------------------------------------------------------------------------

def _row(plan_id: str, interval: str = "month", currency: str = "USD") -> SimpleNamespace:
    return SimpleNamespace(id=plan_id, interval=interval, currency=currency)


def test_the_suffix_alone_does_not_make_a_pair():
    """An id is a hint; the intervals are the fact. `x-annual` billed monthly is not the yearly
    version of anything."""
    from nexus.billing.plans import IntervalPair, interval_pairs

    wrong_way = interval_pairs([_row("x"), _row("x-annual", "month")])
    assert wrong_way["x"] == IntervalPair("x", None)
    assert wrong_way["x-annual"] == IntervalPair("x-annual", None)

    both_yearly = interval_pairs([_row("y", "year"), _row("y-annual", "year")])
    assert both_yearly["y-annual"] == IntervalPair("y-annual", None)


def test_two_currencies_do_not_pair():
    """"Save 20%" between a dollar price and a euro price is not a number anyone can state."""
    from nexus.billing.plans import IntervalPair, interval_pairs

    pairs = interval_pairs([_row("z", currency="USD"), _row("z-annual", "year", currency="EUR")])
    assert pairs["z-annual"] == IntervalPair("z-annual", None)


def test_currency_case_does_not_split_a_pair():
    from nexus.billing.plans import IntervalPair, interval_pairs

    pairs = interval_pairs([_row("w", currency="usd"), _row("w-annual", "year", currency="USD")])
    assert pairs["w"] == IntervalPair("w", "w-annual")
