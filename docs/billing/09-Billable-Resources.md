# 09 — Billable Resources (Discovery Inventory)

> Step-1 discovery output: every billable resource in the actual codebase, found via the
> code-review-graph community map (26 subsystems) plus module-level analysis, mapped to the
> code that produces it and the catalog ID that meters it ([08-Feature-Catalog](08-Feature-Catalog.md)).
> Per-unit economics for each row live in [12-Cost-Analysis](12-Cost-Analysis.md).

## A. AI / LLM (cost driver #1 — Groq primary, Anthropic optional, stub offline)

| Resource | Code location | Trigger | Catalog ID |
|---|---|---|---|
| Research brief | `nexus/agents/research.py` | Account 360 → AI actions | `ai.research_brief` |
| Outreach draft | `nexus/agents/messaging.py` | AI actions, campaigns draft phase, cadence touches, approval redrafts, per-contact composer | `ai.email_draft` |
| Account Q&A (+ live web layer) | `nexus/agents/qa.py` | Ask-about-account | `ai.account_qa` |
| ICP scoring | `nexus/agents/scoring.py` | pipeline / refresh | `ai.scoring` |
| Contact ranking | `nexus/agents/contact_rec.py` | AI actions | `ai.contact_rank` |
| Call scripts | `nexus/agents/call_script.py` | Calls console | `ai.call_script` |
| AI ICP from website + buyer titles | relevance module | Relevance page | `ai.icp_from_website` |
| Orchestrator runs (multi-step tool plans) | `nexus/orchestration/engine.py`, `tools.py` | Orchestrator, campaigns | `workflow.orchestration_run` / `_step` |
| Orchestrator chat (SSE) | orchestration chat | Chat page | `ai.chat_turn` |
| Every token of all of the above | `nexus/agents/llm.py` (single chokepoint, `purpose=` attributed) | — | `ai.tokens` |
| Person social insights | `nexus/personalization/` (Apify provider) | enrich/personalize | `ai.personalization_fetch` |

## B. Search / crawl / third-party data (cost driver #2)

| Resource | Code location | Third party | Catalog ID |
|---|---|---|---|
| Web search | `nexus/integrations/search/` registry | Exa (keyed, rotation pool), Brave, Serper, DDG(free) | `search.web` |
| ICP auto-discovery (daily net-new accounts) | `nexus/discovery/auto.py`, worker `discover_icp_accounts` | Exa + crawler + LLM | `discovery.icp_daily`, `discovery.account_added` |
| Company/contact lookalikes | `nexus/lookalike/` | search + enrich + LLM | `discovery.lookalike_*` |
| Account firmographic enrichment | `nexus/enrichment/account.py` (browser provider: scrapling/DDG/cloak) | crawl + LLM | `enrich.account` |
| Contact waterfall enrichment | `nexus/enrichment/waterfall.py`, `providers.py` | search + finder + verifier | `enrich.contact` |
| Buying-committee sourcing | contact sourcing | search + LLM | `enrich.source_committee` |
| LinkedIn URL finder | `nexus/enrichment/linkedin.py` | search | `enrich.linkedin_finder` |
| Email verification (+ DNS chain, reverify sweeps) | `nexus/verification/` (Reacher self-hosted, `dns.py`) | Reacher infra | `verify.email` |
| News signal scans | `nexus/ingestion/sources.py` WebNewsSource | search | `signal.news_scan` |
| RSS signal scans | RssSignalSource | free HTTP | `signal.rss_scan` |

## C. Outreach & communications

