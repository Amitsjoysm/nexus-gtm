# Prospector — cold prospect search over the InfoJoy Search API

Status: design, decisions recorded with the product owner on 2026-10-16. Branch `feat/prospect-engine`
(cut from master `f6baafc`). Nothing here is implemented yet.

**In one sentence:** a new route, `/prospector`, where a rep searches a third-party B2B database of
contacts and companies, browses partially masked results for free, and pays per field delivered
(saving, listing or exporting the records).

"Prospector" is the working name (decision D1); it lives in the nav label, the page title, the route
and the `module.prospector` gate, so a later rename changes those and nothing structural.

---

## 1. Comparable tools, and what we take from them

| Tool | Browsing | Pay point | Re-access | Bulk | Pattern |
|---|---|---|---|---|---|
| Apollo | Free, filter sidebar | 1 credit to reveal an email | Saved records reused free | Selection cap per plan on Net new/Total; 50k on Saved | **Total / Net new / Saved** tabs |
| ZoomInfo | Free | 1 credit per export | Charged on first enrichment within a records-under-management window | List match upload | Controlled-vocabulary filters |
| Lusha | Free | Reveal: email 1, phone 5 | Re-reveal costs 0 | Bulk "Show details", 25–100 | "Not in my CRM"; contact-details filter (has phone/email); **email and phone priced apart** |
| Cognism | Free, unlimited | On redeeming a contact | Reuse during contract | — | Net new = not redeemed by anyone in the account |
| Seamless.AI | — | 1 credit per researched contact | — | — | — |
| Clay | Free preview with counts | Per result imported | — | ≤ 5k per search | AND across filter types, OR within one |

Adopted: free masked browsing; pay on delivery; **never re-charge a field the workspace already paid
for**; Total / Net new / Saved; a has-direct-phone filter; include and exclude chips; AND across
sections, OR within one; email and phone priced as separate credits; an explicit select-all cap; the
cost shown and confirmed before anything is spent.

Sources: Apollo knowledge base (net new / saved, selection limits), ZoomInfo pricing and API docs,
Lusha docs (prospecting, bulk show, "Not in my CRM"), Cognism help centre (Net New filter, Prospector
search), Clay docs and community (Find people/companies limits, credits).

---

## 2. Decisions (product owner, 2026-10-16)

| # | Decision |
|---|---|
| D1 | Name **Prospector** (for now). Route `/prospector`, package `nexus/prospector/`, gate `module.prospector`, nav label "Prospector". |
| D2 | InfoJoy bills **per record returned**, roughly **$0.001–$0.005** a record. |
| D3 | **Browsing masked results is free.** Credits are charged only when records are saved, added to a list, revealed or exported. Every price and cost is editable in Control plane > Billing; the seed never overwrites a live value. |
| D4 | **Contacts are priced per field.** `prospector.email`: 1 credit per record that has an email. `prospector.phone`: 1 credit per direct phone, **only when "Include direct phones" is ticked**. A record with neither costs 0. |
| D5 | **Companies:** `prospector.company`, 1 credit per new company taken from the **Companies** view. Companies taken through the **Contacts** view (the company a saved contact needs, or "Add their companies to an account list") are **free**. |
| D6 | **Charge once** per workspace per (record, field): a ledger makes re-adding, re-exporting and retries free. |
| D7 | **Partial masks** before payment (shapes in §5). |
| D8 | **Reveal = save**: a row's Reveal saves the record (with the D4/D5 charges) and the row then shows the workspace's own values. |
| D9 | **Every action is rep+** (`Permission.manage_accounts`): search, reveal, save, list, export, verify. |
| D10 | Results live in a **dedicated platform-global, Fernet-sealed cache table**, 24 h TTL, shared by every workspace. |
| D11 | Browse-cost guards: **default page 25** (25/50/100); the shared cache; a **daily row budget per workspace**; a **per-user search rate limit**. |
| D12 | **Net new is a capped scan**: hide held rows to fill the page, next-only cursor, stop after a capped number of scanned rows and say so. |
| D13 | **The Saved tab reads workspace data** (Accounts/Contacts), never the API. |
| D14 | NEXUS-only filters (Email status, Lists, Signals, Funding, Job postings) work **on the Saved tab only**; on Total/Net new they show disabled with the reason. **Email Opened is left out**: NEXUS does not track opens. |
| D15 | **Two location levels**: Contacts view has **Location** (the person: country, state, city, zip) and **Company location** (the company: country, state, city, zip); Companies view has Company location. InfoJoy's DB holds both sets; **the field names for each are established by real calls** (§11), and asked of the owner if the calls cannot prove them. |
| D16 | **One Phone section**: "Has direct phone". |
| D17 | **Select-all cap: 1,000** records per action. |
| D18 | **All plans including Free**: `module.prospector` enabled by default and on no plan (a strict no-op). |
| D19 | **Vendor emails are kept** as delivered, and contacts are saved with `enrichment_source="sourcing:prospector"`. **Campaigns hold them until verified**, through a new enrollment gate (§9.3). A **Verify** button works on one contact or a bulk selection. |
| D20 | **Verify is free for addresses Prospector delivered** (owner correction). Verifying any other contact from the Saved tab charges `verify.email`, as everywhere else. |
| D21 | Limits default to **30 searches per user per minute** (a Runtime setting) and **2,500 rows browsed per workspace per day**. |
| D22 | A superadmin **raises the daily rows per plan or per customer** through a plan entitlement on `prospector.daily_rows`, set in the existing plan editor and Custom plan dialog. No number means the default. It is enforced even while billing runs in `shadow`. |
| D23 | Seed rate cards: **1 credit, $0.003 cost** for each of `prospector.email`, `prospector.phone` and `prospector.company` (70% margin). |
| D24 | **Cross-type action**: the Contacts view also offers "Add their companies to an account list" (free, D5). |
| D25 | The token for the first real calls goes into **Provider keys on the local deploy**, after this branch's catalog entry is deployed there (rollback tag + DB backup first). |

