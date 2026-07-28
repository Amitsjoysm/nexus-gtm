# 18 — Future Expansion

> What the architecture already anticipates, so none of these require redesign — only new
> catalog rows, adapters, or Admin surfaces.
>
> Related: [01-Billing-Architecture](01-Billing-Architecture.md) · [08-Feature-Catalog](08-Feature-Catalog.md)

| Expansion | How it lands on this architecture |
|---|---|
| **Public API monetization** | API keys (Admin §API Keys) + the already-shipping `api.request` meter gain per-key attribution; rate cards add `api.request` pricing tiers. Zero new engines. |
| **New AI surfaces** (voice SDR, image gen, agents-as-a-service) | register capability (`ai.voice_minute`, `ai.image`), bind cost rate, price on card. The LLM chokepoint + provider seams already attribute tokens/minutes. |
| **Premium model routing** | `ai.premium_model` entitlement + per-model cost rates ([11](11-Profitability-Analysis.md) §4) — Anthropic/frontier models as a priced tier. |
| **Marketplace / add-on modules** | modules are already entitlement gates (`module.*`); a marketplace is an Admin-managed catalog category with third-party revenue-share metadata on the rate card. |
| **Partner/reseller program** | plan class `partner` exists; add revenue-share reporting over the same rollups. |
| **Usage-based-only PLG motion** | plan class `usage` exists day one; growth experiment = new plan row. |
| **Multi-currency settlement & tax** | price books already per-currency; enable Stripe Tax / add an Avalara adapter behind the tax hook in invoicing. |
| **Data warehouse / BI export** | rollups + invoices are clean fact tables; add a nightly export job (S3/BigQuery) — read-only, no schema impact. |
| **SSO/SCIM as sellable features** | ship as capabilities (`platform.sso`, `platform.scim`) — Enterprise entitlements from the catalog like everything else. |
| **Dedicated infrastructure tier** | contract `dedicated_infra` flag already modeled; fulfillment automates later (per-tenant compose stack), billing unchanged. |
| **Real-time spend controls for customers** | tenant-set budget caps = self-service entitlement overrides (bounded by plan) — the resolution chain gains one more layer (tenant self-cap > contract > plan). |
| **Churn/expansion ML** | denial + soft-limit + adoption event streams are already the feature set; models consume the rollups. |
| **Scale-out metering (10⁶ customers)** | swap EventBus hop for Valkey streams / Kafka behind the same emit API; partitioned events + watermark rollups are already horizontally shardable by tenant. |

**Design debt intentionally accepted (revisit triggers):** invoice PDFs (HTML now → PDF service
when Enterprise demands), bandwidth metering (when egress becomes material), per-query DB
metering (never customer-facing; ops-only if noisy-neighbor isolation is needed), frontend
click billing (kept analytics-only on principle).
