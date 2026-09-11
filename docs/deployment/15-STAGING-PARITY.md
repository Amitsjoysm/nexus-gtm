# 15 — Making staging (or prod) behave exactly like local

Captured from the reference local deploy on 2026-09-10. Staging produced lookalikes named
"Marketjoy Competitor", contacts from another company's page, and orchestrator discovery that
returned nothing. Every one of those traced to configuration, not code: **nearly every
behaviour-defining setting ships with an offline default**, and a deployment that misses one runs a
degraded product with no error anywhere.

Configuration lives in **four** places. Matching `deploy/.env` alone is not enough.

1. `deploy/.env` — environment variables (below).
2. Control plane → **Runtime settings** — overrides stored in the database.
3. Control plane → **Provider keys** — API keys stored in the database.
4. Control plane → **Feature switches**.

Secrets are never written here. `<secret>` means "set it; the value is yours".

---

## 1. `deploy/.env`

### The ones that silently degrade the product if missed

| Variable | Shipped default | Local value | What you get if it is missing |
|---|---|---|---|
| `NEXUS_SEARCH_PROVIDER` | `duckduckgo` | `exa` | Discovery on DuckDuckGo. (Staging/prod now force Exa for discovery regardless, and answer 503 without a key.) |
| `NEXUS_CONTACT_SEARCH_SOURCES` | `stub` | `search` | "Find contacts" finds nobody — the stub returns role placeholders that sourcing discards |
| `NEXUS_RESEARCH_PROVIDER` | `stub` | `search` | Research brief and "Ask about this account" have no web research |
| `NEXUS_LLM_PROVIDER` | `stub` | `auto` | The stub writes emails and call scripts — sent to real prospects |
| `NEXUS_EMAIL_VERIFY_PROVIDER` | `stub` | `reacher,dns` | Every address reads "unverified" |
| `NEXUS_PERSONALIZATION_PROVIDER` | `stub` | `apify` | No LinkedIn personalisation in drafts |
| `NEXUS_PERSONALIZATION_POSTS_ENABLED` | `false` | `true` | Drafts ignore the prospect's recent posts |
| `NEXUS_ACCOUNT_ENRICH_ENABLED` | `false` | `true` | No firmographic enrichment |
| `NEXUS_ICP_DISCOVERY_ENABLED` | `false` | `true` | No daily net-new account discovery |
| `NEXUS_AUTOMATION_ENABLED` | `false` | `true` | No daily scan (account refresh, signals) |
| `NEXUS_BILLING_ENFORCEMENT` | `shadow` | `on` | Plans evaluated but never enforced |
| `NEXUS_CADENCE_ENABLED` | `false` | `true` | Cadence engine off |
| `NEXUS_CRM_SYNC_ENABLED` | `false` | `true` | CRM auto-sync off |
| `NEXUS_OTP_REGISTRATION_ENABLED` | `false` | `true` | Sign-up without email verification |
| `NEXUS_PAYMENT_PROVIDER` | `noop` | `stripe` | Checkout and invoices do nothing |

### Everything else local sets

