# Master company / contact data layer — design and costing

Status: **design, not built**. Requested 2026-07-31; scoped deliberately as a plan first, because it
touches the working signal pipeline and a rushed version breaks the thing it is meant to make
cheaper.

---

## 1. The problem, measured

Today an `Account` is a **per-tenant row**. If 40 workspaces track Stripe, there are 40 Account rows,
and the refresh sweep crawls Stripe 40 times — 40× the search spend, 40× the outbound requests, 40×
the rate-limit pressure, for one company whose funding round is the same fact for everybody.

Measured on the live database, small as it is:

```
account rows           133
distinct domains       115
duplication            13.5%
```

13.5% is the floor, not the ceiling. It is low here only because these are mostly test tenants with
disjoint data. The duplication rate rises with tenant count and with ICP overlap, and ICPs overlap
heavily by construction — every B2B SaaS vendor selling to fintech is watching the same 200 fintechs.

## 2. Why it matters more than cost

Cost is the obvious argument and the weakest one. Three others matter more:

* **Rate limits are per-provider, not per-tenant.** GitHub gives 60 requests/hour unauthenticated
  *in total*. Forty tenants watching the same company do not get forty budgets; they exhaust one.
* **Freshness.** One shared crawl on a 6-hour cycle is fresher than forty crawls each throttled to
  stay inside a per-tenant budget.
* **Consistency.** Two workspaces looking at the same company today can see different funding
  states, because their refreshes landed at different times. That is indefensible in a sales
  conversation.

## 3. Proposed shape

```
companies                 (platform-global, no tenant_id)
  id                      canonical, e.g. sha1(normalised_domain)
  domain                  normalised, UNIQUE — the identity key
  name, industry, country, employee_count, tech_stack
  parent_company_id       self-FK: subsidiary -> parent hierarchy
  last_crawled_at         drives the shared refresh cycle
  source                  where the record came from (crawl | source_db | import)

company_signals           (platform-global)
  company_id -> companies
  kind, title, url, strength, occurred_at, dedupe_key

accounts                  (tenant-scoped, EXISTS TODAY, unchanged)
  + company_id            NULLABLE FK to companies
```

`accounts` keeps every column it has. The link is **additive and nullable**, which is what makes this
survivable: an account with `company_id IS NULL` behaves exactly as it does today.

### Fan-out

One crawl writes `company_signals`. A per-tenant projection copies (or joins) those into
`signal_events` for each tenant whose account points at that company. Scoring, alerting, inbox and
plays stay per-tenant and untouched — they keep reading `signal_events`.

Two viable projections:

1. **Materialise** into `signal_events` per tenant. More storage, zero read-path change, per-tenant
   dedupe and strength overrides keep working. **Recommended.**
2. **Join at read time.** Less storage, but every list/inbox/scoring query changes, and the tenant
   isolation story gets harder to reason about. Not recommended for a first version.

### Hierarchy

`parent_company_id` gives subsidiary → parent. It matters commercially: a funding round at the parent
is a signal for a subsidiary account, and today that link does not exist. Deliberately a plain
self-FK, not a nested set or closure table — the depth is 2–3 in practice and a closure table is
maintenance nobody will do.

## 4. Sharding and indexing — the honest version

**Do not shard yet.** Sharding a table nobody has filled is speculative complexity, and the
partitioning lesson from M26 applies: converting a live table later is a maintenance window, but
converting an empty one is free. The plan should therefore be *shardable*, not sharded:

* `companies.id` is `sha1(normalised_domain)` — a **hash key**, so a future hash-partition split is
  a mechanical change rather than a redesign. An auto-increment id would force a rewrite.
* `company_signals` is range-partitioned on `occurred_at` from day one, following the runbook in
  `scripts/partition_usage_events.sql`. It is the table that grows without bound.

Indexes, and the query each one serves:

| Index | Query it exists for |
|---|---|
| `companies(domain)` UNIQUE | identity resolution on every crawl and import |
| `companies(last_crawled_at)` | "what is due for a shared refresh?" |
| `company_signals(company_id, occurred_at DESC)` | the timeline for one company |
| `accounts(company_id)` | fan-out: which tenants care about this company |
| `accounts(tenant_id, company_id)` | per-tenant lookup without scanning the global index |

## 5. Migration path

Additive throughout, no rewrite of `accounts`:

