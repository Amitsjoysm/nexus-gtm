# 17 — Production Checklist (billing go-live)

> Complements the platform's existing [GO-LIVE-CHECKLIST](../GO-LIVE-CHECKLIST.md); everything
> here must be green before the first tenant is moved to a priced plan.

## Configuration
- [ ] `NEXUS_BILLING_ENFORCEMENT=shadow` verified in prod for ≥14 days; shadow COGS within ±10%
      of provider bills ([16](16-Testing-Strategy.md) §3)
- [ ] Catalog seeded and reviewed; every COGS-bearing capability has a `billing_cost_rates` row
- [ ] Rate card + plans + price books activated through Admin (not seed), margin guardrail green
- [ ] `legacy-unlimited` auto-assignment confirmed for 100% of existing tenants
- [ ] Kill switch tested in prod (off → shadow → off) with zero user impact

## Money rails
- [ ] Stripe live keys set (`NEXUS_STRIPE_*`), webhook endpoint verified + signature-checked,
      webhook replay idempotency proven with Stripe CLI
- [ ] Test purchase, upgrade, downgrade, failed-payment dunning, refund — each end-to-end in live
      mode with an internal tenant
- [ ] Invoice numbering sequence + immutability verified; credit-note flow tested
- [ ] Payment failure alerting → CS channel wired

## Data & scale
- [ ] `billing_usage_events` monthly partitions + retention/archive job scheduled and tested
- [ ] Rollup job p95 < 60s at current volume ×10; reconciliation job clean 7 days running
- [ ] Backup/restore rehearsal including billing tables (extends existing DR runbook)
- [ ] RLS verified on every billing tenant table (tenancy test suite extended)

## Admin & audit
- [ ] Platform-admin accounts MFA-enforced; roles assigned per person, no shared logins
- [ ] Audit log capturing 100% of admin mutations (spot-audit 20 actions)
- [ ] Refund/credit caps per role enforced; margin-exception flow requires reason
- [ ] Support runbook: "customer disputes a charge" and "customer hit a limit wrongly"
      written and dry-run

## Observability
- [ ] Metering emit failures, queue lag, entitlement-cache miss rate, 402/429 rates, dunning
      queue depth — all on the Grafana stack with alert rules (extends `deploy/monitoring/`)
- [ ] Revenue/margin dashboards reconciled against Stripe payout report for one full cycle
- [ ] Anomaly watch live (usage spike alerts)

## Product surface
- [ ] Settings→Billing page complete (plan, meters, packs, invoices, cancel) and mobile-clean
- [ ] Every 402 in the SPA renders the upgrade prompt (not a raw error) — walked all gated flows
- [ ] Trial expiry + soft-limit emails/banners copy-reviewed
- [ ] Pricing page ↔ plan config parity check (the page is generated from, or checked against,
      the price book)

## Legal/finance
- [ ] ToS/billing terms updated (credits, overage, refunds, suspension policy)
- [ ] Tax posture documented (v1 pass-through; Stripe Tax enablement date decided)
- [ ] Finance sign-off on rate card margins and revenue-recognition treatment of credits
