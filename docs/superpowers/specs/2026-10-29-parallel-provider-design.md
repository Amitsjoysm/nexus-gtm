# Parallel as a company and contact source (2026-10-29)

Asked by the owner: "add a new provider for contact search and company search, finding new companies
and contacts of the companies, at the same credits as Exa, with cost optimisation: make the most of
single calls, every call counts." Every Parallel FindAll page, Entity Search, Decisions, pricing,
rate limits, errors and webhook security were read first (docs.parallel.ai, llms.txt index,
2026-10-28/29). The Entity Search playground needs a Parallel sign-in and was not used.

## Owner decisions (2026-10-29)

1. **Companies: Entity Search finds, Decisions checks.** Not FindAll.
2. **Company chain: shared store, LinkedIn, Parallel, then Exa** as the fallback.
3. **Contacts: pool, InfoJoy, LinkedIn, Parallel people search, then web.**
4. **Same credits as Exa**: a company added is `discovery.account_added` (5 credits), a contact's
   kept email is `enrich.contact` (4), exactly as today. What Parallel costs us is recorded per
   call on observe-only capabilities, like `search.web` for Exa.

## Why not FindAll

| | Price | Per company added (5 credits = $0.05) |
|---|---|---|
| FindAll `base` | $0.25 a run + $0.03 a match | $0.035-0.055: at best 30-40% margin, under the 50% floor, a loss on small runs |
| FindAll `core` / `pro` | $2 + $0.15 / $10 + $1 | far underwater |
| Entity Search | $0.005 a request, 100 results included | about $0.0001-0.005 |
| Decisions | $0.001 a request, up to 100 questions about one entity | $0.001 |

FindAll is also asynchronous (10 s to 2 h). Entity Search and Decisions answer in seconds, so the
same step serves Populate, the daily sweep and the Orchestrator's chat request.

## What each call is asked, and why it is enough

**Entity Search** (`POST /v1beta/findall/entity-search`): `entity_type`, a natural-language
`objective`, `match_limit` 5-1,000. Returns `name`, `url`, `description`; ranked, NOT verified,
recall over precision. Its docs: ask for more than needed and filter downstream; keep the objective
to the core criterion. So the objective names the ICP's industries and countries only; size and
everything else are checked afterwards.

**Decisions** (`POST /v1/decisions`): `state` (the entity) plus up to 100 named questions sharing one
set of retrieved evidence; `choice` questions return one option and probabilities, `refusal` is a
successful answer to handle, citations are per call. Its docs: one request per entity. Per company,
ONE request asks:

- `industry`: choice over the ICP's industries plus `other` and `unknown`, judged by what the
  company itself does (the owner's rule: an equipment maker is Manufacturing).
- `hq_country`: choice over the ICP's countries plus `other` and `unknown`: the headquarters.
- `size_band`: choice `below`, `within`, `above`, `unknown` against the ICP's employee range.
- `identity`: choice `own_independent` (the website is this company's own and it is not a brand or
  unit of another company), `part_of_another`, `not_this_company`, `unknown`. This is the acquired
  brand check (Hess at chevron.com) asked of evidence rather than of a homepage title.

A company is delivered when `identity` is `own_independent`, no answer falls outside the ICP, and
every MANDATORY field answered inside it (no data is no match, D3). A refusal counts as `unknown`.

## Every call counts

- **Use what the price includes.** One Entity Search request costs the same for 5 or 100 results,
  so ask 100. Every result whose domain survives `vetting` goes into the shared company store
  (source `parallel`, codes derived from its own description), so later Populates, for any
  workspace, take it from the store without asking anyone.
- **Never buy the same answer twice.** Entity Search answers are cached in `web_cache` for 24 h by
  objective; Decisions verdicts for 90 days by domain and question set. A failed call is never
  cached.
- **Spend checks only on survivors, only as many as needed.** Free gates first (held, taken,
  `company_domain`, `vetting.names_company`, associations), then Decisions a batch at a time until
  the shortfall is met, as `require_buyer` does with InfoJoy.
- **One request per company carries every check.** Four answers for $0.001.
- **The verdict is evidence, kept.** The account's industry and country carry the citation as
  their source (`field_sources`), and the employee band is kept on the account
  (`custom_fields.employee_band`), which the size gate reads when no exact count exists.
- **Contacts: one people request per account.** 100 people for $0.005, each proven locally (the
  description names the company, the title passes `title_fits`, a LinkedIn link is a person's own
  profile); the proven ones go to the shared people pool for every workspace.
- **Cost is recorded per call** (`search.entity`, `verify.decision`, observe-only), so the margin of
  every Parallel-found company is visible in the usage stream.

## Failure posture

Inert until keyed (Provider keys, `parallel`). Unkeyed: a note `not_configured` and the chain moves
on to Exa. 401: the key is marked red (`record_rejection_from_response`) and the next is tried.
402 (Parallel balance): `out_of_credit`, the chain moves on, logged at WARNING. 429/5xx: one retry
with backoff, then `failed`. Never raises into Populate, the sweep or Find contacts.

## Out of scope (named, not built)

FindAll (async, under the margin floor), webhooks (polling-free design needs none), Parallel
memory (never changes results), Monitor, Task API enrichments.
