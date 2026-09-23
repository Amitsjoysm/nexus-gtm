# tests/test_credit_usage_report.py
"""Where did my credits go?

`/billing/usage` reported per-capability ACTION COUNTS and nothing about credits — `enrich.account:
used 40` is really 120 credits at 3 per action, and nothing on the screen said so. A customer
watching a balance fall had no way to find out what was spending it.

Three views, because they answer three different questions:

* **by capability** — "what is eating my balance?" Sorted by spend, because the top two lines are
  almost always the whole answer and an alphabetical list buries them.
* **by day** — "why did it drop on Tuesday?" A bulk import or a crawl shows up as a spike, and a
  total alone cannot distinguish that from steady use.
* **by user** — "who is spending it?" ATTRIBUTION IS PARTIAL BY CONSTRUCTION: background work
  (refresh sweeps, crawls, plays) has no user to attribute to, so the per-user numbers do not sum
  to the total. That gap has to be reported as its own line rather than silently dropped, or the
  screen quietly lies about a number people will check.
"""
from __future__ import annotations

import pytest

from nexus.models.identity import Tenant


async def _spend(tenant_id: str, capability_id: str, *, n: int = 1, user_id: str | None = None):
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    async with get_sessionmaker()() as s:
        ts = TenantSession(s, tenant_id)
        for i in range(n):
            await check_and_meter(
                ts, capability_id=capability_id, quantity=1, user_id=user_id,
                idempotency_key=f"{capability_id}:{user_id}:{i}",
            )
        await s.commit()


