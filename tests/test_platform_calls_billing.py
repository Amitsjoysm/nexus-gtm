"""Platform calls: every one is charged even if nobody logs it, and each workspace has its own number.

Two decisions with the product owner, 2026-09-23:

* **A sweep charges unlogged calls.** A platform call was charged only when the rep logged its
  outcome, so a call nobody logged was free. Every live platform call is now recorded when dialled
  (`placed_calls`, migration 0059); two hours on, a sweep charges any still uncharged its measured
  minutes, once. Keyed on the call id like the disposition charge, so the two can never both charge.
* **A superadmin assigns each workspace a caller ID on the platform account.** Otherwise every
  customer's prospects saw one number, and one heavy customer's volume getting it flagged as spam
  lowered answer rates for everyone. `tenants.platform_caller_id`, set from the customer directory
  under `providers.manage`, and checked against the platform account before it is saved.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.calling.provider import CallHandle, CallProvider, set_call_provider
from nexus.core.config import get_settings
from nexus.core.db import utcnow
from nexus.core.security import decode_access_token
from nexus.models.account import Account, Contact
from tests.conftest import auth, signup, tenant_session

CALL_SID = "CA" + "7" * 32


class _FakeTwilio(CallProvider):
    name = "twilio"

    def __init__(self, *, status="completed", seconds=125, owns=True):
        self.placed = 0
        self.status = status
        self.seconds = seconds
        self.owns = owns
        self.account_sid = "AC" + "1" * 32

    async def place_call(self, *, to, from_, context=None):
        self.placed += 1
        self.last_from = from_
        return CallHandle(mode="live", provider_call_id=CALL_SID)

    async def get_call_status(self, provider_call_id):
        if self.status is None:
            return None
        return {"status": self.status, "duration_s": self.seconds}

    async def get_recording(self, provider_call_id):
        return None

    async def get_transcript(self, provider_call_id):
        return None

    async def check_account(self, *, caller_id=None):
        return (self.owns, "ok" if self.owns else f"does not own {caller_id}")


@pytest.fixture
async def billing(fresh_db):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates

    await sync_catalog()
    await sync_plans()
    await sync_rates()


@pytest.fixture
def platform(monkeypatch):
    monkeypatch.setattr(get_settings(), "telephony_from_number", "+15557770000")
    fake = _FakeTwilio()
    set_call_provider(fake)
    yield fake
    set_call_provider(None)


async def _workspace(client, slug):
    token = await signup(client, slug=slug, email=f"o@{slug}.x", company=slug.upper())
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
        ts.add(acc)
        await ts.flush()
        c = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane", phone="+15551234567")
        ts.add(c)
        await ts.flush()
    r = await client.post("/api/calling/tasks", headers=auth(token),
                          json={"account_id": acc.id, "contact_id": c.id})
    return token, tid, r.json()["id"]


async def _dial(client, token, task):
    r = await client.post(f"/api/calling/tasks/{task}/dial", headers=auth(token),
                          json={"agent_number": "+15559998888"})
    assert r.status_code == 200, r.text


async def _placed(tid):
    from nexus.models.calling import PlacedCall

    async with tenant_session(tid) as ts:
        return await ts.list(PlacedCall)


async def _minutes(tid) -> float:
    from sqlalchemy import func, select

    from nexus.models.billing import BillingUsageEvent

    async with tenant_session(tid) as ts:
        return float(await ts.session.scalar(
            select(func.coalesce(func.sum(BillingUsageEvent.quantity), 0)).where(
                BillingUsageEvent.tenant_id == tid,
                BillingUsageEvent.capability_id == "calling.minutes",
            )
        ) or 0)


async def _age(tid, hours):
    from nexus.models.calling import PlacedCall

    async with tenant_session(tid) as ts:
        for row in await ts.list(PlacedCall):
            row.placed_at = utcnow() - timedelta(hours=hours)


async def _sweep():
    from nexus.workers.tasks import handle_charge_unlogged_calls

    return await handle_charge_unlogged_calls({})


# ---- every platform call is recorded, and charged exactly once --------------------------------


async def test_a_platform_dial_is_recorded_with_who_placed_it(client, billing, platform):
    token, tid, task = await _workspace(client, "pc1")
    await _dial(client, token, task)

    (row,) = await _placed(tid)
    assert row.provider_call_id == CALL_SID and row.source == "platform"
    assert row.user_id == decode_access_token(token)["sub"]
    assert row.charged_at is None


async def test_an_unlogged_call_is_charged_by_the_sweep_once(client, billing, platform):
    token, tid, task = await _workspace(client, "pc2")
    await _dial(client, token, task)
    await _age(tid, 3)

    await _sweep()
    await _sweep()

    assert await _minutes(tid) == 3, "125 seconds is three started minutes, charged once"
    (row,) = await _placed(tid)
    assert row.charged_at is not None and row.minutes == 3


async def test_a_logged_call_is_not_charged_again_by_the_sweep(client, billing, platform):
    token, tid, task = await _workspace(client, "pc3")
    await _dial(client, token, task)
    await client.post(f"/api/calling/tasks/{task}/disposition", headers=auth(token),
                      json={"disposition": "connected", "provider_call_id": CALL_SID})
    await _age(tid, 3)

    await _sweep()

    assert await _minutes(tid) == 3


@pytest.mark.parametrize("hours, status", [(1, "completed"), (3, "in-progress")])
async def test_the_sweep_leaves_recent_and_live_calls_alone(client, billing, platform, hours, status):
    platform.status = status
    token, tid, task = await _workspace(client, f"pc4{hours}")
    await _dial(client, token, task)
    await _age(tid, hours)

    await _sweep()

    assert await _minutes(tid) == 0
    (row,) = await _placed(tid)
    assert row.charged_at is None


async def test_a_call_twilio_cannot_describe_is_given_up_after_a_week(client, billing, platform):
    platform.status = None
    token, tid, task = await _workspace(client, "pc5")
    await _dial(client, token, task)
    await _age(tid, 24 * 8)

    await _sweep()

    assert await _minutes(tid) == 0, "nothing measured, nothing charged"
    (row,) = await _placed(tid)
    assert row.charged_at is not None and row.minutes is None


async def test_a_call_on_the_workspaces_own_twilio_is_not_recorded(client, billing, monkeypatch):
    from nexus.calling import connection

    fake = _FakeTwilio()

    async def own(ts):
        return connection.ResolvedTelephony(provider=fake, from_number="+15550001111",
                                            source="workspace")

    monkeypatch.setattr(connection, "resolve_call_provider", own)
    token, tid, task = await _workspace(client, "pc6")
    await _dial(client, token, task)

    assert await _placed(tid) == []


# ---- a caller ID per workspace on the platform account -----------------------------------------


async def _superadmin(client, monkeypatch, slug):
    email = f"boss@{slug}.com"
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


async def test_a_workspace_calls_from_the_number_assigned_to_it(client, billing, platform, monkeypatch):
    boss = await _superadmin(client, monkeypatch, "pcboss1")
    token, tid, task = await _workspace(client, "pc7")

    r = await client.put(f"/api/admin/billing/customers/{tid}/caller-id", headers=auth(boss),
                         json={"from_number": "+15550009999"})
    assert r.status_code == 200, r.text

    await _dial(client, token, task)
    assert platform.last_from == "+15550009999"
    status = (await client.get("/api/calling/telephony", headers=auth(token))).json()
    assert status["from_number"] == "+15550009999"


async def test_without_an_assignment_the_platform_number_is_used(client, billing, platform):
    token, tid, task = await _workspace(client, "pc8")
    await _dial(client, token, task)
    assert platform.last_from == "+15557770000"


async def test_a_number_the_platform_account_does_not_own_is_refused(
    client, billing, platform, monkeypatch,
):
    platform.owns = False
    boss = await _superadmin(client, monkeypatch, "pcboss2")
    _, tid, _ = await _workspace(client, "pc9")

    r = await client.put(f"/api/admin/billing/customers/{tid}/caller-id", headers=auth(boss),
                         json={"from_number": "+15550009999"})

    assert r.status_code == 400
    assert "does not own" in r.json()["detail"]


async def test_only_a_provider_manager_assigns_a_caller_id(client, billing, platform):
    token, tid, _ = await _workspace(client, "pc10")

    r = await client.put(f"/api/admin/billing/customers/{tid}/caller-id", headers=auth(token),
                         json={"from_number": "+15550009999"})

    # 404, not 403: every staff endpoint answers a non-admin exactly as it answers an invented
    # route, so the staff surface cannot be mapped without a credential (require_platform_permission).
    assert r.status_code == 404, "a workspace owner reached the platform's caller-ID endpoint"


async def test_clearing_the_assignment_returns_to_the_platform_number(
    client, billing, platform, monkeypatch,
):
    boss = await _superadmin(client, monkeypatch, "pcboss3")
    token, tid, task = await _workspace(client, "pc11")
    await client.put(f"/api/admin/billing/customers/{tid}/caller-id", headers=auth(boss),
                     json={"from_number": "+15550009999"})

    r = await client.put(f"/api/admin/billing/customers/{tid}/caller-id", headers=auth(boss),
                         json={"from_number": ""})

    assert r.status_code == 200
    await _dial(client, token, task)
    assert platform.last_from == "+15557770000"


def test_the_customer_directory_offers_the_caller_id():
    # No frontend test runner, so this reads the source, like the other UI tests.
    from pathlib import Path

    src = Path("frontend/src/pages/admin/CustomersTab.tsx").read_text(encoding="utf-8")
    assert "<CallerId row={row}" in src and "setCustomerCallerId" in src