| Resource | Code location | Catalog ID |
|---|---|---|
| SMTP email sends (customer's own mailbox) | `nexus/integrations/email_sender.py` | `outreach.email_send` |
| IMAP save-to-drafts | same | `outreach.email_draft_save` |
| Campaign launches / draft+send phases | `nexus/campaigns/service.py`, worker `run_campaign` | `outreach.campaign` |
| Cadence touches (multi-touch engine, per-touch approval) | `nexus/cadences/`, worker `advance_cadences` | `outreach.cadence_touch` |
| SEP pushes | `nexus/integrations/sep.py` (Outreach/Salesloft) | `outreach.sep_push` |
| Call tasks / dispositions / briefs | `nexus/calling/`, `nexus/agents/call_script.py` | `calling.task`, `calling.brief` |
| Telephony minutes (provider seam, Twilio-ready) | `nexus/calling/provider.py` | `calling.minutes` |

## D. Automations, jobs, workflows

| Resource | Code location | Catalog ID |
|---|---|---|
| Plays (signal→action automations) | `nexus/plays/engine.py` | `automation.play_run` |
| Continuous account refresh (sense→act loop) | worker `refresh_due_accounts` → `process_account` | `automation.account_refresh` |
| CRM auto-sync (heartbeat + event fast path) | worker `sync_crm_*`, `nexus/ingestion/crm_sync.py` | `integration.crm_sync` |
| Network source syncs | worker `sync_network_account` | `network.source_sync` |
| Daily digests | worker `send_daily_digests` | `notify.email_digest` |
| Every queue execution (all handlers) | `nexus/workers/tasks.py` dispatch | `job.queue_execution` |

## E. Network (Relationship Graph)

| Resource | Code location | Catalog ID |
|---|---|---|
| Google/Microsoft OAuth contact+calendar sync | `nexus/network/connectors/{google,microsoft}.py` | `network.source_sync` |
| LinkedIn Connections.csv import | `nexus/network/linkedin_csv.py` | `network.linkedin_import` |
| NL "who do we know" search | `nexus/network/search.py` | `network.search` |
| Warm intro paths | `nexus/network/intro.py` | `network.intro_paths` |
| Graph storage (persons/edges) | `nexus/models/network.py` | `network.persons` |

## F. Integrations, notifications, data movement

| Resource | Code location | Catalog ID |
|---|---|---|
| HubSpot / Salesforce connections + pushes | `nexus/ingestion/crm.py` | `integration.crm_connection`, `integration.crm_sync` |
| Alerts: in-app / webhook / Slack / email | `nexus/alerts/` + config channels | `notify.*` |
| CSV imports (custom fields, LinkedIn) | `custom_fields` router, network router | `data.import_csv`, `network.linkedin_import` |
| Exports (CSV lib present in frontend; server export endpoints as added) | `frontend/src/lib/csv.ts` + future endpoints | `data.export` |
| Reports & dashboards (cadence report, analytics overview, run detail) | analytics/cadence routers | `report.*` |

## G. Platform resources

| Resource | Source | Catalog ID |
|---|---|---|
| Seats | memberships | `seat.member` |
| Workspaces / organizations | tenants, workspaces | `platform.workspace` |
| Storage (accounts, contacts, signals, network graph, custom fields) | nightly measure job | `platform.storage` |
| Every API request (route, status, latency) | `RequestContextMiddleware` extension | `api.request` |
| Custom field definitions | `custom_fields` | `platform.custom_fields` |
| Inbox tasks / signals stored | pipeline | `inbox.task`, `signal.stored` |

## H. Deliberately not billed (and why)

| Item | Reason |
|---|---|
| Auth flows (login/OTP/reset) | table stakes; metering yes (abuse), billing never |
| Health/ready/metrics endpoints | ops |
| Button clicks as such | frontend telemetry ≠ invoice-defensible; the triggered server action is billed instead ([10](10-Usage-Tracking.md)) |
| Database queries individually | internal implementation detail; surfaced as storage + api.request + job meters |
| Bandwidth v1 | negligible at current scale; catalog-ready (`platform.bandwidth`) when egress matters |

**Coverage claim:** every community in the code graph maps to at least one catalog row above;
the blanket `api.request` + `job.queue_execution` + `ai.tokens` meters guarantee nothing —
present or future — escapes measurement even before it gets a first-class capability ID.
