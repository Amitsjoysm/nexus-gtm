# tests/test_seat_limits.py
"""Seats are sold per plan, so a plan's seat count has to stop someone adding the next member.

Two fields claim to hold this line and neither did.

`billing_plans.max_seats` is read only by admin response models and write bodies — it is stored,
displayed in the console, and checked by nothing. `seat.member` is better: it is a GAUGE, so it
resolves to live membership rather than to a counter that only climbs, which is what lets a
workspace get back under a limit by removing someone. But `POST /workspace/members` never called
the metering seam, so the gauge was never consulted either.

The result was a free workspace, sold with one seat, able to add as many members as it liked.

The ladder itself: Free 1, Launch 5, Accelerate 15, and the annuals carry the same plan with more
people on it — Launch Annual 15, Accelerate Annual 35. Custom plans set their own.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session


EXPECTED_SEATS = {
    "free": 1,
    "launch": 5,
    "launch-annual": 15,          # 5 + 10
    "accelerate": 15,
    "accelerate-annual": 35,      # 15 + 20
}


def _seed_seats() -> dict[str, int | None]:
    from nexus.billing.plans import PLAN_SEED, seat_quota_for

    return {p["id"]: seat_quota_for(p["id"]) for p in PLAN_SEED}


class TestTheLadder:
    @pytest.mark.parametrize("plan_id,seats", sorted(EXPECTED_SEATS.items()))
    def test_each_plan_sells_the_agreed_number_of_seats(self, plan_id, seats):
        assert _seed_seats().get(plan_id) == seats

    def test_an_annual_plan_carries_more_people_than_its_monthly_sibling(self):
        """Annual is a commitment, so it buys room to grow into rather than the same team."""
        seats = _seed_seats()
        assert seats["launch-annual"] > seats["launch"]
        assert seats["accelerate-annual"] > seats["accelerate"]

    def test_the_ladder_only_goes_up(self):
        seats = _seed_seats()
        order = ["free", "launch", "accelerate"]
        values = [seats[p] for p in order]
        assert values == sorted(values), f"seat counts are not monotonic: {dict(zip(order, values))}"

    def test_a_plan_and_its_entitlement_agree(self):
        """`max_seats` is what the console shows; the entitlement is what actually stops someone.
        Two numbers for one fact is how a customer is told they have seats they cannot use."""
        from nexus.billing.plans import PLAN_SEED, seat_quota_for

        mismatched = [
            (p["id"], p.get("max_seats"), seat_quota_for(p["id"]))
            for p in PLAN_SEED
            if p["id"] in EXPECTED_SEATS and p.get("max_seats") != seat_quota_for(p["id"])
        ]
        assert not mismatched, f"max_seats disagrees with the seat.member quota: {mismatched}"

    def test_custom_and_enterprise_plans_are_not_capped_here(self):
        """A negotiated deal sets its own seats; a number in the seed would silently override it."""
        seats = _seed_seats()
        assert seats.get("enterprise") is None
        assert seats.get("legacy-unlimited") is None


class TestEnforcement:
    @pytest.fixture
    def enforcing(self, monkeypatch):
        from nexus.core.config import get_settings

        monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
        return get_settings()

    async def _workspace_on(self, plan_id: str):
        from nexus.billing.catalog import sync_catalog
        from nexus.billing.plans import sync_plans
        from nexus.billing.rates import sync_rates
        from nexus.models.billing import BillingSubscription
        from nexus.models.identity import Workspace

        await sync_catalog()
        await sync_plans()
        await sync_rates()
        tid = await make_tenant()
        async with tenant_session(tid) as ts:
            ts.add(BillingSubscription(plan_id=plan_id, status="active"))
            ws = Workspace(tenant_id=tid, name="W")
            ts.add(ws)
            await ts.flush()
            return tid, ws.id

    async def _add_member(self, ts, workspace_id: str, n: int):
        """One membership, exactly as the invite endpoint creates it."""
        from nexus.core.security import hash_password
        from nexus.models.identity import Membership, User

        user = User(email=f"m{n}@example.com", full_name=f"M{n}",
                    password_hash=hash_password("x"))
        ts.session.add(user)
        await ts.flush()
        ts.add(Membership(tenant_id=ts.tenant_id, user_id=user.id,
                          workspace_id=workspace_id, role="rep"))
        await ts.flush()

    async def test_a_free_workspace_is_stopped_at_its_single_seat(self, enforcing):
        """The reported shape of the hole: one seat sold, unlimited members added."""
        from nexus.billing.entitlements import check_and_meter

        tid, ws = await self._workspace_on("free")
        async with tenant_session(tid) as ts:
            await self._add_member(ts, ws, 1)          # the owner's seat
            blocked = await check_and_meter(
                ts, capability_id="seat.member", idempotency_key="seat-2"
            )
        assert blocked.allowed is False, "a 1-seat plan allowed a second member"
        assert blocked.reason == "quota_exhausted"

    async def test_a_launch_workspace_gets_its_five(self, enforcing):
        from nexus.billing.entitlements import check_and_meter

        tid, ws = await self._workspace_on("launch")
        async with tenant_session(tid) as ts:
            for n in range(4):
                await self._add_member(ts, ws, n)
                res = await check_and_meter(
                    ts, capability_id="seat.member", idempotency_key=f"seat-{n}"
                )
                assert res.allowed is True, f"blocked at member {n + 1} of 5"

    async def test_removing_a_member_frees_the_seat(self, enforcing):
        """The reason `seat.member` is a gauge. A counter would only ever climb, so a customer
        could never get back under a limit they had crossed."""
        from sqlalchemy import select

        from nexus.billing.entitlements import check_and_meter
        from nexus.models.identity import Membership

        tid, ws = await self._workspace_on("free")
        async with tenant_session(tid) as ts:
            await self._add_member(ts, ws, 1)
            assert (await check_and_meter(ts, capability_id="seat.member",
                                          idempotency_key="a")).allowed is False

            member = (await ts.session.scalars(select(Membership))).first()
            await ts.delete(member)
            await ts.flush()

            assert (await check_and_meter(ts, capability_id="seat.member",
                                          idempotency_key="b")).allowed is True

    def test_the_invite_endpoint_consults_the_seat_limit(self):
        """Structural: the gauge is only a gate if the add path asks it. It did not."""
        import inspect

        from nexus.api.routers import workspace

        src = inspect.getsource(workspace.invite_member)
        assert "seat.member" in src, (
            "POST /workspace/members creates a Membership without checking the plan's seats — "
            "so both max_seats and the seat.member quota are decorative"
        )