---

## 3. What the codebase forces

1. **`contacts.account_id` is NOT NULL.** Saving a person also finds or creates their company account
   through `accounts.dedupe.find_existing_account` (free, D5).
2. **The API returns no record id.** Identity is derived with the rules `nexus/people` and
   `nexus/companies` use: a person is their normalised email, else first + last name + company domain;
   a company is its normalised domain (`companies.resolution.normalise_domain`). Never a bare name.
3. **Row keys sent to the browser are HMACs**, keyed by a secret derived from `secret_key` plus the
   tenant id. Never `sha256(email)`: the email space is small enough to brute-force
   (`nexus/people` says the same of its own hash column), so a bare hash in the DOM would be the
   unmasked value with one extra step.
4. **The engagement engine has no send gate on `email_status` or `enrichment_source`.** The
   "sourced contacts stay gated" comments in `agents/discovery.py` and `contacts/sourcing.py` describe
   the old campaign engine (spec §13 of the engagement work). D19's hold is therefore **new code**: a
   small enrollment gate (§9.3), not a reuse.
5. **`api/csv_export.py` does not neutralise spreadsheet formulas.** Vendor text in a CSV is
   attacker-influenced, so the Prospector export neutralises leading `= + - @ \t \r` (§9.4). The
   existing exports are left as they are; changing them is a separate fix.
6. **NEXUS has no email-open tracking**, hence D14.

---

## 4. Architecture

### 4.1 Package `nexus/prospector/`: one job per module

| Module | Job | Depends on |
|---|---|---|
| `fields.py` | The filter catalog: each UI section → API field(s) per view; which scopes it works on; enumerated values where observed. Served by `GET /prospector/filters`, so the frontend never hard-codes a vocabulary. | — |
| `filters.py` | Strict Pydantic models `ContactFilters`, `CompanyFilters`, `WorkspaceFilters` (NEXUS-only). `api_payload()` drops empties and never emits a sort. | `fields` |
| `client.py` | `InfoJoyClient.search(kind, payload, page, size, count) -> SearchOutcome`. Key pool, timeouts, rotation; **total, never raises**. | `providers.resolver` |
| `records.py` | Parse raw items into `VendorContact` / `VendorCompany` dataclasses, with the field names and casing **as observed** (§11). | — |
| `identity.py` | `identity(record)`, `fingerprint(identity)` (sha256, server-side only), `row_key(tenant_id, identity)` (HMAC, browser-safe). | `companies.resolution` |
| `masking.py` | `mask_email`, `mask_phone`, `mask_domain`, `mask_linkedin` (§5). | — |
| `cache.py` | The platform-global sealed page store: `get`, `put`, `prune`. Total; a failure is never stored. | `core.crypto` |
| `holdings.py` | Which of these identities the workspace already holds (batched, one query per kind per page) and which fields it has already bought (ledger). | `TenantSession` |
| `limits.py` | Per-user rate limit and daily row budget. | `core.ratelimit`, billing entitlements |
| `search.py` | Total and Net new pages: validate → limits → cache or client → holdings → mask → DTO. | the above |
| `saved.py` | The Saved tab over Accounts/Contacts with mappable and NEXUS-only filters. | models |
| `pricing.py` | A **quote**: for a selection and action, which fields are new to this workspace → quantity per capability. Pure apart from the ledger read. | `holdings` |
| `deliver.py` | Save, list, reveal, company-list: resolve rows → quote → preflight → one SAVEPOINT {records, list items, ledger, charges}. | `accounts.dedupe`, `lists.service`, `billing` |
| `export.py` | CSV rows for delivered records, formula-safe. | `api.csv_export` |
| `verify.py` | Verify selected contacts: free for Prospector-delivered addresses, charged otherwise; inline up to 25, a durable job above that. | `verification`, `enrichment.policy` |

