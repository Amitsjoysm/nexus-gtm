"""Creating a workspace works on Postgres under RLS, by all three paths (found 2026-11-18).

`/auth/signup`, the OTP path's `/auth/register/verify` (what production uses) and `/auth/workspaces`
create the tenant on a raw session with no tenant bound. Each staged the sign-up's training-consent
row (tenant-scoped, RLS-enrolled) and only then called `start_subscription_for`, which binds the
tenant. Under SQLAlchemy 2.0 (the image pins 2.0.51 in `constraints.txt`) the binding statement
does not flush pending rows, so production signups worked. Under 2.1 (what CI's unconstrained
install gets: 2.1.9 on 2026-11-18) it autoflushes the consent row BEFORE the binding exists,
Postgres refuses it ("new row violates row-level security policy for table training_consents"),
the subscription helper swallows the error, and the commit fails on the aborted transaction: a 500
for every new workspace. Measured both ways on the unfixed code. The tenant is now bound right
after its row, so the order no longer depends on the library. SQLite has no RLS, so the offline
suite never saw it (`tests/test_signup_binds_tenant.py` guards the order there).

Each test also checks what a new workspace must have: its consent row, an active Free
subscription and Free's 200 credits.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from tests_integration.conftest import requires_pg
from tests_integration.rls_harness import app_client, rls_database, seed_billing

pytestmark = [pytest.mark.asyncio, requires_pg]


@pytest_asyncio.fixture
async def signup_db(monkeypatch):
    async with rls_database(monkeypatch, database="nexus_signup_rls_test",
                            role="nexus_app_signup_test",
                            password="signup-rls-test-password") as owner_url:
        await seed_billing()
        yield owner_url


async def _workspace_state(tenant_id: str) -> dict:
    """What the new workspace holds, read as the owner (it sees every tenant)."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingCreditLedger, BillingSubscription
    from nexus.models.ledger import TrainingConsent

    async with get_platform_sessionmaker()() as s:
        sub = (await s.scalars(select(BillingSubscription).where(
            BillingSubscription.tenant_id == tenant_id))).first()
        consents = await s.scalar(select(func.count()).select_from(TrainingConsent).where(
            TrainingConsent.tenant_id == tenant_id))
        credits = await s.scalar(select(func.coalesce(func.sum(BillingCreditLedger.delta), 0))
                                 .where(BillingCreditLedger.tenant_id == tenant_id))
    return {"plan": sub.plan_id if sub else None, "status": sub.status if sub else None,
            "consents": int(consents or 0), "credits": float(credits or 0)}


def _expect_a_whole_workspace(state: dict) -> None:
    assert state == {"plan": "free", "status": "active", "consents": 1, "credits": 200.0}, state


async def test_signup_creates_a_whole_workspace_as_the_app_role(signup_db):
    async with app_client() as c:
        r = await c.post("/api/auth/signup", json={
            "company_name": "Signup Co", "company_slug": "signup-co", "full_name": "Ada",
            "email": "ada@signup-co.example.com", "password": "password123",
            "training_consent": True})
    assert r.status_code == 201, r.text
    _expect_a_whole_workspace(await _workspace_state(r.json()["tenant_id"]))


async def test_the_otp_path_creates_a_whole_workspace_as_the_app_role(signup_db, monkeypatch):
    import nexus.auth.registration as reg
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "otp_registration_enabled", True)
    sent: dict = {}

    async def capture(to: str, code: str) -> bool:
        sent["code"] = code
        return True

    monkeypatch.setattr(reg, "send_otp_email", capture)
    async with app_client() as c:
        start = await c.post("/api/auth/register/start", json={
            "company_name": "Otp Co", "company_slug": "otp-co", "full_name": "Ada",
            "email": "ada@otp-co.example.com", "password": "password123",
            "training_consent": False})
        assert start.status_code == 202, start.text
        r = await c.post("/api/auth/register/verify",
                         json={"email": "ada@otp-co.example.com", "code": sent["code"]})
    assert r.status_code == 201, r.text
    _expect_a_whole_workspace(await _workspace_state(r.json()["tenant_id"]))


async def test_a_second_workspace_is_created_whole_as_the_app_role(signup_db):
    async with app_client() as c:
        first = await c.post("/api/auth/signup", json={
            "company_name": "First Co", "company_slug": "first-co", "full_name": "Ada",
            "email": "ada@first-co.example.com", "password": "password123"})
        assert first.status_code == 201, first.text
        token = first.json()["access_token"]
        r = await c.post("/api/auth/workspaces", headers={"Authorization": f"Bearer {token}"},
                         json={"name": "Second Co", "slug": "second-co"})
    assert r.status_code == 201, r.text
    _expect_a_whole_workspace(await _workspace_state(r.json()["tenant_id"]))
