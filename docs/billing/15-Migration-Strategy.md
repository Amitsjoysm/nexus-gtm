# 15 — Migration Strategy

> Zero downtime, zero regression, zero behavior change for existing tenants — enforced by
> construction, not by care.
>
> Related: [02-Entitlement-Engine](02-Entitlement-Engine.md) §6 ·
> [14-Implementation-Roadmap](14-Implementation-Roadmap.md)

## 1. Invariants (the contract with existing users)

1. Every existing tenant keeps **exactly** its current capabilities: at migration, all tenants
   are auto-subscribed to `legacy-unlimited` (plan class `unlimited`, $0, never billed, never
   blocked). No existing tenant sees a limit, a banner, or an invoice until a human moves them.
2. **Default-allow forever:** an unregistered or unresolved capability ⇒ allow + shadow log.
   Forgetting to catalog something can only under-meter, never break a feature.
3. **Kill switch:** `NEXUS_BILLING_ENFORCEMENT=off|shadow|on` (env + admin flag). `off` turns the
   seam into a no-op passthrough; incident response is one toggle, no deploy.
4. Additive-only schema (new tables; zero changes to existing ones), applied by the existing
   entrypoint `bootstrap_db.py → alembic upgrade head` + automatic RLS — the same proven path
   migrations 0018–0020 took.

## 2. Rollout sequence

```
Deploy Phase 0/1  → enforcement=off→shadow. Product identical; usage accumulates.
Weeks 1–2         → validate shadow data (reconciliation, COGS sanity vs provider bills).
Enable per-capability enforcement for INTERNAL tenants only (plan class internal).
Launch plans      → new signups land on Free/Trial (priced world starts at signup).
Existing tenants  → stay legacy-unlimited indefinitely; migration to paid plans is a
                    commercial conversation + one Admin action per tenant (audited),
                    with a 30-day usage report attached so the offer fits reality.
Grandfathering    → any tenant that must keep old terms gets a `grandfathered` plan clone.
```

## 3. No-downtime mechanics

- All writes are new tables; the seam is added as dependencies/decorators — no existing handler
  logic edited beyond one added line per endpoint/job ([02](02-Entitlement-Engine.md) §5).
- Metering is async (queue) with sync fallback; worst-case failure mode is *lost telemetry in
  shadow mode*, never a blocked request.
- Rolling restart via the existing compose/health-gate deploy; entrypoint migration is
  idempotent create-or-upgrade, same as every prior release.
- Rollback = flip enforcement off (data keeps accruing harmlessly) or, worst case, revert the
  image — billing tables are ignored by old code by construction.

## 4. Data migration specifics

| Concern | Handling |
|---|---|
| Existing `email_reverify_cooldown_days`, `icp_daily_count`, automation flags | keep working untouched; superseded later by entitlement fields via a mapping seed (tenant settings → contract overrides) with the settings as fallback |
| Existing tenants' historical usage | not back-billed, ever; shadow data starts at deploy |
| Currency/plan pinning | all migrated tenants pinned to USD `legacy-unlimited`; price-book choice happens only at first real subscribe |
| Tests | full suite must stay green at every phase (the Phase-0 exit criterion); billing suites add on top ([16](16-Testing-Strategy.md)) |
