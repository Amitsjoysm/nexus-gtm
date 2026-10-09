# 16 — Taking gtm.infojoy.com live: start collecting, and run like local

Captured from the reference local deploy on 2026-10-02. It supersedes the tables in
`15-STAGING-PARITY.md` where they disagree (that file was captured on 2026-09-10, before LinkedIn
prospecting, self-hosted fetching, telephony, mailbox apps and the signal window).

Secrets are never written here. `<secret>` means "set it; the value is yours". Nobody pastes a
secret into chat, a ticket or the repo: they go into `deploy/.env` on the server or into
Superadmin → Provider keys.

---

## Part A — Start collecting data

**There is no separate list of URLs to add. A company starts being collected the moment it is an
account with a website domain.** From the domain alone the platform finds the company's news, its RSS
feed, its job board (Greenhouse, Lever or Ashby), SEC filings, GitHub, Hacker News and LinkedIn job
posts, and, if the `website` source is on, watches its pricing, security and careers pages. Adding an
account queues its first crawl immediately; after that it refreshes on its tier (hot accounts every
6 hours, quiet ones less often). **Refresh now** on the account page crawls it on demand.

Ways to add companies, best first:

| Way | Where | Notes |
|---|---|---|
| Fill from your ICP | **Relevance** → save the ICP → "How many companies now?" | Shared store, then LinkedIn, then web. 5 credits per company added; the balance is checked first. |
| Every day, automatically | **Settings → Automation** → daily number | Reaches the number or says why not ("Today: 7 of 10 ..."). Needs a saved ICP. |
| Ask the AI | **Orchestrator**: "find companies matching my ICP" | Adds new companies (fixed 2026-10-01; it used to list 25 you already had). Same price as Populate. |
| One company | **Accounts → Add account** (name + website) | First crawl queued at once. |
| A list | **Accounts → Import** (CSV: company name + website columns) | Semicolon and tab files are read too; bad rows are reported, not fatal. |
| From your CRM | **Integrations** → connect HubSpot or Salesforce → **Pull accounts** | |

People: **Find contacts** on an account page, **Find similar** on a contact, or **Contacts → Import**.

What has to be true for collection to run:

1. `NEXUS_AUTOMATION_ENABLED=true` on the server (the heartbeat that refreshes accounts).
2. The workspace's **Settings → Automation** switch is on (new workspaces start on).
3. Search keys that work (Part C): Exa for discovery, and a signal search provider with credit.

Check it is working at **Superadmin → Platform health**: the `search` row reads
`signals=<backend> discovery=exa (strict)`, and the email verifier, LLM and phone lookup rows are
green. An account's **Signals** tab shows what was found; a source that ran and found nothing is
recorded as `empty`, a broken one as `error`.

---

## Part B — URLs to register outside the app

Every URL below starts with the public base URL, `https://gtm.infojoy.com`, with no trailing slash.

| What | URL | Register it in |
|---|---|---|
| Gmail mailboxes (OAuth) | `https://gtm.infojoy.com/api/engagement/mailboxes/oauth/google/callback` | Google Cloud console → the OAuth client → Authorized redirect URIs |
| Outlook / Microsoft 365 mailboxes (OAuth) | `https://gtm.infojoy.com/api/engagement/mailboxes/oauth/microsoft/callback` | Azure portal → App registration → Authentication → Redirect URIs |
| Gmail reply pickup (push) | `https://gtm.infojoy.com/api/engagement/webhooks/gmail` | Google Pub/Sub push subscription, authentication on, audience = this same URL |
| Outlook reply pickup | `https://gtm.infojoy.com/api/engagement/webhooks/graph` | Nothing to register: the app subscribes itself (needs public HTTPS) |
| Stripe webhook | `https://gtm.infojoy.com/api/billing/webhooks/stripe` | Stripe dashboard → Webhooks. **Superadmin → Configuration → Billing → Webhook** lists the URL and the 13 events to tick, and **Send test** proves the signing secret |
| CRM connect by OAuth (optional) | `https://gtm.infojoy.com/api/integrations/crm/oauth/hubspot/callback` and `.../salesforce/callback` | HubSpot / Salesforce app consoles. Only if you use OAuth; a pasted CRM token needs no URL |
| Relationship graph (optional) | `https://gtm.infojoy.com/api/network/oauth/google/callback` (and `/microsoft/`) | The Network app's OAuth client |

Scopes and the staging/local rows are in `docs/engagement/oauth-environments.md`. DNS: point
`gtm.infojoy.com` at the server; Caddy obtains the certificate from `DOMAIN` and `ACME_EMAIL`.

---

## Part C — Settings that make production run like local

Configuration lives in **six** places, and matching `deploy/.env` alone is not enough:

1. `deploy/.env` on the server
2. Superadmin → **Configuration** (runtime settings, stored in the database)
3. Superadmin → **Provider keys**
4. Superadmin → **Mailbox apps**
5. Superadmin → **Payments**
6. Superadmin → **Feature switches** (and **Pages in the menu**)

