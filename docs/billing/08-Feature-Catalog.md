# 08 — Feature Catalog (billing_capabilities)

> The canonical registry. Every row = one billable capability with a stable ID that application
> code references and Admin prices. Seeded from the discovery inventory in
> [09-Billable-Resources](09-Billable-Resources.md); unit costs in [12-Cost-Analysis](12-Cost-Analysis.md);
> credit prices in [13-Pricing-Recommendations](13-Pricing-Recommendations.md).

Catalog row schema: `id · category · sub_category · name · description · unit
(action|token|search|check|message|seat|gb|minute|request|job|run) · meter_kind
(counter|gauge|passthrough) · default_mode (shadow|enabled|metered|enterprise) · cost_rate_id`.

## Catalog (v1 seed — 58 capabilities)

### Platform & seats
| ID | Unit | Default mode | Notes |
|---|---|---|---|
| `seat.member` | seat | metered | seat-days rating |
| `platform.workspace` | action | enabled | workspaces per tenant (quota) |
| `platform.storage` | gb | metered | nightly measured; rows→GB |
| `api.request` | request | shadow | blanket middleware meter; analytics + future public-API billing |
| `platform.custom_fields` | action | enabled | defs per tenant (quota) |

### AI — agents & orchestration (LLM chokepoint attributes: tokens, model, purpose)
| ID | Unit | Default | Maps to |
|---|---|---|---|
| `ai.tokens` | token | shadow | raw token meter (every purpose) — COGS truth |
| `ai.research_brief` | action | metered | agents/research |
| `ai.email_draft` | action | metered | agents/messaging (campaign/cadence/approval redrafts incl.) |
| `ai.account_qa` | action | metered | agents/qa (incl. live web layer) |
| `ai.scoring` | action | shadow | agents/scoring (pipeline; bundled into refresh) |
| `ai.contact_rank` | action | metered | agents/contact_rec |
| `ai.call_script` | action | metered | agents/call_script |
| `ai.icp_from_website` | action | metered | relevance AI-ICP + buyer-title generation |
| `workflow.orchestration_run` | run | metered | orchestration engine run start |
| `workflow.orchestration_step` | job | shadow | per-step execution (COGS attribution) |
| `ai.chat_turn` | action | metered | orchestrator chat message |
| `ai.personalization_fetch` | action | metered | Apify social insights per contact (when enabled) |

### Search, discovery & enrichment
| ID | Unit | Default | Maps to |
|---|---|---|---|
| `search.web` | search | shadow | search provider registry (Exa/Brave/Serper/DDG) — COGS meter |
| `discovery.icp_daily` | job | metered | daily ICP auto-discovery run (per net-new account kept: `discovery.account_added`) |
| `discovery.account_added` | action | metered | strict-ICP account persisted |
| `discovery.lookalike_company` | action | metered | account lookalikes run |
| `discovery.lookalike_contact` | action | metered | contact lookalikes run |
| `enrich.account` | action | metered | web firmographic enrich (crawl+LLM) |
| `enrich.contact` | action | metered | waterfall contact enrich (finder+verify) |
| `enrich.source_committee` | action | metered | source-contacts per account |
| `enrich.linkedin_finder` | action | metered | LinkedIn URL finder |
| `verify.email` | check | metered | Reacher/DNS verification (incl. reverify sweeps) |
| `signal.news_scan` | job | shadow | web-news fetch per account refresh |
| `signal.rss_scan` | job | shadow | RSS source fetch |
| `signal.stored` | action | shadow | signal rows persisted (storage-adjacent) |

### Outreach & workflow
| ID | Unit | Default | Maps to |
|---|---|---|---|
| `outreach.email_send` | message | metered | SMTP send (campaign/cadence/approval) |
| `outreach.email_draft_save` | message | metered | IMAP save-to-drafts |
| `outreach.campaign` | run | metered | campaign launched |
| `outreach.cadence_touch` | action | metered | cadence touch executed |
| `outreach.sep_push` | action | metered | Outreach/Salesloft push |
| `calling.task` | action | enabled | call task creation (quota) |
| `calling.brief` | action | metered | pre-call dossier |
| `calling.minutes` | minute | enterprise | telephony minutes (when provider enabled) |
| `automation.play_run` | job | metered | play execution |
| `automation.account_refresh` | job | shadow | heartbeat sense→act loop per account |
| `inbox.task` | action | shadow | inbox tasks created (analytics) |

### Network (Relationship Graph)
| ID | Unit | Default | Maps to |
|---|---|---|---|
| `network.source_sync` | job | metered | Google/Microsoft OAuth sync run |
| `network.linkedin_import` | job | metered | Connections.csv import |
| `network.search` | search | metered | NL who-do-we-know query |
| `network.intro_paths` | action | enabled | intro-path lookups (quota) |
| `network.persons` | gauge | metered | graph size (storage-adjacent quota) |

### Integrations, notifications, data movement
| ID | Unit | Default | Maps to |
|---|---|---|---|
| `integration.crm_sync` | action | metered | HubSpot/Salesforce account push |
| `integration.crm_connection` | action | enabled | connected CRM count (plan-gated) |
| `notify.in_app` | message | shadow | alerts in-app |
| `notify.webhook` | message | metered | webhook deliveries |
| `notify.slack` | message | metered | Slack channel deliveries |
| `notify.email_digest` | message | metered | daily digest emails |
| `data.import_csv` | job | metered | custom-fields/LinkedIn CSV imports |
| `data.export` | job | metered | exports (as shipped; catalog-ready now) |
| `report.cadence` | action | enabled | cadence report views (quota on Free) |
| `report.analytics` | action | enabled | dashboard/analytics reads (quota on Free) |
| `job.queue_execution` | job | shadow | every queue job (ops + COGS analytics) |

**Module gates** (dependencies for whole areas): `module.outreach`, `module.calling`,
`module.network`, `module.discovery`, `module.integrations`, `module.api` — plain
enabled/disabled entitlements that other capabilities `depends_on`
([02](02-Entitlement-Engine.md) §1), giving one-switch plan differentiation.

## Governance

- IDs are immutable once shipped; renames create a new row + alias.
- New feature = register capability (one Admin form or seed migration) + call the seam. That is
  the entire engineering cost of monetizing anything new.
- `default_mode=shadow` on everything COGS-bearing from day one — measure first, price second.