1. `0035` — create `companies` and `company_signals`; add nullable `accounts.company_id`.
2. Backfill: for each distinct normalised domain, create a company and point existing accounts at
   it. Idempotent, resumable, and safe to run repeatedly — it only ever fills a NULL.
3. Shared crawl runs **alongside** the per-tenant one, writing only `company_signals`. Nothing
   consumes it yet. This is the step where correctness is proved without risk.
4. Compare: for a sample of accounts, diff shared-crawl output against per-tenant output. Only when
   they agree does anything switch.
5. Flip fan-out on behind `NEXUS_SHARED_COMPANY_CRAWL=true`, per-tenant crawl becomes the fallback
   for accounts with no `company_id`.

### Progress (2026-08-03)

Steps 1–3 are built. Step 4 (`diff.py`) is built but has **not been run against real data**, which
is the gate that step 5 waits on, and it needs a deployed shadow period rather than a green test run.

**Step 5's missing half, now built.** Fan-out alone does not save anything: `process_account` still
crawled every account per tenant regardless of `company_id`, so the shared crawl was **pure
additional cost** — forty workspaces tracking Stripe meant forty per-tenant crawls *plus* the shared
one. `pipeline._covered_by_shared_crawl` is the seam that makes the saving real. It skips the
per-tenant crawl only when all three hold: the flag is on, the account is linked, **and the company
has actually been crawled at least once**. The third condition is the one that is easy to miss —
backfill links accounts long before the crawler reaches them, and skipping on the link alone would
black out every newly-linked account until the shared crawl caught up. Scoring, inbox and plays are
untouched; only the duplicated crawl is shared. The result carries `signals_source`, because
otherwise "0 new signals" means both "nothing happened at this company" and "the shared crawler owns
this account", which are opposite problems.

Still gated, deliberately: `NEXUS_SHARED_COMPANY_CRAWL_ENABLED` remains **off**. The saving is now
implemented, not enabled.

Steps 3–4 are the whole point. The failure mode of this project's history is shipping a plausible
integration and discovering in production that it was pointed at the wrong company — six times in
the signal subsystem alone. A shared store multiplies that blast radius by the number of tenants, so
it gets a shadow period.

## 6. Outliers and edge cases that must be handled

Each of these has burned this codebase before, in the per-tenant version:

* **No domain.** Many accounts are name-only. They cannot join the shared store safely (name
  collision = cross-tenant data leak). They stay per-tenant crawled. Non-negotiable.
* **Domain collisions that are not the same company.** `example.com` style shared hosts, and
  agencies using a client's domain. Resolution keys on domain **plus** a name check.
* **Rebrands and domain changes.** `companies.domain` UNIQUE means a rebrand creates a second row.
  Needs a merge path with an alias table, or the timeline splits in two.
* **Tenant-specific overrides.** A workspace may correct a company's industry or employee count.
  Those edits must live on `accounts`, never on `companies`, or one tenant's correction rewrites
  everybody's data.
* **Deleted/archived accounts.** Fan-out must skip them, or a deleted account resurrects itself with
  fresh signals.
* **Per-tenant signal dedupe.** `signal_events` has a UNIQUE on `(tenant_id, dedupe_key)`. Fan-out
  must respect it, so a shared signal arriving twice does not violate the constraint.
* **RLS.** `companies` and `company_signals` are platform-global and carry **no** `tenant_id`, so
  `apply_rls.py` correctly ignores them. The fan-out writer therefore runs as the owner role — the
  same posture as the staff console and the payment webhook, and for the same reason.

## 7. Costing

| Step | Size | Risk |
|---|---|---|
| Schema + migration `0035` | S | low — additive |
| Identity resolution + backfill | M | medium — collisions, no-domain accounts |
| Shared crawl worker (shadow) | M | low — writes nothing anyone reads |
| Diff/verification harness | S | low, and it is what makes the rest safe |
| Fan-out projection + dedupe | L | **high** — touches the working signal path |
| Hierarchy + rollup signals | M | medium |

Roughly three milestones. The fan-out step is the one that can degrade working functionality and
should ship last, behind a flag, after the shadow diff is clean.

---

# External source database (superadmin-registered)

Decision taken 2026-07-31: **read-only, allowlisted queries**.

```
source_databases           (platform-global)
  id, name
  dsn_encrypted            Fernet at rest, like nexus/network/crypto.py
  kind                     'postgres'
  enabled
  last_ok_at, last_error
```