### C1. `deploy/.env`

**Must differ from local:**

| Variable | Local | Production |
|---|---|---|
| `DOMAIN` | `localhost` | `gtm.infojoy.com` |
| `ACME_EMAIL` | (set) | an inbox you read: certificate expiry notices go there |
| `NEXUS_APP_BASE_URL` | `https://localhost` | `https://gtm.infojoy.com` |
| `NEXUS_NETWORK_OAUTH_REDIRECT_BASE` | `https://localhost` | `https://gtm.infojoy.com` |
| `NEXUS_OAUTH_REDIRECT_BASE` | not set | `https://gtm.infojoy.com` (only for CRM OAuth) |
| `NEXUS_SECRET_KEY`, `NEXUS_OTP_SECRET`, `POSTGRES_PASSWORD`, `NEXUS_APP_DB_PASSWORD`, `GRAFANA_ADMIN_PASSWORD` | `<secret>` | **new** `<secret>` values for production; never reuse local ones |
| `NEXUS_STRIPE_*` | test keys | live keys, or leave empty and use the Payments tab (C5) |

**Copy as local has them** (these are what make the product behave; every one has an offline default
that silently degrades it if missed):

| Variable | Value |
|---|---|
| `NEXUS_ENV` | `prod` |
| `NEXUS_AUTOMATION_ENABLED` | `true` |
| `NEXUS_AUTOMATION_TICK_INTERVAL_S` | `60` |
| `NEXUS_DIGEST_INTERVAL_HOURS` | `24` |
| `NEXUS_DEMO_SIGNALS_ENABLED` | `false` (production refuses to start otherwise) |
| `NEXUS_ICP_DISCOVERY_ENABLED` | `true` |
| `NEXUS_LLM_PROVIDER` | `auto` |
| `NEXUS_LLM_MODEL`, `NEXUS_GROQ_MODEL` | `openai/gpt-oss-120b` |
| `NEXUS_SEARCH_PROVIDER` | `exa` |
| `NEXUS_SIGNAL_SEARCH_PROVIDER` | `firecrawl` |
| `NEXUS_CONTACT_SEARCH_SOURCES` | `search` |
| `NEXUS_RESEARCH_PROVIDER` | `search` |
| `NEXUS_ACCOUNT_ENRICH_ENABLED` | `true` |
| `NEXUS_EMAIL_VERIFY_PROVIDER` | `reacher,dns` |
| `NEXUS_EMAIL_VERIFY_URL` | your Reacher `/v0/check_email` endpoint (local uses the shared instance) |
| `NEXUS_PERSONALIZATION_PROVIDER` | `apify` |
| `NEXUS_PERSONALIZATION_POSTS_ENABLED` | `true` |
| `NEXUS_CADENCE_ENABLED` | `true` |
| `NEXUS_CRM_SYNC_ENABLED` | `true` |
| `NEXUS_CRM_PROVIDER` | `hubspot` (the fallback; each workspace can connect its own) |
| `NEXUS_OTP_REGISTRATION_ENABLED` | `true` (sign-up verifies the email) |
| `NEXUS_AUTH_RATE_LIMIT_ENABLED` / `_MAX` / `_WINDOW_S` | `true` / `10` / `60` |
| `NEXUS_PAYMENT_PROVIDER` | `stripe` |
| `NEXUS_BILLING_ENFORCEMENT` | `on` locally. **Decide for production:** `on` refuses actions a workspace cannot pay for; `shadow` records and never blocks |
| `NEXUS_DB_POOL_SIZE` / `_MAX_OVERFLOW` / `_MAX_CONNECTIONS` | `16` / `5` / `300` (size to the database tier: B1ms allows 35 user connections, shared with the worker) |
| `NEXUS_SYSTEM_SMTP_PROVIDER`, `_USERNAME`, `_PASSWORD`, `_FROM`, `_FROM_NAME` | the mailbox that sends sign-up codes and digests; use a company address in production |
| Keys: `NEXUS_EXA_API_KEY(S)`, `NEXUS_GROQ_API_KEY(S)`, `NEXUS_APIFY_API_KEY(S)`, `NEXUS_FIRECRAWL_API_KEY(S)`, `NEXUS_HUBSPOT_ACCESS_TOKEN` | `<secret>`. Optional here: keys added on the Provider keys screen are used first and need no restart |

Safe to leave out (no longer read): `NEXUS_ENRICHMENT_SEARCH_PROVIDER`,
`NEXUS_CONTACT_SEARCH_PROVIDER`, `NEXUS_WEBSITE_ICP_SEARCH_PROVIDER`,
`NEXUS_DISCOVERY_SEARCH_PROVIDER`.

### C2. Superadmin → Configuration (runtime settings)

Changes take effect within 30 seconds in every process, no redeploy.