@pytest.fixture
async def workspace(fresh_db, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.credits import grant_credits
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.billing.subscriptions import ensure_subscription
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")

    async with get_sessionmaker()() as s:
        t = Tenant(name="Report", slug="report")
        s.add(t)
        await s.flush()
        ts = TenantSession(s, t.id)
        await ensure_subscription(ts, plan_id="accelerate")
        await grant_credits(ts, 5000, kind="grant", reason="test", idempotency_key="seed")
        await s.commit()
        return t.id


async def _report(tenant_id: str):
    from nexus.billing.usage_report import credit_usage_report
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    async with get_sessionmaker()() as s:
        return await credit_usage_report(TenantSession(s, tenant_id))


# ---- by capability -----------------------------------------------------------------------------

async def test_it_reports_credits_not_just_action_counts(workspace):
    """THE gap. `used: 40` for enrich.account is really 120 credits, and nothing said so."""
    await _spend(workspace, "enrich.account", n=4)
    report = await _report(workspace)

    row = next(r for r in report["by_capability"] if r["capability_id"] == "enrich.account")
    assert row["actions"] == 4
    assert row["credits"] > 0
    assert row["credits"] != row["actions"], "credits and actions must be separate numbers"


async def test_capabilities_are_sorted_by_spend(workspace):
    """The top two lines are almost always the whole answer; alphabetical buries them."""
    await _spend(workspace, "enrich.account", n=5)     # 3 credits each
    await _spend(workspace, "verify.email", n=2)       # 0.25 each
    report = await _report(workspace)

    spends = [r["credits"] for r in report["by_capability"]]
    assert spends == sorted(spends, reverse=True), spends
    assert report["by_capability"][0]["capability_id"] == "enrich.account"


async def test_a_capability_that_spent_nothing_is_omitted(workspace):
    """Sixty rows of zero bury the handful that matter — the same reason the admin customer
    directory reports only what was actually used."""
    await _spend(workspace, "enrich.account", n=1)
    report = await _report(workspace)
    assert all(r["credits"] > 0 for r in report["by_capability"])


async def test_the_totals_reconcile_with_the_balance(workspace):
    """A report whose numbers do not add up to the balance is worse than no report."""
    from nexus.billing.credits import balance
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    await _spend(workspace, "enrich.account", n=3)
    report = await _report(workspace)

    async with get_sessionmaker()() as s:
        live = await balance(TenantSession(s, workspace))
    assert report["balance"] == pytest.approx(live), "the report must agree with the real balance"
    assert report["spent"] == pytest.approx(sum(r["credits"] for r in report["by_capability"])), (
        "the capability rows must add up to the total spend, or the screen cannot be reconciled"
    )
    # NOT `granted - spent == balance`: those are PERIOD figures, and a balance carried over from
    # an earlier period is real money. The identity only holds for a workspace that started this
    # period at zero, which is not the general case.


# ---- by day ------------------------------------------------------------------------------------

async def test_it_reports_a_daily_timeline(workspace):
    """'Why did it drop on Tuesday?' — a total cannot distinguish a bulk import from steady use."""
    await _spend(workspace, "enrich.account", n=2)
    report = await _report(workspace)

    assert report["by_day"], "no daily breakdown"
    day = report["by_day"][0]
    assert "date" in day and "credits" in day
    assert sum(d["credits"] for d in report["by_day"]) == pytest.approx(report["spent"])


# ---- by user -----------------------------------------------------------------------------------

async def test_it_attributes_spend_to_users(workspace):
    await _spend(workspace, "enrich.account", n=2, user_id="u-alice")
    await _spend(workspace, "verify.email", n=4, user_id="u-bob")
    report = await _report(workspace)

    users = {r["user_id"]: r["credits"] for r in report["by_user"]}
    assert users.get("u-alice", 0) > 0
    assert users.get("u-bob", 0) > 0


async def test_unattributed_background_work_is_shown_not_hidden(workspace):
    """ATTRIBUTION IS PARTIAL BY CONSTRUCTION. Refresh sweeps, crawls and plays have no user, so
    the per-user rows cannot sum to the total. Dropping the difference would make the screen
    quietly lie about a number people will check against their balance."""
    await _spend(workspace, "enrich.account", n=2, user_id="u-alice")
    await _spend(workspace, "enrich.account", n=3, user_id=None)      # background
    report = await _report(workspace)

    assert report["unattributed_credits"] > 0
    by_user = sum(r["credits"] for r in report["by_user"])
    assert by_user + report["unattributed_credits"] == pytest.approx(report["spent"]), (
        "per-user spend plus the unattributed remainder must equal the total, or the screen "
        "cannot be reconciled against the balance"
    )


async def test_an_empty_workspace_reports_zeroes_not_an_error(workspace):
    """A brand-new workspace opening the page must see an empty report, not a failure."""
    report = await _report(workspace)
    assert report["spent"] == 0
    assert report["by_capability"] == []
    assert report["by_user"] == []


# ---- grants the report must not lose --------------------------------------------------------

async def test_a_support_grant_appears_in_the_report(workspace):
    """Found by seeding a real workspace and reading the numbers back.

    `POST /admin/billing/tenants/{id}/credits` — the goodwill grant, support's single most common
    action — calls `grant_credits` WITHOUT a `period_key`, so the row lands with NULL. The report
    filtered the ledger on `period_key == period`, so those credits raised the balance and appeared
    nowhere: the screen would read "granted 2,000, spent 293" beside a balance 500 higher than
    those two numbers can explain.

    That is precisely the reconciliation failure this report exists to prevent, and the worst
    version of it — support has just told the customer the credits are there.

    Note the report was already internally inconsistent about this: the per-capability ACTION loop
    accepts `period_key in (None, period)` while the ledger query did not.
    """
    from nexus.billing.credits import balance, grant_credits
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    await _spend(workspace, "enrich.account", n=2)

    async with get_sessionmaker()() as s:
        ts = TenantSession(s, workspace)
        # Exactly how the admin endpoint calls it: no period_key.
        await grant_credits(ts, 500, kind="adjustment", reason="goodwill",
                            idempotency_key="support-1")
        await s.commit()

    report = await _report(workspace)
    async with get_sessionmaker()() as s:
        live = await balance(TenantSession(s, workspace))

    assert report["balance"] == pytest.approx(live)
    assert report["granted"] >= 500, (
        f"a support grant of 500 is missing from the report (granted={report['granted']}); "
        "the customer's balance moved and the screen cannot explain why"
    )


async def test_a_grant_from_an_earlier_period_is_not_counted_as_this_one(workspace):
    """The fix must not go the other way. An unkeyed row is attributed by its OWN date, not swept
    into whatever period happens to be open — otherwise last quarter's grant inflates this
    month's figure and the report is wrong in a new direction."""
    from datetime import timedelta

    from nexus.billing.credits import grant_credits
    from nexus.core.db import get_sessionmaker, utcnow
    from nexus.core.tenancy import TenantSession
    from nexus.models.billing import BillingCreditLedger

    before = (await _report(workspace))["granted"]

    async with get_sessionmaker()() as s:
        ts = TenantSession(s, workspace)
        await grant_credits(ts, 777, kind="adjustment", reason="old goodwill",
                            idempotency_key="support-old")
        await s.flush()
        row = await ts.first(
            BillingCreditLedger, BillingCreditLedger.idempotency_key == "support-old"
        )
        row.created_at = utcnow() - timedelta(days=70)
        await s.commit()

    after = (await _report(workspace))["granted"]
    assert after == pytest.approx(before), (
        f"a grant from ~70 days ago moved this period's total from {before} to {after}"
    )


# ---- who spent it: a person, not an id ---------------------------------------------------------
#
# The screen read `email` off each row and fell back to `user_id` — and the server never sent an
# email, so every customer saw rows like "4a88e2a9a2e8438f96f3295fe51ccebd" under "Who used them".


async def _member(tenant_id: str, email: str, full_name: str) -> str:
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Membership, User

    async with get_sessionmaker()() as s:
        user = User(email=email, full_name=full_name, password_hash="x")
        s.add(user)
        await s.flush()
        s.add(Membership(tenant_id=tenant_id, user_id=user.id, role="rep"))
        await s.commit()
        return user.id


async def test_each_row_names_the_person_not_their_id(workspace):
    alice = await _member(workspace, "alice@report.test", "Alice Adams")
    await _spend(workspace, "enrich.account", n=2, user_id=alice)

    (row,) = (await _report(workspace))["by_user"]

    assert row["name"] == "Alice Adams"
    assert row["email"] == "alice@report.test"


async def test_someone_who_left_reads_as_a_former_member_not_an_id(workspace):
    # Their spend is real and stays on the report; their identity is no longer this workspace's.
    await _spend(workspace, "enrich.account", n=1, user_id="4a88e2a9a2e8438f96f3295fe51ccebd")

    (row,) = (await _report(workspace))["by_user"]

    assert row["name"] == "Former member"
    assert row["email"] == ""


async def test_a_member_of_another_workspace_is_not_named_here(workspace):
    # Names resolve through THIS workspace's membership, never the global user table alone.
    from nexus.core.db import get_sessionmaker

    async with get_sessionmaker()() as s:
        other = Tenant(name="Other", slug="other")
        s.add(other)
        await s.commit()
        other_id = other.id
    stranger = await _member(other_id, "eve@other.test", "Eve Other")
    await _spend(workspace, "enrich.account", n=1, user_id=stranger)

    (row,) = (await _report(workspace))["by_user"]

    assert row["name"] == "Former member", "a person from another workspace was named here"
    assert row["email"] == ""


def test_the_response_model_carries_the_name_and_email():
    # Pydantic drops any key the model does not declare — which is exactly how the email the
    # screen expected never arrived.
    from nexus.api.routers.billing import CreditUserRowOut

    assert {"name", "email"} <= set(CreditUserRowOut.model_fields)


def test_the_screen_never_displays_the_raw_id():
    # There is no frontend test runner, so this reads the source, like the other UI tests.
    import re
    from pathlib import Path

    src = Path("frontend/src/pages/billing/CreditUsageReport.tsx").read_text(encoding="utf-8")
    uses = re.findall(r"user\.user_id", src)
    assert uses == ["user.user_id"], "the raw user id is rendered somewhere other than the list key"
    assert "key={user.user_id}" in src