Router: `nexus/api/routers/prospector.py` (thin: parse, `require(manage_accounts)`, call a service,
map outcomes to HTTP). MCP: two tools in `nexus/mcp/tools/catalog.py`. Frontend:
`frontend/src/pages/prospector/`.

### 4.2 Endpoints (all `require(Permission.manage_accounts)`, all tenant-scoped)

| Method, path | Purpose |
|---|---|
| `GET /prospector/filters` | Section catalog per view: API fields, scope availability, enumerated values. |
| `POST /prospector/search` | `{view, scope: total\|net_new, filters, page \| cursor, page_size, include_count}` → a masked page. |
| `POST /prospector/saved` | `{view, filters, workspace_filters, page, page_size}` → workspace rows (the tenant's own data, unmasked). |
| `POST /prospector/quote` | `{selection, action, include_phones}` → credits per capability, already-paid counts, unavailable rows. Spends nothing. |
| `POST /prospector/deliver` | `{selection, action: save\|list\|reveal\|company_list, list_id?, include_phones, request_id}` → counts and record ids. |
| `POST /prospector/export` | `{selection, include_phones, request_id}` → `text/csv` attachment. |
| `POST /prospector/verify` | `{contact_ids}` (≤ 1,000) → verdicts inline (≤ 25) or a queued job. |

A **selection** is `{view, scope, filters, rows: [{api_page, api_page_size, row_key}]}` or
`{view, scope, filters, all_matching: {limit ≤ 1000}}`. The server re-derives every row from the cache
(or re-fetches its page). A row key only *identifies* a row, and no value is ever taken from the
client. A key not found on its page (the vendor data changed) is reported `unavailable`, never
guessed.

### 4.3 Data model: migration `0072_prospector` (15 characters, parent `0071_ledger_erasures`)

1. **`prospector_pages`** (platform-global, no `tenant_id`, like `web_cache`): `id` (sha256 of kind
   + canonical API payload + page + size), `kind`, `page_number`, `page_size`, `payload_sealed`
   (Fernet), `row_count`, `total_count` (nullable), `fetched_at`, `expires_at` (indexed). It holds
   **vendor** data only, keyed by a hash of the query, never tenant data. PII sits here sealed for at
   most 24 h; `prune` deletes expired rows on the existing sweep.
2. **`prospector_purchases`** (tenant-scoped, enrolled in RLS by `apply_rls.py`): `tenant_id`,
   `fingerprint` (64), `field` (`email` | `phone` | `company`), `contact_id` / `account_id`
   (nullable links), `request_id`, `first_charged_at`, `last_delivered_at`.
   Unique `(tenant_id, fingerprint, field)`. Hashes only; never a value.
3. **`prospector_daily_rows`** (tenant-scoped, RLS): `tenant_id`, `day` (UTC date), `rows`. Unique
   `(tenant_id, day)`. The budget counter is its own table, not the usage stream, so the cost guard
   works whatever `NEXUS_BILLING_ENFORCEMENT` says (`off` records nothing).

Saved-tab person location reads `Contact.custom_fields["location"]` (`country`, `state`, `city`,
`zip`), written on save. SQLAlchemy's JSON indexing compiles to `json_extract` on SQLite and `->>` on
Postgres, so no new contact columns are needed.

### 4.4 Configuration

- `providers/catalog.py`: `"infojoy_search": ProviderSpec("infojoy_search", "InfoJoy Search (prospect database)", "infojoy_search_api_key_list")`.
  `probe` and `verify` issue a one-row contacts search (`PageSize: 1`); verify upgrades to `verified`
  like the other search providers.
- `Settings`: `infojoy_search_api_keys` (env `NEXUS_INFOJOY_SEARCH_API_KEYS`, comma-separated, with a
  `_list` property) and `infojoy_search_base_url` (default
  `https://infojoysearchapi-d5e7g0cydphvcme7.centralus-01.azurewebsites.net`, https only, never a
  request parameter).
- Runtime settings (`runtime_config/catalog.py`, each with its `effect` and a reader outside
  `config.py`): `prospector_rate_limit_per_minute` (30), `prospector_net_new_scan_rows` (500),
  `prospector_daily_rows_default` (2,500).
- **Not configured** is a clear **400**: "Prospector is not set up: a superadmin adds an InfoJoy
  Search key in Control plane > Provider keys." There is no fake fallback.

### 4.5 Billing (`nexus/billing/`)

| Capability | Kind | Rate card (seed) | Notes |
|---|---|---|---|
| `module.prospector` | module, `enabled` | — | On no plan: a strict no-op. Nav item, route and every capability below `depends_on` it. |
| `prospector.email` | metered, unit `email` | 1 credit, $0.003 | D4 |
| `prospector.phone` | metered, unit `phone` | 1 credit, $0.003 | D4, opt-in per action |
| `prospector.company` | metered, unit `company` | 1 credit, $0.003 | D5, Companies view only |
| `prospector.search_rows` | observe-only, unit `row` | cost $0.003, no price | Recorded for vendor-fetched rows only (cache hits are free to us), so browsing COGS is visible in the usage stream. Joins `OBSERVE_ONLY` in `tests/test_rate_card_conformance.py`. |
| `prospector.daily_rows` | limit holder, unit `row` | — | Its plan entitlement's `quota` is the daily row budget (D22). Read by `limits.py`, never metered. |

The margin floor (`validate_rate`) sees each card against its own cost, so 1 credit vs $0.003 is 70%.
The vendor really bills per **record**: a contact bought with both fields costs us one record and
earns 2 credits, so the per-field cost is deliberately conservative. Browsing COGS is invisible to
the per-field cards by construction, which is why `prospector.search_rows` exists.

---

## 5. Masking (D7)

The masked shapes. A row whose workspace record exists shows that record's own values instead (§6).

| Value | Raw | Masked | Rule |
|---|---|---|---|
| Email | `john.smith@example.com` | `j•••@e•••.com` | First character of the local part, first of the first domain label, last label only. A part of length 1 shows `•••`. |
| Direct phone | `+1 (415) 555-7890` | `•••-•••-7890` | Last 4 digits only when there are ≥ 8 digits; otherwise `•••-•••-••••`. |
| Domain | `example.com` | `e•••.com` | As the email's domain. |
| LinkedIn | `https://linkedin.com/company/example-inc` | `linkedin.com/company/e•••` | First character of the slug. |

Names, title, department, seniority, company name, country, state, city, industry and size range are
shown: they are the business-card facts the product already treats as searchable (`nexus/people`
leaves name, title and company unencrypted for the same reason).

**Structural guarantee.** The search row model (`ProspectRowOut`) declares no raw email, phone, domain
or URL field; only `email_masked`, `phone_masked`, `domain_masked`, `linkedin_masked` and `has_*`
booleans. Pydantic drops anything undeclared, so a value cannot leak by being forgotten in a
serializer. The one exception is the nested `record` object (`SavedRecordOut`): it is filled only for
a row the workspace already holds, and only from the workspace's own Contact/Account row (never from
the vendor payload). Those are the tenant's own values, already visible on Contacts and Accounts. **Proven by test**: for every raw value in the fixture (and its local part, domain label
and LinkedIn slug where ≥ 3 characters), the serialized JSON of `/prospector/search` (Total and Net
new), the MCP search tool's result and `/prospector/quote` contains no substring match, for every row
the workspace has not paid for.