| Setting | Local | Production |
|---|---|---|
| `admin_ip_allowlist` | `172.18.0.1/32` (the local Docker gateway) | **your two office IPs.** Copying the local value locks everyone out of the console |
| `account_enrich_min_interval_days` | `30` | `30` |
| Signal window (`signal_window_user_choice`, `signal_window_default`) | defaults | your choice; the default "All time" changes nothing |

Four overrides stored locally (`contact_search_provider`, `discovery_search_provider`,
`enrichment_search_provider`, `website_icp_search_provider`) are no longer in the catalog and are
ignored; do not recreate them. After setting `email_verify_url`, press **Check connection**.

### C3. Superadmin → Provider keys

| Provider | Local state | Production |
|---|---|---|
| Exa | 1 key, verified | **Required.** Discovery, find contacts, find similar, research, orchestrator all answer 503 without it, by design |
| Groq | 5 keys, verified | Required for every draft, call script and brief. Use keys from **different Groq organisations** (one org shares one budget). Pick the model on the same screen |
| Apify | 2 keys, verified | LinkedIn prospecting, phone lookup, personalisation. Actors must be **approved per Apify account** in the Apify console; replace the key that answers 401 |
| Firecrawl | 2 keys, **failed: out of credit (402)** | Top up, or signal search loses its strongest source |
| Brave | 2 keys, **failed: placeholder values** | Delete them |
| `google_oauth`, `microsoft_oauth` | 1 each, untested | The mailbox app client secrets (C4) |
| Twilio (optional) | none | `ACCOUNT_SID:AUTH_TOKEN`, plus Configuration → Calling (platform account) = Twilio, only if the platform places calls |

Press **Test** on each key; **Verify** makes one real (paid) request and is opt-in.

### C4. Superadmin → Mailbox apps

Public base URL `https://gtm.infojoy.com`, the Google client id, the Microsoft client id and tenant,
and the Gmail push topic and service account. Client secrets go in the two secret fields here (stored
as provider keys, shown only by their last four characters). Local holds client ids but no public base
URL, so mailbox connection is set up but not usable locally.

### C5. Superadmin → Payments

Local has **no** credential on this screen: it runs on the `NEXUS_STRIPE_*` environment keys. For
production, add the live secret key here, **Verify** (it reads the Stripe account name and live/test
mode), then **Activate**: an active credential selects Stripe on its own. Then register the webhook
(Part B) and save its signing secret. Plans and prices are already in the database; a Stripe price is
created on a plan's first purchase.

### C6. Feature switches and pages

Local: every module enabled (`agents`, `cadences`, `calling`, `campaigns`, `network`, `outreach`),
no pages hidden. Decide per module for production; **Pages in the menu** hides a page from every
workspace without switching its feature off.

### C7. Per workspace, after go-live

Each customer workspace sets: **Relevance** (product context, value propositions, ICP), **Settings →
Email style & signature** (example emails, target length, what a first email asks for), **My
mailboxes** (connect Gmail or Outlook, each with its own signature), **Settings → Automation** (daily
number), and **Integrations** (CRM). Platform admins are added on **Superadmin → Access**.

---

## After deploying

1. Platform health all green.
2. Configuration: every changed setting reads **in effect**, not only **saved**.
3. Add one account you know well and open its Signals tab after a few minutes.
4. Sign up a test workspace end to end (OTP email arrives, plan is Free, Relevance populate works).

### Once, after the release with shared reuse and LinkedIn checks (2026-10-02)

Both scripts are dry runs unless given `--apply`, print the database they will touch first, and are
safe to repeat. Run the dry run, read the counts, then apply.

```
docker cp scripts/derive_company_codes.py <app-container>:/tmp/derive.py
docker exec <app-container> python /tmp/derive.py            # then again with --apply
docker cp scripts/clean_linkedin_urls.py <app-container>:/tmp/clean_li.py
docker exec <app-container> python /tmp/clean_li.py          # then again with --apply
```

- `derive_company_codes` lets companies one workspace added be offered to others with the same ICP
  (behind each workspace's fit threshold). Locally: 256 of 258 rows gained an industry code.
- `clean_linkedin_urls` moves any LinkedIn URL that is not what its field claims to
  `rejected_linkedin_url`, and fills account company pages a LinkedIn read proved. Locally: nothing
  bad stored, 121 account pages filled.

**Campaigns:** Platform health now has a **campaigns** row. If campaigns look stuck, it names the
stage: the switch off, emails overdue (the worker is not sending them), mailboxes not read
(replies not picked up), mailboxes needing reconnecting, or a mailbox's own error such as Google
refusing the read because the Gmail API is not enabled in the Google Cloud project behind the
Mailbox app.

**CRM (HubSpot):** the token to paste is a private app's access token, which starts with `pat-`.
HubSpot now lists private apps under **Development → Legacy apps** (older portals: Settings →
Integrations → Private Apps). A **Personal Access Key** is for the HubSpot CLI and is refused by the
CRM API even when brand new. Leave **API base URL** blank.
