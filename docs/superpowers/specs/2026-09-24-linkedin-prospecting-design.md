# LinkedIn prospecting: database first, then LinkedIn actors, then Exa

Decided with the product owner 2026-09-24. Replaces "Exa only" for ICP company discovery, Find similar
companies and contact sourcing.

## Measured before designing (one approved run of each actor, 2026-09-24)

- `harvestapi/linkedin-company-search` (taHaRcqil3scbchuI), full mode: returns `website`,
  `linkedinUrl`, `industries[{id,name,hierarchy}]`, `employeeCount` + `employeeCountRange`,
  `locations[].parsed.countryCode`, `similarOrganizations`, and `_meta.pagination`
  (`totalResultCount` 3,032, `totalElements` 1,000, 20 pages x 50). Outlier: Lightning AI reports 286
  employees inside the 51-200 range — the RANGE is what the search filtered on.
- `similarOrganizations`: 60 across 5 companies, **0 with a website**. They cannot become accounts
  without a lookup.
- `harvestapi/linkedin-company-employees` (Vb6LZkh4EqRlR0Ka9), short mode, asked for sales leaders at
  `linkedin.com/company/vanta`: returned ONE row — a Fleet Coordinator at Flex E Lease. Wrong company,
  wrong title. The output field is `currentPositions` (docs say `currentPosition`), there is no
  headline in short mode, and `linkedinUrl` is an opaque `ACw…` id, not a vanity URL.
- `harvestapi/linkedin-company` (UwSdACBp7ymaGUJjS): company details by URL or by name, bulk,
  $0.004 each. Added (product owner) to resolve similar companies' websites and an account's
  LinkedIn page.
- Of the two configured Apify keys, the first answers 401 (revoked).

## Decisions

| Question | Decision |
|---|---|
| Source order | Shared database always first, then the actor for the shortfall, Exa only if the actor delivers nothing or fails |
| Credits | Same price per company whatever the source: `discovery.account_added` (5 credits), preflighted for N, charged per company delivered |
| Contact email | Actor for people (short mode, ICP titles); our verified finder for email |
| Third actor | Yes — `linkedin-company` for similar-company websites and an account's LinkedIn page |
| **Domain** | **The domain defines everything about an account.** An actor company becomes an account only through a verified company domain; see below |

## The domain gate (product owner: "companies are always mapped with the correct domain")

- `website` → `normalise_domain` (free mail, reserved, shorteners refused) → then refused if the host
  is social, directory, app store or link-in-bio (`linkedin.com`, `facebook.com`, `instagram.com`,
  `x.com`, `twitter.com`, `youtube.com`, `crunchbase.com`, `apps.apple.com`, `play.google.com`,
  `linktr.ee`, `beacons.ai`, `bio.link`, …) — the existing `_NON_COMPANY_HOSTS` plus these.
- No usable website → **no account**. The LinkedIn record may still be kept in the shared store only
  if it has a domain; nothing domainless is ever stored or delivered.
- An account's LinkedIn page found by name is used only if the page's website normalises to **the
  account's own domain**. A name match is not proof.
- Two LinkedIn pages on one domain (a subsidiary on the parent's site): the domain wins — one company
  row, blanks filled, nothing overwritten.

## Components

1. **Industry codes** (`nexus/prospecting/industries.py`): the 434 LinkedIn v2 industries, vendored as
   `nexus/data/linkedin_industries_v2.csv`, loaded once into an in-memory cache (434 fixed rows is
   faster from memory than a table). `map_industries(terms)`: deterministic label/hierarchy match
   first; the LLM only for terms that miss, choosing from a ~20-label shortlist; the result is stored
   on the ICP (`icp.linkedin_industry_ids`) when the ICP is saved.
2. **Shared store fields** (migration `0061`): `companies.linkedin_url`, `linkedin_id`,
   `linkedin_industry_id` (indexed), `employee_range_min/max`, `hq_country_code` (indexed),
   `description`, `linkedin_fetched_at`, `similar_linkedin` (JSON). Plus `prospect_cursors`
   (tenant-scoped: which result pages of which query this workspace has consumed) and `prospect_runs`
   (a populate request's progress and outcome).
3. **Actor clients and parsers** (`nexus/prospecting/linkedin.py`), on the existing `ApifyClient`
   (key rotation, refusal detection). Parsers are tested against the captured output's shape.
4. **The company chain** (`nexus/prospecting/companies.py`) `find_icp_companies(ts, n)`:
   database (ICP codes, country, size band, NOT held by this workspace — a SQL anti-join) → actor
   pages from the workspace's cursor, every page's companies stored in the shared store, only the
   shortfall delivered → Exa. Past the 1,000-result ceiling the search splits by country and size
   band, each variant with its own cursor. Used by populate and by the daily discovery sweep.
5. **Populate on ICP save**: `POST /discovery/populate {count}` preflights `count` x 5 credits,
   enqueues a job, returns a run id; `GET /discovery/populate/{id}` reports progress. Accounts are
   created owned by the requester and charged per company delivered. The Relevance page asks
   "How many companies now?" (10/20/50/100/custom) with the cost and the balance.
6. **Find similar** (phase 2): account LinkedIn page (stored, or found by name and domain-verified) →
   `similarOrganizations` → websites resolved in one `linkedin-company` run → domain gate → workspace
   dedupe → deliver; Exa fallback.
7. **Contacts** (phase 3): employees actor for the account's verified LinkedIn page, `jobTitles` from
   the ICP's buyer titles, short mode. A row is used only if a current position names the account's
   company AND its title fits the ICP. Dedupe by canonical LinkedIn URL and by name within the
   account. Emails from the verified finder.

## Failure posture

A dead key (401), an unapproved actor (403), a timeout or an empty answer falls through to the next
source and is recorded against the key; nothing crashes a sweep. Every discarded row is counted in
the run's outcome (wrong domain, no website, wrong company, wrong title, duplicate), never silently
dropped. Credits: preflight for N before any spend; charge only what was delivered.

## Scale: a workspace holding 3,500 accounts

Today: dedupe is correct (every candidate is checked against all held domains) but discovery
starves — only 256 held domains reach the search as exclusions, so the same top results return each
day and nearly all are already held. After: the database step is an anti-join against the
workspace's accounts (indexed, unbounded); the actor step starts from the workspace's own cursor so
it never re-reads pages it has consumed, and splits past the 1,000 ceiling; the normalised-domain
check stays as the backstop at insert.