| Group | Variables (local value where it is not a secret) |
|---|---|
| Environment | `NEXUS_ENV=prod` (use `staging` for staging — both enforce strict Exa discovery), `DOMAIN`, `ACME_EMAIL`, `NEXUS_APP_BASE_URL` (your staging URL) |
| Cryptographic roots | `NEXUS_SECRET_KEY` `<secret>`, `NEXUS_OTP_SECRET` `<secret>` — never editable from the UI |
| Database | `POSTGRES_PASSWORD` `<secret>`, `NEXUS_APP_DB_PASSWORD` `<secret>`, `NEXUS_DB_POOL_SIZE=16`, `NEXUS_DB_MAX_OVERFLOW=5`, `NEXUS_DB_MAX_CONNECTIONS=300` |
| Auth rate limit | `NEXUS_AUTH_RATE_LIMIT_ENABLED=true`, `NEXUS_AUTH_RATE_LIMIT_MAX=10`, `NEXUS_AUTH_RATE_LIMIT_WINDOW_S=60` |
| Automation | `NEXUS_AUTOMATION_TICK_INTERVAL_S=60`, `NEXUS_DIGEST_INTERVAL_HOURS=24`, `NEXUS_DEMO_SIGNALS_ENABLED=false` |
| Search | `NEXUS_EXA_API_KEY` / `NEXUS_EXA_API_KEYS` `<secret>`; `NEXUS_SIGNAL_SEARCH_PROVIDER=firecrawl` + `NEXUS_FIRECRAWL_API_KEY` / `_KEYS` `<secret>` |
| LLM | `NEXUS_LLM_MODEL=openai/gpt-oss-120b`, `NEXUS_GROQ_MODEL=openai/gpt-oss-120b`, `NEXUS_GROQ_API_KEY` / `_KEYS` `<secret>` — use keys from **different Groq organisations**; keys in one org share one request budget |
| Apify | `NEXUS_APIFY_API_KEY` / `_KEYS` `<secret>` |
| Email verification | `NEXUS_EMAIL_VERIFY_URL=<your Reacher /v0/check_email endpoint>` |
| CRM | `NEXUS_CRM_PROVIDER=hubspot`, `NEXUS_HUBSPOT_ACCESS_TOKEN` `<secret>` |
| Payments | `NEXUS_STRIPE_PUBLISHABLE_KEY`, `NEXUS_STRIPE_SECRET_KEY`, `NEXUS_STRIPE_WEBHOOK_SECRET` — all `<secret>` |
| System email | `NEXUS_SYSTEM_SMTP_PROVIDER=gmail`, `NEXUS_SYSTEM_SMTP_USERNAME`, `NEXUS_SYSTEM_SMTP_PASSWORD` `<secret>`, `NEXUS_SYSTEM_SMTP_FROM`, `NEXUS_SYSTEM_SMTP_FROM_NAME` |
| Relationship graph | `NEXUS_NETWORK_GOOGLE_CLIENT_ID`, `NEXUS_NETWORK_GOOGLE_CLIENT_SECRET` `<secret>`, `NEXUS_NETWORK_OAUTH_REDIRECT_BASE=<your staging URL>` |
| Monitoring | `GRAFANA_ADMIN_PASSWORD` `<secret>` |

No longer read, safe to delete: `NEXUS_ENRICHMENT_SEARCH_PROVIDER`, `NEXUS_CONTACT_SEARCH_PROVIDER`,
`NEXUS_WEBSITE_ICP_SEARCH_PROVIDER`, `NEXUS_DISCOVERY_SEARCH_PROVIDER` (see §5).

## 2. Control plane → Runtime settings

| Setting | Local | Staging / prod |
|---|---|---|
| `account_enrich_min_interval_days` | `30` | `30` |
| `admin_ip_allowlist` | `172.18.0.1/32` (the local Docker gateway) | **Your two office IPs.** The local value would lock everyone out of the superadmin panel on any other host, or let the wrong network in. |

## 3. Control plane → Provider keys

| Provider | Local state | Needed for |
|---|---|---|
| Exa | 1 key, verified | **Everything in discovery** — find contacts, lookalikes, find similar, orchestrator, Ask AI, research brief, the enrichment web step. Without it those answer 503, by design. |
| Groq | 5 keys, verified | Every draft, call script, brief |
| Apify | 2 keys, verified | B2B enrichment actor, LinkedIn personalisation, phone lookup |
| Firecrawl | 2 keys, **failed (402 — out of credits)** | Signal dork search. Top up, or signals lose their strongest source. |
| Brave | 2 keys, **failed (placeholder values)** | Nothing routes to Brave. Delete them. |

Apify actors are approved **per Apify account**, in the Apify console — a new key on a new account
403s until someone clicks approve.

## 4. Control plane → Feature switches

`module.network`, `module.campaigns`, `module.cadences` = `coming_soon`. Everything else enabled.

## 5. What routes where (since 2026-09-10)

| Feature | Search backend | If it is unavailable |
|---|---|---|
| Find contacts, Lookalikes, Find similar, Orchestrator discovery, Ask AI / research brief | **Exa only** (in staging and prod, whatever `NEXUS_SEARCH_PROVIDER` says) | 503 naming the fix — never a DuckDuckGo substitute |
| "Enrich from web", adding an account, Run pipeline | Apify B2B actor first, then **Exa only** | Best effort: the actor's fields are kept |
| Signals (dork search) | `NEXUS_SIGNAL_SEARCH_PROVIDER`, **never Exa** (empty or `exa` resolve to Firecrawl) | Firecrawl without a key degrades to keyless DuckDuckGo |
| Signals (news, RSS, job boards, EDGAR/GitHub/HN) | Keyless / DuckDuckGo | Independent of every key above |
| Daily scan (scheduled refresh) enrichment | Source databases + Apify B2B actor, **no web search** | — |
| Daily ICP discovery | Exa only | Skipped for the day with the reason recorded |

Check what a running deployment is actually doing at **Platform health → search**:
`signals=<backend> discovery=exa (strict)`.