---

## 6. Search flow (Total)

1. Validate (`filters.py`): `extra="forbid"`, ≤ 50 values per list, ≤ 120 characters per value,
   `page_size ∈ {25, 50, 100}` from the UI (≤ 500 internally), `page ≤ 400`, `StrictBool` flags.
2. **Rate limit** (`core.ratelimit.hit("prospector:{tenant}:{user}", window=60,
   limit=prospector_rate_limit_per_minute)`) → 429 with `Retry-After`.
3. **Budget**: today's rows + `page_size` must stay within the plan's `prospector.daily_rows` quota,
   else `prospector_daily_rows_default`. Otherwise 429: "Today's Prospector browsing limit (2,500
   rows) is used up. It resets at 00:00 UTC; an admin can ask for a higher limit."
4. **Cache** by `sha256(kind + canonical payload + page + size)`. On a hit, unseal. On a miss, call
   the client: success → seal and store; failure → nothing stored, and the error is mapped (§10).
   `include_count=true` sends `IncludeTotalCount: true` on that same request and caches the total
   alongside.
5. Parse → identities → **holdings** (one batched query per kind) → each row gets `saved: bool` and,
   when saved, `record` = the workspace record's id and its own `email`/`phone` (the tenant's data,
   already visible on Contacts).
6. Mask the unsaved rows; build `ProspectRowOut` (with `row_key`, `api_page`, `api_page_size`).
7. Count `rows` into `prospector_daily_rows` (all rows served, cached or not). Record
   `prospector.search_rows` for vendor-fetched rows only.
