# 12 — Cost Analysis (COGS model → billing_cost_rates)

> Unit cost of every billable capability, from the providers actually wired into this codebase.
> These rates seed `billing_cost_rates` (versioned; events stamp cost-at-time-of-use,
> [03](03-Metering-Architecture.md) §1). All third-party figures are **estimates at current
> public list pricing (July 2026)** — the table is data precisely so Finance can re-rate without
> a deploy.
>
> Related: [11-Profitability-Analysis](11-Profitability-Analysis.md) ·
> [13-Pricing-Recommendations](13-Pricing-Recommendations.md)

## 1. Provider unit costs (inputs)

| Provider (as wired) | Unit | Est. cost | Notes |
|---|---|---|---|
| Groq llama-3.3-70b (primary LLM) | 1M tokens in / out | $0.59 / $0.79 | key rotation pool already supported |
| Anthropic Claude Sonnet (optional primary) | 1M in / out | $3.00 / $15.00 | 10–20× Groq; see routing policy in [11](11-Profitability-Analysis.md) §4 |
| Exa search | search | $0.005 | rotation pool supported |
| Brave / Serper | search | $0.003–0.005 | fallback engines |
| DuckDuckGo | search | $0 | keyless default |
| Reacher (self-hosted) | check | ~$0.0002 | VPS amortized (~$20/mo ÷ ~100k checks) |
| Apify (person insights, when enabled) | profile fetch | ~$0.03 | actor pricing |
| Twilio voice (when enabled) | minute | ~$0.014 | US outbound |
| Customer SMTP (Gmail/Outlook) | email | $0 | customer's own mailbox — zero COGS to us |
| HubSpot/Salesforce/Outreach/Salesloft APIs | call | $0 | customer's own accounts |
| Google/Microsoft Graph (network sync) | call | $0 | customer OAuth |
| Postgres storage (managed-equiv + backup) | GB-month | ~$0.10 | |
| Infra baseline (VM+Valkey+Caddy+monitoring) | month | ~$60–150 | single-VM today; allocation model §3 |

## 2. Per-capability unit COGS (seed for `billing_cost_rates`)

Token profiles measured from the actual prompts (typical in/out):

| Capability | Composition | Unit COGS (Groq) |
|---|---|---|
| `ai.email_draft` | ~1.5k in + 450 out | $0.0012 |
| `ai.account_qa` | ~2.5k in + 400 out **+ up to 2 web searches** | $0.012 |
| `ai.research_brief` | Exa research (~2 searches) + summarize | $0.012 |
| `ai.call_script` | ~1.8k in + 700 out | $0.0016 |
| `ai.contact_rank` | ~1.2k in + 300 out | $0.0009 |
| `ai.scoring` | ~0.8k in + 150 out | $0.0006 |
| `ai.icp_from_website` | crawl + ~3k in + 800 out | $0.010 |
| `ai.chat_turn` | budgeted envelope (1.2k cap) + 300 out | $0.0010 |
| `ai.personalization_fetch` | Apify actor | $0.030 |
| `search.web` | engine call | $0–0.005 (blended $0.004) |
| `discovery.icp_daily` (run) | pool search + ≤40 enriches + scoring | ~$0.10 |
| `discovery.account_added` | run cost ÷ kept accounts (target 20) | ~$0.015 |
| `discovery.lookalike_company` (run) | 3 searches + ≤8 enriches + LLM | ~$0.10 |
| `discovery.lookalike_contact` (run) | in-workspace scoring (no network) | ~$0.0005 |
| `enrich.account` | 2–4 searches (DDG-first) + LLM extract | ~$0.010 |
| `enrich.contact` | search + finder + ≤12 verifies + LLM | ~$0.012 |
| `enrich.source_committee` | search + LLM + verifies (≤5 contacts) | ~$0.05 |
| `verify.email` | Reacher (+DNS, free) | $0.0002 |
| `outreach.email_send` / `_draft_save` | customer SMTP/IMAP | ~$0.0001 (compute) |
| `outreach.cadence_touch` | includes one `ai.email_draft` | $0.0013 |
| `outreach.sep_push` / `integration.crm_sync` | API call, our compute | $0.0001 |
| `network.source_sync` | Google/MS APIs free; compute + ingest | $0.002 |
| `network.linkedin_import` | parse + ingest | $0.0005 |
| `network.search` | indexed SQL + rank | $0.0002 |
| `calling.brief` | assembled dossier (+ optional insights) | $0.001 (+$0.03 w/ Apify) |
| `calling.minutes` | Twilio | $0.014/min |
| `signal.news_scan` | 1 search per account refresh | $0.004 |
| `notify.webhook` / `notify.slack` | HTTP post | $0.0001 |
| `platform.storage` | PG | $0.10/GB-mo |
| `automation.account_refresh` | news scan + scoring | ~$0.005 |
| `api.request` | compute amortized | ~$0.00001 |

## 3. Fixed-cost allocation (for net margin, [11](11-Profitability-Analysis.md))

- **Infra baseline** allocated per active tenant-seat-month: at 100 paying seats on the current
  single-VM stack (~$100/mo incl. monitoring) → ~$1/seat-month; scales sub-linearly (the stack
  is one image; horizontal worker scale-out is the roadmap's Phase 8 concern).
- **Support**: modeled at $6/seat-month on Starter/Growth (email), $15 Professional (priority),
  Enterprise per-contract (dedicated CSM as a contract line item).
- **Maintenance/ops (eng time)**: 15% of revenue planning assumption — appears in net margin,
  never in the 50% *gross* floor which is COGS-only.

## 4. Cost-control levers already in the platform

Burst limits + cooldowns on every entitlement (COGS spike protection), provider fallback chains
(Groq→stub; Exa→DDG), key rotation pools on 429s, verification cool-downs (30-day valid-email
rule), discovery enrich caps (`icp_discovery_enrich_max`), and the anomaly watch
([10](10-Usage-Tracking.md) §3). The margin guardrail ([04](04-Pricing-Engine.md) §5) makes the
50% floor a *validation rule*, not a hope.