* Superadmin registers a DSN in the staff console, gated on a new `sources.manage` permission
  (**not** folded into `admins.manage` — registering a data source and granting platform power are
  different acts).
* We only ever execute **named, parameterised queries** defined in code — `company_by_domain`,
  `contacts_by_company`. No arbitrary SQL from the console, ever: an admin UI that runs free SQL
  against a production database is a blast radius and a compliance problem, and the value it adds
  over a psql session is nil.
* Connections are read-only at the driver level *and* the queries are allowlisted, because either
  alone is one mistake away from a write.
* It slots in as another **enrichment provider** behind the existing seam, tried *before* the paid
  APIs — which is exactly where the cost saving comes from. Inert until registered, clear error
  when misconfigured, never a fake fallback. Same convention as every other provider in this repo.

Failure posture: a source database being down must degrade to "fall through to the paid provider",
never to "signal collection stops". It is an optimisation, not a dependency.

---

# Part 2 — the contact side

The company half above was written first and the contact half was missing; this completes it.
Contacts are **not** a symmetric copy of the company design, and treating them as one would be the
main way to get this wrong.

## 8. Why people are harder than companies

| | Companies | People |
|---|---|---|
| Identity key | `domain` — near-perfect, stable | email changes, people have several, names collide |
| Data class | public business information | **personal data** — GDPR/CCPA apply |
| Mutation | slow (rebrand, acquisition) | fast (job changes are the *point*) |
| Erasure | rarely requested | a legal right, with deadlines |

The consequence: a shared **company** store is mostly an efficiency question. A shared **person**
store is a privacy design that happens to also save money, and it has to be built in that order.

## 9. What this unlocks that nothing else can

`job_switch` — "champion changed companies" — is already scored at **0.8**
(`agents/scoring.py`), has an alert rule (`alerts/rules.py`), an inbox label
(`inbox/service.py`) and enrichment copy (`alerts/enrichment.py`). **Nothing in the codebase has
ever emitted it.** It is dead capability of exactly the kind `feature_flag` was before M24.

It is dead because a per-tenant `Contact` row is a snapshot: there is nowhere to notice that the
person in it moved. A shared person with employment history makes the change observable **once**, and
every tenant tracking that person learns their champion just landed somewhere new. That is the
highest-value signal in B2B sales and it currently cannot be produced at all.

## 10. Proposed shape

```
people                     (platform-global, no tenant_id)
  id                       sha1(primary_email_norm) — hash key, shardable later
  primary_email_hash       UNIQUE — sha256, NOT the address itself
  email_encrypted          Fernet, same pattern as network/crypto.py
  full_name, first_name, last_name
  linkedin_url             strong secondary key when email is absent
  current_company_id  ->   companies
  current_title, seniority
  last_seen_at

person_emails              (platform-global) — one person, many addresses
  person_id -> people
  email_hash UNIQUE, email_encrypted
  verification_status      valid | invalid | unknown   <- the expensive bit worth sharing
  verified_at

person_employment          (platform-global) — the history, and the job-switch source
  person_id -> people
  company_id -> companies
  title, seniority
  started_at, ended_at, is_current

contacts                   (tenant-scoped, EXISTS TODAY, unchanged)
  + person_id              NULLABLE FK to people
```

As with accounts, `contacts` keeps every column it has and the link is **nullable**. A contact with
`person_id IS NULL` behaves exactly as it does today.

## 11. The line between shared and tenant-private

This is the load-bearing decision. Getting it wrong leaks one customer's work to another.

**Shared** — factual, expensive to obtain, and true regardless of who asks:
name, title, seniority, employer, LinkedIn URL, employment history, **email deliverability verdict**.

That last one is where most of the money is: email verification is billed per check, and the answer
"this mailbox exists" does not differ by tenant.

**Tenant-private, never shared** — notes, call records, cadence state, engagement history, custom
fields, scores, and **the fact that this tenant tracks this person at all**.

That final item is not a detail. The shared store must never answer "which tenants have this
person" — that reveals a competitor's target list. Concretely: no endpoint may accept a `person_id`
and return tenants, and fan-out queries must always start from a tenant and join outward, never the
reverse.

## 12. Email storage: hashed for lookup, encrypted for use

A global plaintext email index spanning every tenant is a breach magnet, and probably the single
most dangerous table this system could hold. So:

* the **index** is `sha256(normalised_email)` — enough to resolve identity, useless if exfiltrated
  without already knowing the address;
* the **value** is Fernet-encrypted, reusing `nexus/core/crypto.py`, key derived from `secret_key`
  unless a dedicated key is set — exactly the pattern already used for OAuth tokens and TOTP seeds.

Resolution therefore never needs to decrypt: hash the incoming address and look it up. Decryption
happens only when a tenant that legitimately holds the contact displays it.

## 13. Reuse the resolver that already exists

`nexus/network/resolution.py` already implements person identity resolution for the relationship
graph — exact normalised email, else normalised name + company. **Use it.** A second, subtly
different resolver would create two disagreeing notions of "same person" in one codebase, and the
disagreement would surface as duplicated or wrongly-merged contacts that nobody can explain.

## 14. Erasure, and why the shared store is *better*

Today a right-to-erasure request means finding a person across every tenant's `contacts` table and
hoping none were missed. With `people`, erasure is one row plus a cascade, and it is auditable.

Deletion has to be **asymmetric**, and this is easy to get backwards:

* Tenant A soft-deleting their contact → hides it for tenant A. The `people` row is untouched and
  tenant B is unaffected.
* A genuine erasure request → tombstone the `people` row, cascade a hard delete to every tenant's
  `contacts`, and record the request. This is the only path that crosses tenants.

Conflating the two would let any user erase a person from every other customer's CRM.

## 15. Sharding and indexing

Same posture as companies: **shardable, not sharded**. `people.id` is a hash so a future
hash-partition split is mechanical. `person_employment` is the table that grows without bound and is
range-partitioned on `started_at` from the outset.

| Index | Query it exists for |
|---|---|
| `people(primary_email_hash)` UNIQUE | identity resolution on every enrichment |
| `people(linkedin_url)` | resolution when there is no email |
| `people(current_company_id)` | "who do we know at this company" |
| `person_employment(person_id, is_current)` | current role for one person |
| `person_employment(company_id, is_current)` | staffing a buying committee |
| `person_employment(company_id, started_at DESC)` | **job-switch detection** |
| `contacts(person_id)` | fan-out: which tenants care about this person |
| `contacts(tenant_id, person_id)` | per-tenant lookup without scanning the global index |

## 16. Edge cases specific to people

* **Two people, one email** (`info@`, `sales@`, shared inboxes). Role addresses must not become a
  person; keep a denylist of local-parts and fall back to name + company.
* **One person, many emails** — work, personal, post-acquisition alias. That is what `person_emails`
  is for; the primary is whichever last verified `valid`.
* **Name collisions.** Two "John Smith"s at one company is real. Never resolve on name alone without
  a company, and never merge on a fuzzy match without an exact secondary key.
* **A person with no email at all.** Common for LinkedIn-sourced contacts. They resolve on
  name + company, which is weaker — record the confidence so a low-confidence merge is reversible.
* **Job change vs data correction.** "Title changed" is not "moved companies". Only a change of
  `company_id` emits `job_switch`; a title edit closes and reopens employment at the same company.
* **A tenant's private correction.** If a tenant fixes a title, it lives on `contacts`, never on
  `people` — otherwise one customer's edit rewrites everyone's data. Same rule as companies.
* **Re-import churn.** A CSV upload must not mint a second person for someone already known, so the
  resolver runs on import, not only on enrichment.

## 17. Costing (contact side)

| Step | Size | Risk |
|---|---|---|
| `people` / `person_emails` / `person_employment` + nullable `contacts.person_id` | S | low — additive |
| Hashed + encrypted email storage | S | low, and the part that must not be deferred |
| Resolution wired to `network/resolution.py` + backfill | M | **high** — bad merges are hard to unpick |
| Shared verification verdict reused before billing a check | S | low, and where the saving lands |
| Employment history + `job_switch` emission | M | medium — finally lights up a dead signal kind |
| Erasure cascade + audit | M | **high** — legal exposure if wrong |

## 18. The decision this design cannot make on its own

Sharing personal data across tenants needs a **lawful basis**, and that is a legal question rather
than an engineering one. The architecture supports either answer:

* **Shared people store** — maximum saving, `job_switch` becomes possible, erasure gets easier;
  requires the privacy position to be settled first.