8. Log the filter **names** and counts, never values (names are PII).

**Net new (D12).** A signed opaque cursor `{kind, query_hash, api_page, offset, api_page_size}`
(HMAC over `secret_key`). The scan fetches API pages of `max(100, page_size)`, through the same cache,
skips held rows, and stops when the page is full, the data is exhausted, or
`prospector_net_new_scan_rows` (500) rows have been scanned. The response carries `next_cursor`,
`scanned`, `exhausted` and `scan_capped`, and the UI says "Scanned 500 records: 12 net new. Keep
scanning?". Scanned rows count against the budget, because they were fetched.

**Tab counts.** Saved always shows a count (cheap SQL). Total shows one only after "Show total".
Net new never does (it is unknowable without a full scan), so it shows "—".

---

## 7. The Saved tab (D13, D14)

SQL over the workspace's own data, tenant-scoped and paginated (`page`, `page_size`), newest first:

| Section | Saved-tab source |
|---|---|
| Job titles / Seniority | `Contact.title` ILIKE any, `Contact.seniority` IN |
| Company | `Account.name` ILIKE, `Account.domain` IN (normalised) |
| Company location | `Account.country`, `Account.region`, `Account.custom_fields["city"]`, `Account.postal_code` |
| Location (person) | `Contact.custom_fields["location"][...]` |
| Employees / Revenue | `Account.employee_count` / `annual_revenue` within the parsed range bounds |
| Industry & keywords | `Account.industry`, `custom_fields["sub_industry"]`, keywords over name/description |
| Technologies | `Account.tech_stack` cast to text and matched case-insensitively on the quoted value (`LIKE '%"react"%'`): one expression on SQLite and Postgres, no dialect branch. No helper exists today; this adds one to `saved.py`. |
| Phone | `Contact.phone` present |
| Email status | `Contact.email_status` IN |
| Lists | `ListItem` membership |
| Signals / Funding / Job postings | `EXISTS signal_events` for the account, kind IN (chosen kinds / `funding` / `hiring`), in the last 30, 90 or 180 days (the section's window control, default 90) |

Departments, SIC codes and Has LinkedIn are not stored on workspace records, so they show disabled on
Saved with "not stored for workspace records". Rows show real values and link to the Account or
Contact. Actions on Saved: add to list, export (the workspace's own values, free, and not metered as
`data.export`, matching D3's "nothing bought, nothing charged"), Verify.

---

## 8. Filter sections (D14–D16)

Contacts view: **Name** (FirstName, LastName) · **Job titles** (Titles; Departments; SeniorityLevels)
· **Company** (CompanyNames, CompanyUrls) · **Location** (person: country, state, city, zip) ·
**Company location** (company: country, state, city, zip) · **Employees** (EmployeeSizeRanges) ·
**Revenue** (RevenueRanges) · **Industry & keywords** (IndustryTypes, SubIndustries, SicCodes,
Keywords, each with its Exclude form where the API has one) · **Technologies** · **Phone**
(HasDirectPhone) · Saved only: **Email status**, **Lists**, **Signals**, **Funding**, **Job
postings**.

Companies view: **Company** · **Company location** · **Employees** · **Revenue** · **Industry &
keywords** · **Technologies** · **Has LinkedIn** · Saved only: **Lists**, **Signals**, **Funding**,
**Job postings**.

Small bounded sets (seniority, employee size, revenue, and departments if bounded) are checkbox lists
whose values come from `GET /prospector/filters`, recorded from the real calls (§11). Open sets
(titles, industries, technologies, keywords, SIC, names, places) are chips. Exclusions are chips with
an Exclude toggle where the API has the Exclude form. AND across sections, OR within one.

---

## 9. Delivery: save, list, reveal, company list, export, verify

### 9.1 Quote (`pricing.py`)
For each resolved row:
- **Contact**: `email` is charged if the row has an email and the ledger has no `(fp, email)`;
  `phone` if `include_phones` is set, the row has a direct phone, and the ledger has no `(fp, phone)`.
- **Company (Companies view)**: `company` is charged unless the ledger has `(fp, company)`.
- **Held rows are free**: save/list links the existing record and writes no vendor value into it;
  export writes the workspace's own values.
- **Company list from the Contacts view (D24)** and a saved contact's company: free (D5).

The confirm dialog shows the quote: "12 emails × 1, 4 phones × 1 (included), 3 already paid: 16
credits". The `include_phones` toggle re-quotes.

### 9.2 Deliver (`deliver.py`)
1. Resolve rows. For select-all, page through the API at 500 a page until `limit` (and for Net new,
   past held rows, within the scan cap).
2. Quote, then **`preflight`** every capability for its quantity. A 402 comes before anything is
   bought or written.
3. One **SAVEPOINT**:
   - Accounts: `find_existing_account(domain, name)`, else `Account(source="prospector", name,
     domain=normalise_on_write(...), country, region, custom_fields{city, industry,
     employee_size_range, linkedin_url}, owner_user_id=actor)`. The LinkedIn URL goes through
     `enrichment/linkedin.company_page_url`; a non-company URL is dropped and reported.
   - Contacts: dedupe by normalised email within the account (else name), else `Contact(account_id,
     full_name, title, seniority, email, phone only if include_phones,
     enrichment_source="sourcing:prospector", email_status=None,
     custom_fields{department, location})`.
   - List items through `lists.service.add_members` (kinds checked there).
   - Ledger rows for every newly charged field.
   - `metered(ts, cap, quantity=n, idempotency_key=f"prospector:{request_id}:{cap}",
     user_id=actor)` per capability with `n > 0`.

   A refusal rolls the savepoint back: no records, no ledger, no charge, a 402 with the upsell.
4. Return `{created, reused, listed, unavailable, charged: {email, phone, company}, record_ids}`.

A retry with the same `request_id` re-creates nothing (dedupe), charges nothing (the idempotency key
and the ledger), and returns the same counts. Long actions show `WorkingIndicator`.

### 9.3 Campaign hold (D19): new code in the engagement engine
Enrollment (`POST /engagement/campaigns/{id}/contacts`, including list expansion through
`lists.service.campaign_contact_ids`) and the candidate picker (`engagement/sequences/candidates.py`)
treat a contact as **held** when `enrichment_source == "sourcing:prospector"` and `email_status` is
not one of `valid`, `catch_all`, `risky`.

- Held contacts are skipped and counted: "3 held: email not verified yet (from Prospector). Verify
  them in Prospector or on the contact."
- The picker shows them disabled, with the same reason.
- A one-off email from the contact page is **not** held: a rep writing to one person is a decision,
  not a bulk send.
- `tests/test_prospector_campaign_hold.py` pins it. This is the only change outside `nexus/prospector/`
  that alters behaviour; it touches no existing contact, because none carries this source today.

### 9.4 Export (`export.py`)
Same quote and charge as deliver (D3), committed **before** the file is returned. A failed download is
retried with the same `request_id` and costs nothing. Columns:
- Contacts: first_name, last_name, title, department, seniority, email, direct_phone (only with
  include_phones), company_name, company_domain, country, state, city.
- Companies: company_name, domain, country, state, city, industry, employee_size_range,
  linkedin_url.

Every text cell is formula-neutralised (a leading `= + - @ \t \r` is prefixed with `'`) by a new
`csv_export.safe_cell`, used by Prospector only. The existing exports are unchanged.

### 9.5 Verify (`verify.py`, D19, D20)
- `contact_ids` (≤ 1,000), workspace contacts with an email.
- **Free** for a contact whose `enrichment_source == "sourcing:prospector"`. Any other contact
  charges `verify.email` (preflighted for that subset).
- **≤ 25: inline.** Verifier calls are network-only, run with bounded concurrency (5), and the results
  are written after the gather: the AsyncSession concurrency rule. **> 25: a durable job**
  (`prospector_verify_contacts`), and the UI says "Verifying N addresses in the background".
- A fresh verifier is used (never the registry cache), as `reverify` does. Verdicts:
  - valid / catch-all / mailbox-level risky keep the address and release the hold;
  - invalid removes it (`policy.forget_email` + `remember_rejected`): the save policy never keeps a
    disproved address;
  - unknown keeps it, still held (D19 keeps vendor emails).

### 9.6 Reveal (D8)
A row action, "Reveal email (1 credit)" or "Reveal email + phone (2 credits)", is `deliver` with
`action=reveal` for one row. The row then shows the workspace record's values and a link to it.

---

## 10. Failure handling (the client never raises)

| Upstream | Result | Cached? |
|---|---|---|
| No key | 400 "not set up" (admins get the Provider keys path) | no |
| 200, parsed | page | yes |
| `{"Errors": [...]}` (validation) | 422 with the vendor's sentence, sanitised and capped | no |
| 401/403 | rotate to the next key, and `record_rejection_from_response` marks the key red; all rejected → 502 "the data provider rejected our key" | no |
| 429 | rotate, back off once → 503 "busy, try again shortly" | no |
| Timeout (connect 5 s, read 20 s) / transport | 504 / 503 "the data provider did not answer" | no |
| Unparseable body | 502; the shape is logged without values | no |

A failed call is never cached, and a cached answer ignores an earlier failure (the
`integrations/search/engines.py` `last_failure` rule). Every screen state: skeleton rows while
loading, `EmptyState` with clear-all, `ErrorState` with Retry, a not-configured state, and a
budget-reached state.

---

## 11. The first real calls (D15, D25): before any parser

The order matters because the parser cannot be written until the shape is observed:

1. Commit 1: the provider catalog entry, the settings fields and `scripts/infojoy_probe.py`. **No
   migration.**
2. Build the image from the worktree. Tag the running image for rollback, `pg_dump` into
   `.claude/backups/`, and `up -d --no-build app worker` on the local deploy (`localhost:8099`).
3. The owner stores the token in Superadmin > Provider keys (sealed, never returned).
4. Run the probe inside the app container. It makes a handful of small calls (PageSize ≤ 10):
   - both endpoints;
   - `IncludeTotalCount` true and false;
   - each location field against a known city, to tell person-level from company-level matching;
   - candidate names for the second location set, compared by result difference;
   - `HasDirectPhone` true and false;
   - an over-500 page size (the documented error).

   It prints field names, JSON types, casing, the distinct values of the enum-like fields (seniority,
   department, size, revenue, industry), the count field's name, and which location filters changed
   results. **Every email, phone, name and URL is redacted** before printing.
5. The redacted observation is committed as
   `docs/superpowers/specs/2026-10-16-prospector-observed-api.md`, and the synthetic test fixtures are
   built from it.
6. If the calls cannot prove the second location set's field names, ask the owner for InfoJoy's names
   rather than guess: an unknown filter key may simply be ignored, and that would read as "no
   filter".

---

## 12. Frontend (`frontend/src/pages/prospector/`)

Invoke `impeccable` and `ui-ux-pro-max` before writing it; follow `DESIGN.md`.

- **Route** `/prospector`, wrapped in `RequireCapability("module.prospector")` and `RequirePage`.
  **Nav** item "Prospector" after Contacts, `capability: "module.prospector"`. Added to
  `HIDEABLE_PAGES` (pinned by `tests/test_page_visibility.py`).
- **`ProspectorPage`**: the view switch (Contacts | Companies), `ScopeTabs` (Total / Net new / Saved
  with counts) and URL state (`?view=&scope=&f=`), so Back and shared links work.
- **`FilterRail`** with **`FilterSection`** accordions: a button with `aria-expanded` and
  `aria-controls`, an active-count badge, a per-section clear, and Clear all at the top. Below `md` it
  becomes a drawer. Sections disabled on the current tab say why.
- Inputs: `ChipInput` (include/exclude), `OptionList` (checkboxes from `/filters`) and `YesAnyToggle`.
  Changes apply after an 800 ms debounce. A cached page costs nothing, and a stale request is
  cancelled with `AbortController`.
- **`ResultsTable`** (the `DataTable` primitive): a checkbox column; masked cells show an icon and the
  mask, with an sr-only "email hidden until saved"; a Saved badge with a link to the record.
- **`BulkBar`** (sticky): "N selected · Select all matching (up to 1,000)", then:
  - Contacts view: Save contacts, Add to contact list, Add their companies to an account list,
    Export CSV, Verify (Saved).
  - Companies view: Save accounts, Add to account list, Export CSV.
- **`ConfirmDeliveryDialog`** (`Modal`): the quote, the Include direct phones toggle, and a list
  picker (the lists `AddToListModal` already loads, reused in a pick-only mode). `WorkingIndicator`
  runs while delivering.
- **`Pager`**: Prev / Next, page number, size 25/50/100, "Show total". Net new shows "Keep scanning".
- States: skeletons, empty, error, not configured, budget reached, rate limited (a toast). An
  `aria-live` region announces "25 results, page 2". Fully keyboard operable; contrast ≥ 4.5:1 from
  the tokens.

---

## 13. MCP (`nexus/mcp/`)

- **`prospector_search`** (scope `read`): `{view, filters, page, page_size ≤ 100}` →
  `POST /api/prospector/search`. Masked exactly as the app; the budget and rate limit apply.
- **`prospector_save`** (scope `write`, `price_capability="prospector.email"`): `{view, filters,
  row_keys with pages | all_matching ≤ 1000, list_id?, include_phones, request_id}` →
  `POST /api/prospector/deliver`. Preflight and charges as the app. It returns counts and record ids;
  values are read through the existing contact tools, i.e. only records the workspace has paid for.
- Both `(method, path)` pairs join `gateway._ALLOWED`. Tool descriptions name no vendor
  (`nexus/mcp/brand.py`). Tested in `tests/test_mcp_tools.py`.

---

## 14. Security checklist

- **The key**: Provider keys (sealed, `key_hint` only), env fallback, sent as the `X-Api-Token`
  header, never in a URL, never logged.
- **Base URL**: fixed in config, https, not request-controlled, so there is no SSRF surface.
- **Validation**: strict models; bounded list lengths, value lengths, page size and page depth; the
  API's 500 maximum enforced before sending.
- **Tenancy**: every workspace read goes through `TenantSession`; the two new tenant tables get RLS;
  the platform cache holds vendor data only, keyed by a query hash; row keys are tenant-bound HMACs,
  so one workspace's keys mean nothing in another.
- **PII**: sealed at rest (24 h maximum), never logged (filter names and counts only), masked to the
  browser and to MCP until paid, and proven by test. Export cells are formula-safe.
- **Abuse and cost**: a per-user rate limit, a per-workspace daily budget (enforced regardless of
  billing mode), the 1,000 select-all cap, and the Net new scan cap.
- **RBAC**: `manage_accounts` (rep+) on every endpoint (D9).
- **Prospector contacts are never pooled** into the shared `people` store: the pool takes only what
  our finder found (CLAUDE.md "Shared people records"), and a test pins that `sourcing:prospector`
  contacts are skipped.

---

## 15. Testing (test-first)

- **Unit**: filters; masking (with the no-substring property); identity and HMAC row keys; the client
  over `httpx.MockTransport` (rotation, timeouts, not configured, vendor errors, never raises); cache
  (seal, TTL, failure not stored, total); holdings (batched: a query-count assertion); search
  (budget, rate limit, the Net new scan cap and cursor tampering); pricing (ledger, phone opt-in, held
  rows free, contact-view companies free); deliver (savepoint rollback on 402, idempotent retry,
  dedupe, list kinds); export (columns, formula-safe, charged once); verify (free vs charged, inline
  vs job, verdict handling); the campaign hold.
- **API**: RBAC; cross-tenant row keys and cursors rejected; the no-unmasked-PII test over every
  search, quote and MCP response.
- **Structural**: billing conformance (cards clear the floor, `OBSERVE_ONLY` updated); provider
  catalog; runtime settings (effect, reader); nav and route capability, page visibility; MCP
  allowed paths.
- **Migration**: replay test, id ≤ 32 characters, RLS on the two tenant tables
  (`tests_integration/`).
- **Frontend** (no runner; the repo reads source): the route guarded, the nav entry, the search row
  type declares no raw PII field, `WorkingIndicator` used for delivery.

## 16. Out of scope

Email-open tracking (and so Email Opened). Saved searches and alerts on new matches. Suggestion lists
for open vocabularies. Writing vendor data into the shared `people` store. Formula-neutralising the
existing Accounts/Contacts exports (a separate fix).
