#!/usr/bin/env python
"""Prove a tenant cannot overdraw its credit balance under real concurrency.

    docker exec nexus-gtm-app-1 python /app/scripts/verify_credit_concurrency.py
    docker exec -e NO_LOCK=1 nexus-gtm-app-1 python /app/scripts/verify_credit_concurrency.py

The second form is the CONTROL and is expected to FAIL. Measured 2026-09-04 against the live
Postgres: with the lock, 25 parallel callers against 20 credits spent exactly 20 and stopped at a
balance of 0; with the lock disabled, the same 25 spent 38 and left the balance at -18. Run both,
or the passing run proves nothing.

Genuine concurrent double-spend against real Postgres.

The suite runs on SQLite, which serialises writers, so a concurrency test there proves nothing —
it would pass with the advisory lock removed. This drives N truly parallel connections at one
tenant's balance and asserts the ledger never goes negative.

Each task gets its OWN session. SQLAlchemy's AsyncSession is not safe for concurrent use, and
sharing one here would test the wrong thing (an interleaving error) instead of the lock.
"""
import asyncio
import sys
import uuid


async def _one_burn(tenant_id: str, capability: str, key: str) -> bool:
    """One metered call on its own connection, exactly as a separate request would be."""
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession, apply_rls
    from nexus.billing.entitlements import check_and_meter

    async with get_sessionmaker()() as session:
        await apply_rls(session, tenant_id)
        ts = TenantSession(session, tenant_id)
        res = await check_and_meter(
            ts, capability_id=capability, quantity=1, idempotency_key=key
        )
        await session.commit()
        return bool(res.allowed)


async def main() -> int:
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker, get_sessionmaker
    from nexus.core.tenancy import TenantSession, apply_rls
    from nexus.models.billing import BillingSubscription
    from nexus.models.identity import Tenant
    from sqlalchemy import text as sql

    settings = get_settings()
    settings.billing_enforcement = "on"

    # CONTROL: with NO_LOCK=1 the tenant credit lock is replaced by a no-op. If the run still
    # passes, this probe is not testing what it claims to and the lock could be deleted unnoticed.
    import os
    if os.environ.get("NO_LOCK") == "1":
        import nexus.billing.credits as credits_mod

        async def _noop(ts):
            return None

        credits_mod._lock_tenant_credits = _noop
        print("  [control] tenant credit lock DISABLED")
    await sync_catalog(); await sync_plans(); await sync_rates()

    if "postgresql" not in settings.database_url:
        print(f"REFUSING: not Postgres ({settings.database_url[:30]}) — this test is meaningless "
              "on SQLite, which serialises writers")
        return 2

    slug = f"race-{uuid.uuid4().hex[:8]}"
    async with get_platform_sessionmaker()() as s:
        tenant = Tenant(name=slug, slug=slug)
        s.add(tenant)
        await s.commit()
        tid = tenant.id

    # `ai.email_draft` is 2 credits on the seeded card. Read from the card rather than assumed
    # would be better; hard-coded keeps this script dependency-free and it asserts the total.
    CAPABILITY = "ai.email_draft"
    PRICE = 2
    AFFORDABLE = 10                      # exactly 10 calls are payable
    # Deliberately inside the connection pool (db_pool_size + db_max_overflow, 30 by default).
    # At 60 the probe exhausted the pool and half the callers died on a 30s checkout timeout —
    # which is a real capacity finding, but it masks the property under test. See the audit note.
    PARALLEL = 25

    async with get_sessionmaker()() as session:
        await apply_rls(session, tid)
        ts = TenantSession(session, tid)
        ts.add(BillingSubscription(plan_id="launch", status="active"))
        await ts.flush()
        await grant_credits(ts, AFFORDABLE * PRICE, reason="race",
                            idempotency_key="race-grant")
        await session.commit()

    # Distinct keys: a shared key would be deduplicated and hide the race entirely.
    results = await asyncio.gather(
        *(_one_burn(tid, CAPABILITY, f"race:{i}") for i in range(PARALLEL)),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    allowed = sum(1 for r in results if r is True)

    async with get_sessionmaker()() as session:
        await apply_rls(session, tid)
        final = await balance(TenantSession(session, tid))
        spent = (await session.execute(sql(
            "select coalesce(sum(-delta),0) from billing_credit_ledger "
            "where tenant_id=:t and kind='burn'"), {"t": tid})).scalar()

    print(f"  parallel callers      : {PARALLEL}")
    print(f"  credits granted       : {AFFORDABLE * PRICE}  ({AFFORDABLE} calls at {PRICE})")
    print(f"  calls allowed         : {allowed}")
    print(f"  credits spent         : {float(spent):g}")
    print(f"  final balance         : {final:g}")
    print(f"  exceptions            : {len(errors)}")
    for e in errors[:3]:
        print(f"      {e!r}")

    ok = True
    if final < 0:
        print(f"  ** OVERDRAFT: balance is {final:g} — the lock did not hold **"); ok = False
    if float(spent) > AFFORDABLE * PRICE:
        print(f"  ** OVERSPEND: {float(spent):g} spent against {AFFORDABLE * PRICE} granted **")
        ok = False
    if allowed > AFFORDABLE:
        print(f"  ** {allowed} calls were allowed against {AFFORDABLE} affordable **"); ok = False
    if errors:
        print("  ** concurrent callers raised — a race that fails loudly is still a race **")
        ok = False
    print("\nRESULT:", "PASS — no overdraft under real concurrency" if ok else "FAIL")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