* **Per-tenant contacts, shared *company* store only** — no cross-tenant personal data at all. Keeps
  most of the crawl saving (companies are the expensive part), loses `job_switch` and the shared
  verification verdict.

Build the company layer first either way: it is unambiguously safe, it carries most of the cost
saving, and it does not depend on this answer.

---

## External source database — decisions locked (2026-08-04)

**Platform-wide only. Superadmin only. Dry run before activation.**

Ruled out: per-tenant sources ("customer brings their own warehouse"). That is a different feature
wearing the same clothes, and mixing them is expensive to unpick once data has landed:

| | Platform-wide (building this) | Per-tenant (not building) |
|---|---|---|
| Results land in | `companies` / `people` — shared, no `tenant_id`, RLS deliberately skipped | `accounts` / `contacts` — tenant-scoped, RLS-enrolled |
| Registered by | `sources.manage`, a **platform** permission | workspace owner — **tenant** RBAC, a different gate entirely |
| Cost effect | The point: one vendor licence amortised across every tenant, tried ahead of the paid APIs | None. A customer-facing integration, valuable but not a saving |
| Bad-mapping blast radius | Every tenant at once | One workspace |
| Credentials held | Ours | The customer's — materially higher liability |

Writing to the wrong table is not a config change: platform data in a tenant table is duplicated N
times, tenant data in the shared store is a cross-tenant leak. Per-tenant later would reuse ~80% of
this (introspection, mapping, dry run) plus a `tenant_id`, a different permission gate, and
different write targets.

### Build order

1. **Foundation — done** (`nexus/sources/safety.py`, commit `c3c94fd`). SSRF guard on the DSN
   (resolve-then-check, so a public name pointing at loopback is caught) and identifier validation
   that runs on names we discovered ourselves, because a table name is attacker-controlled if the
   attacker owns the source database.
2. **`source_databases` model + migration — done** (`nexus/models/source_db.py`, migration
   `0041_source_databases`). DSN Fernet-sealed under its own key (`nexus/sources/crypto.py`);
   `last_ok_at` / `last_error` so a source that has quietly stopped working is visible.
3. **Connection test — done** (`engine.test_connection`). `validate_dsn` first, always, and it
   *asserts* the session is read-only rather than assuming it.
4. **Schema introspection — done** (`engine.introspect`). Over `information_schema`; every returned
   name through `require_identifier` before it is stored or shown, and an unsafely-named table is
   skipped rather than failing the whole pass.
5. **Mapping — done** (`engine.validate_mapping` / `engine.build_select`). Admin picks discovered
   table/column onto app fields. **No SQL string ever arrives from the browser** — we build
   parameterised queries from revalidated identifiers, and revalidate again at build time because
   a mapping loaded back from our own JSON column is not thereby trustworthy.
6. **Dry run — done** (`engine.dry_run`, `service.run_dry_run`). Reads a bounded sample through the
   mapping and reports what it would produce, writing nothing. A source is not selectable until a
   dry run has passed. Same staging as the shared company crawl (shadow → diff → fan-out), for the
   same reason: a plausible integration pointed at the wrong column is the failure mode this
   subsystem has shipped six times.
7. **Enrichment provider — NOT built yet.** Tried before the paid APIs. **Failure posture: fall
   through to the paid provider, never stop collection.** It is an optimisation, not a dependency.
   Deliberately left out of the same change as steps 2–6: shipping the consumer alongside the
   ladder makes it possible to skip the proof by accident.

**Open question — answered.** `allow_private` is a **setting** (`NEXUS_SOURCE_DB_ALLOW_PRIVATE`),
false everywhere by default, and never a request parameter: an admin must not be able to switch off
the guard from the form the guard exists to protect. It may be turned on for local development,
where a source database on localhost is genuinely plausible, and
`Settings._reject_private_source_dsn_in_production` **refuses to start** if it is true while
`NEXUS_ENV` is `staging` or `prod` — the same shape as `_reject_synthetic_signals_in_production`,
because a guard silently ignored is worse than one that was never there.

**Status ladder as built.** `registered → connected → introspected → mapped → verified`, advanced
only by `nexus/sources/service.py` and never from a request body. Re-introspecting or re-mapping
clears the dry-run proof. Verification does not activate: `enabled` starts false and enabling below
`verified` is refused, while disabling is never refused. Pinned by `tests/test_source_databases.py`.
