# SDR team feedback: root causes and plan (2026-10-26)

Feedback from the SDR team (team leads Vishal and Abhishek), who used InfoJoy GTM for day-to-day SDR
work. This document holds the diagnosis and the plan only. **No code has changed.** Work starts once
the owner has answered the decisions in section 8.

Read against local master `0d7c1fb`. Measured on the local stack's Postgres with read-only queries,
and with the real scoring engine run offline in a throwaway container (`--network none`) over an
export of local accounts. Nothing was written to any database, and no paid provider was called. The
SQL is in the appendix.

**Not seen yet:** the team's feedback files (detailed feedback, plus their recommended Dispositions
and Categories for Email and Calling reps). We also don't know which environment they tested on, or
which screens they used (Relevance → Populate / daily discovery, the Orchestrator, or Prospector).

---

## 1. Data accuracy

### 1a. "We chose Oil & Gas; it pulled Manufacturing companies and labelled them Oil & Gas"

**What the code does.** ICP discovery (`prospecting/companies.find_icp_companies`) asks the shared
store, then LinkedIn, then web search for any shortfall (`companies.py:539-607`). The web step calls
`DataSourceRegistry.company_search` (`companies.py:496`). The only real company-search provider is
`SearchBackedCompanySearchProvider`, which queries Exa with "<industries> companies in <countries>"
(`integrations/company_search.py:227-237`). It then **stamps the ICP's first industry and first
country onto every hit**:

```python
# integrations/company_search.py:301-320
industry = (icp.get("industries") or [None])[0]
country = (icp.get("geo") or [None])[0]
...
CompanyCandidate(..., industry=industry, country=country, ...)
```

**Root cause.** The label is the search query, not anything the company or a data source said.
Exa ranks pages that *mention* oil and gas, so equipment manufacturers and software vendors serving
the industry come back, and every one is labelled "Oil & Gas" in the ICP's first country. The
stamping has been there since `a9a9e908` (2026-06-04). `deliver._account_for` was later written on
the assumption that candidates carry only what a source reported ("Never the ICP's own values",
`prospecting/deliver.py:36-38`), but the adapter upstream breaks that assumption.

**Why it is permanent and spreads to other workspaces:**
1. Account enrichment fills **blanks only** (`enrichment/account.py:294`, `:318`), so the stamped
   industry and country are never corrected.
2. The fit score then credits the ICP for matching itself: `industry '<stamp>' is in ICP`
   (`relevance/engine.py:159-161`) and geo 1.0 (`:185-187`).
3. The stamped country also defeats the hard geography gate, `_within_geo`
   (`discovery/auto.py:154-175`), because a stamped country always matches.
4. The account is backfilled into the shared `companies` table, and
   `companies/resolution.derive_codes` (`resolution.py:139-154`) turns the stamped text into a
   LinkedIn industry code, an HQ country and an office row.
5. `_from_shared` (`companies.py:295-318`) then offers that row to **every other workspace** whose
   ICP names the same industry. Its screen passes, because the stamped text is in their ICP.
6. When LinkedIn later reads the company, `store_companies` replaces the code but keeps the
   stamped `companies.industry` text, which is fill-blanks (`companies.py:193-198`). The account
   label stays wrong.

**Measured (local).**
- Nexus Platform's ICP lists "Manufacturing" first and "United States" first. 111 web-found
  accounts are labelled exactly "Manufacturing", and **111 of 111 say "United States"**. A random
  sample is mostly software vendors: Zuora, Maxio, Syspro, IsoMetrix Software, Nakisa, sa.global,
  Velixo. uts.ae (UAE) is filed as United States.
- Infojoy holds 65 accounts labelled "Manufacturing". **All 65 are also held by Nexus Platform**:
  52 came through shared rows with derived code 25, and 13 through LinkedIn-read rows.
- The shared store holds **202 rows** that LinkedIn never read, carrying a stamped industry and
  "United States". Any workspace can be offered them.
- Infojoy (first ICP industry "Software & SaaS") has 62+18 accounts labelled exactly
  "Software & SaaS". Plan Check, whose only industry is "Software & SaaS", has 26.

**A second, smaller cause: LinkedIn's own label.** LinkedIn candidates are treated as ICP matches
"by construction" and skip every gate (`deliver.py:3-5`, `:96-105`). The industry is the one the
company picked for its own page. The 13 LinkedIn-read rows above are consumer brands that LinkedIn
files under "Manufacturing" (Skin Gym, K18 Hair, Jones Road Beauty, DUDE Wipes, Smalls). For Oil &
Gas, a valve or pump maker that labels itself "Oil and Gas" passes the same way. This is data
quality, not a code bug, but nothing cross-checks it today.

**A third cause: a search vendor's domain filed as a company.** Exa's `category=company` sometimes
returns its own profile page. `exa.ai` is not in `_NON_COMPANY_HOSTS`
(`company_search.py:73-89`), so three accounts in three workspaces are filed under `exa.ai`:
Waystar, Intuit Mailchimp and Eyelit Technologies. Domain dedupe would merge them, and contact
finding would guess `@exa.ai` addresses.

**The mapping itself is right.** `map_industries(["Oil & Gas"])` gives `[57]` "Oil and Gas", whose
descendants are Oil Extraction and Natural Gas Extraction. Some neighbours are weak, though:
- "Energy" maps to 3243 *Services for Renewable Energy*, a professional-services code.
- "Oilfield Services" and "Upstream Oil & Gas" partly miss and go to the LLM shortlist.

**Edge cases.**
- The same stamping hits the Orchestrator's web fill (`agents/discovery.py` uses the same
  registry).
- A multi-industry ICP stamps only its first industry, so a list made for "Oil & Gas, Utilities"
  labels everything "Oil & Gas".
- A rep who corrects an account's industry by hand is never overwritten, which is correct. The
  shared row keeps the stamp.
- Prospector (InfoJoy) shows InfoJoy's own `IndustryType` (`prospector/records.py:134`). If the
  team used Prospector, the label is the vendor's, and the fix there is SIC codes or exclusions,
  not this bug. InfoJoy also fails on its side for `IndustryTypes` plus a country: 500 after
  120 s (observed-API doc, 2026-10-22).

**Proposed fix.**
- Candidates carry only what a source reported. The stamp goes; the industry comes from evidence
  (decision D1).
- Search-vendor hosts are refused as domains.
- A dry-run cleanup clears the stamped values on accounts and shared rows, then re-derives them
  (D2).
- A LinkedIn read replaces derived or stamped `companies.industry` text, not only the code.
- Regression tests:
  - a web hit never carries the ICP's industry or country unless the source stated it;
  - a stamped shared row is never offered to another workspace;
  - `exa.ai` is never an account domain.

### 1b. "Contacts often came back 'not found', even after trying different buyer titles"

**What the code does.** Find contacts is `POST /accounts/{id}/source-contacts`
(`routers/accounts.py:767-782`), which calls `contacts/sourcing.source_account_contacts`
(`sourcing.py:123-217`). The chain:
1. The shared people pool.
2. The account's **verified** LinkedIn page (`prospecting/similar.verified_page`,
   `similar.py:101-124`). If the shared company has no page, LinkedIn is searched **by account
   name**, and the result is used only when its website equals the account's domain.
3. The employees actor, with the ICP's titles as `jobTitles`, at most 10
   (`prospecting/contacts.py:99-101`).
4. Each row must name this company exactly (`linkedin.employee_fits`, `linkedin.py:360-380`) and
   fit a title.
5. Only when LinkedIn found nobody does it fall back to Exa people search
   (`sourcing.py:149-217`).
6. Emails come only from pattern guessing plus verification (`enrichment/waterfall.py:304-318`).
   `unknown` is not saved (`enrichment/policy.keep_address`).

**Root causes, most likely first.**
1. **The reason is thrown away.** `linkedin_contacts` returns notes (`no_titles`,
   `no_verified_page`, `nobody_fits`, `not_configured`, `failed`) and discard counts
   (`wrong_company`, `wrong_title`). `source_account_contacts` only *logs* them
   (`sourcing.py:142-144`). The API returns a bare list, and the page says "Couldn't source
   net-new people for this account right now." (`AccountDetailPage.tsx:446-450`). So the team
   changed titles when titles were not the problem. When the page cannot be verified, no title
   helps.
2. **There is often no verified page.** Locally, 54% of discovery accounts and 72% of Orchestrator
   accounts have no LinkedIn page on their shared company. Each Find contacts then searches
   LinkedIn by *account name*. For web-found accounts that name came from a search-result title
   ("Service Works Global (SWG)", "Processia, an Eviden business"), and the domain was sometimes
   wrong (`exa.ai`), so the proof fails.
3. **Titles are sent to LinkedIn as typed.** "Chief Revenue Officer (CRO)" and "Founder / CEO" go
   in with their parentheses and slashes. `icp_title_targets` also appends `job_levels` codes
   (`head`, `c_level`, `vp`, `director`) as if they were titles (`lookalike/contacts.py:316`), so
   the 10th title sent is "head".
4. **There is no data-vendor source.** InfoJoy has contacts with emails and direct phones and is
   already contracted, but only Prospector uses it. Find contacts never asks it.
5. **Some contacts have no address because the mailbox cannot be proven.** The policy drops
   `unknown`. Enterprise mail gateways (Mimecast, Proofpoint, on-prem Exchange), which are common
   at Oil & Gas companies, often answer unknown or catch-all. The person is saved without an email
   (`waterfall.py:247-273`).

**Measured.** There are only 23 contacts locally, so this path cannot be sized without spending
money (D-measure). Of the two `sourcing:linkedin` contacts, neither has an email.

**Edge cases.**
- `employee_fits` needs an exact (normalised) company name, so a person whose current role names a
  parent or a renamed brand is dropped as `wrong_company`.
- An ICP with no titles never asks LinkedIn (`no_titles`).
- The email finder **overwrites the contact's person source** with the email source
  (`waterfall.py:272-273`). A LinkedIn-found person becomes `pattern_verified`, which also bears
  on 1c and on the campaign send gate that reads `sourcing:` prefixes.

**Proposed fix.**
- The API returns the outcome and its reasons. The page says which step stopped and what to do:
  "We couldn't prove this company's LinkedIn page. Add it here", "3 people found, no email
  verifiable", "LinkedIn returned 12 people, none in your titles".
- `job_levels` become a level filter, not titles.
- Titles are normalised into variants before they are sent.
- InfoJoy becomes a contact step (D6).
- "Person found, email not verifiable" is shown apart from "nobody found".

### 1c. "Show the source of every account and contact so SDRs can validate it"

**What exists.**
- `Account.source` is a route code (`auto_discovery`, `discovery`, `csv_import`). The Accounts
  page groups it into four buckets (`AccountsPage.tsx:49-60`).
- `Contact.enrichment_source` is one string, overwritten as above, and no screen shows it except
  Prospector's Saved tab.
- The registry computes **per-field provenance** for company candidates
  (`registry.py:238-247`), but it is dropped when the `Candidate` and `Account` are built
  (`companies.py:531-534`, `deliver.py:31-41`).
- There is no evidence link (the LinkedIn page, website page, search hit or InfoJoy record) and no
  "as of" date per field.

**Root cause.** There is no provenance model. One overloaded column holds "how the row arrived".

**Proposed fix (D7).**
- Per-field provenance for the account fields (domain, industry, country/HQ, size, revenue, tech)
  and the contact fields (person, title, email, phone), each with the source, an evidence URL or
  record reference, and the date.
- The person's source is kept apart from the email's source.
- Shown on the account and contact pages and in exports: "Industry: Oil and Gas, from LinkedIn
  page (link), 2026-10-20".

---

## 2. ICP fit and mandatory filters

**What the code does.** `RelevanceEngine.score_icp_fit` (`relevance/engine.py:135-257`) is a
**weighted mean** with no hard criteria:
- weights: industry 0.35, size 0.30, geo 0.15, tech 0.20, and when the ICP sets them, region and
  postal 0.075 each and revenue 0.30;
- unknown size, tech, region, postal and revenue score a neutral 0.5;
- unknown industry scores 0.

Hard gates exist only for **known** size and country, and only for web and shared candidates
(`deliver.py:96-100`). LinkedIn and database candidates skip every gate and the threshold. Industry
and tech are never hard.

**Root causes.**
1. **Most "60-70% while failing industry" accounts do not fail industry in the data. They carry
   the stamp from 1a.** Over the exported local accounts, the real engine shows that in Nexus
   Platform **116 of the 117** accounts that pass industry while tech does not are stamped, and
   they score 70-89. In Infojoy, 70 of 151 such accounts are stamped.
2. **A genuine industry miss can still score 58-67.** It needs revenue known and in band, or a
   region in the ICP. For example: industry 0, size 1, geo 1, tech unknown 0.5, revenue 1 gives
   (0 + .30 + .15 + .10 + .30) / 1.30 = **65**.
3. **Tech can almost never pass or fail on evidence.**
   - Only **117 of 564** live accounts (21%) have any stack, and those stacks are website
     infrastructure: Cloudflare, DMARC, Let's Encrypt, Proofpoint.
   - `required_tech` is compared by exact lower-case equality, with no aliases.
   - The Relevance form splits on commas (`RelevancePage.tsx:62-66`), so Nexus Platform's
     "CRM (Salesforce, HubSpot, Zoho)" is stored as `"CRM (Salesforce"`, `"HubSpot"`, `"Zoho)"`.
     Those fragments can never match.

**Edge cases.**
- LinkedIn's location filter matches **any office**, so a company headquartered elsewhere passes a
  country ICP. A mandatory country needs an HQ-or-any-office rule (D3b).
- LinkedIn gives headcount as a range. A band that straddles the ICP's edge needs a rule.
- A rep's hand-added account must not be silently archived for failing a mandatory field. Flag it.
- Learned weights from the outcome loop must never relax a mandatory field.

**Proposed fix.**
- The ICP gets `mandatory` per criterion: Industry, Employee size, Country/Region, Tech stack, and
  Job titles for contacts.
- **One gate**, `icp_gate(icp, account) -> pass | fail(field) | unverified(field)`, runs before
  any score. Every path uses it: deliver for **all** sources, the Accounts and list filters, list
  building, campaign enrolment and the Prospector save hint.
- A failed gate shows "Fails: Industry", not a percentage.
- The gate takes an ICP value, not the tenant's single profile, so per-sub-campaign ICPs (point 5)
  plug straight in.
- Tech input becomes chips, not comma-split text, plus an alias table; the data source is D4.
- How unknown values are treated is D3.

---

## 3. Intent and signals inside ICP filtering

**What the code does.**
- Signals are collected **only for accounts already held**, on their refresh cycle
  (`nexus/ingestion/`).
- Fit and intent are separate numbers: composite = fit 0.4 + intent 0.4 + health 0.2
  (`agents/scoring.py:67`).
- No ICP field mentions a signal. Prospector's signal filters work only on the Saved tab, over
  workspace data (`prospector/fields.py:162-171`).
- The Orchestrator's `intent_signals` only add words to the web query (`company_search.py:235`).

**Root causes.**
1. **There is no criterion and no gate.** Signals rank accounts; they never qualify them.
2. **The taxonomy lacks the kinds the team named.**
   - Expansion, new location and new facility are needles inside the generic `news` kind at 0.6,
     lumped in with partnerships and launches (`ingestion/sources.py:44-49`).
   - Exec appointments sit under `hiring`.
   - "Adopting a technology" is only GitHub languages (`sources.py:1270-1290`).
   - Hiring keeps **department counts, not role titles** (`ingestion/hiring.py:85-99`), so "hiring
     for specific roles" cannot be asked.
3. **Discovery cannot use signals.** A company's signals are known only once it is held.

**Measured (local).** 4,867 signals:
- `news` 4,422 (91%), of which 3,877 are at weak strength 0.4;
- `job_posting` 133, `hiring` 132, `tech_install` 122, `funding` 58.

**Edge cases.**
- Attribution is this subsystem's recurring bug (CLAUDE.md lists five). A signal used as a filter
  multiplies a misattribution.
- A recency window is needed per criterion.
- Signal-led discovery changes search spend, so the owner rule applies: trade-off first, approval
  before any dork or source change (D5).

**Proposed fix (D5).**
- Optional ICP signal criteria, each with a window: hiring for roles matching keywords, funding,
  expansion (new office, location or facility), tech adoption. They work as filters on held
  accounts and as a rank boost on discovery.
- New kinds: `expansion` split out of `news`, and hiring that keeps role titles.
- Signal-led discovery is an optional later phase.

---

## 4. AI personalization for emails and call scripts

**What the code does.**
- **Campaign drafts** use `engagement/drafting/context.build_context` (`context.py:140-263`). It
  supplies the person's name, title and seniority, `account_facts` (`agents/copy.py:382-414`:
  industry, size, revenue, location, tech, description) and the **8 most recent** signals, ranked
  by strength.
- **Call scripts** (`agents/call_script.py:95-142`) open on the strongest signal, or on "your
  current priorities", then land a value prop and its pains. Both are workspace-global
  (`select_value_prop`).
- **The personalisation check** (`drafting/personalisation.py`) passes if any fact is used, and
  the contact's **title counts as a fact** (`context.py:213-215`).

**Root causes.**
1. **The model is given wrong facts.** "Industry: <stamp>" and "Location: <stamp>" come straight
   from 1a, and "Known tech: DMARC, Let's Encrypt" is infrastructure. The inaccuracy the team saw
   is the data, not the model.
2. **It rarely has a real trigger.** Of 405 accounts with signals, **232 (57%)** have only generic
   news among their 8 most recent, and **151 (37%)** only weak 0.4 mentions. With nothing specific,
   the prompt's strongest material is the value prop and the pain, which is why the output
   rephrases them.
3. **It rarely knows what the company does.** Only 212 of 564 accounts (38%) have a description.
4. **There is no campaign context.** The offer is one `product_context` and one set of
   `value_props` per workspace (`models/relevance.py:18-29`), so two campaigns cannot pitch
   differently (see point 5).
5. **The check is weak.** Naming the title satisfies "uses a fact", so a draft with no real
   observation passes.

The model is Groq `openai/gpt-oss-120b` with the offline stub as fallback (`llm_provider=auto`).
That is not the first-order cause.

**Edge cases.**
- The stub is what a deployment with a dead key sends (CLAUDE.md, Drafted copy). Grounding rules
  must hold for it too.
- A call script is spoken, so a wrong "fact" is worse on a call than in an email.

**Proposed fix (D8, D8b).**
- A **verified fact sheet** per draft: only facts with provenance (point 1c), the company's own
  description, the role, and strong in-window signals with links. Weak `news` is never included.
- The sub-campaign's offer.
- The draft must use a non-title fact when one exists. The facts used are shown beside the draft
  with their links.

---

## 5. Workspace and campaign setup (Client → Sub-campaign → ICP → selection → outreach)

**What exists.**
- **Isolation is per tenant.** RLS works at the tenant level, a user can hold several tenants, and
  the top bar switches between them (`routers/auth.py:530-601`).
- **Each new tenant starts on the Free plan**, with its own credits, mailboxes and members
  (`auth.py:565`).
- Inside a tenant there is a vestigial `Workspace` row. Nothing is scoped by it (only
  `identity.py`, `auth.py` and `workspace.py` reference `workspace_id`).
- **One ICP and one offer per tenant**: `relevance_profiles.tenant_id` is unique
  (`models/relevance.py:18-19`).
- A campaign (`models/engagement.py:127-150`) holds a name, a mailbox, a source list and steps
  with an `angle`. It has no ICP, no offer and no audience definition.

**Root cause.** The data model has no level between the tenant and the campaign. Running two clients
in one tenant mixes their accounts, ICP, offer, signals and do-not-contact. Running them as
separate tenants splits plans and credits, and there is no cross-client view.

**Edge cases.**
- The same company or person can belong to two clients (D9b).
- Do-not-contact scope: per client, or global?
- One mailbox serving two clients.
- Reporting per client.
- Moving existing data into a first "client".
- RLS must stay the boundary between clients, or isolation drops to application code.

**Proposed fix (D9).** Recommended: **each client is its own tenant**, which keeps RLS as the
boundary, under a new **agency parent** that shares one credit balance and gives one switcher and
one roll-up. Inside each client:
- Campaign → Sub-campaign, each with its own ICP (feeding point 2's gate), offer (feeding point 4),
  audience and sequence.
- The flow screens: Client → Campaign / Sub-campaign → ICP filters → account and contact
  selection → outreach setup.

---

## 6. Call script document upload

**What exists.** The script is always generated from scratch (`agents/call_script.py`). There is
no upload or storage path for one, and nothing reads a document. Dispositions are a fixed set of 8
in code (`models/calling.py:39-55`). Email reply categories are a fixed tuple of 9
(`engagement/replies/classify.py:24`). The team's recommended sets are in the files we have not
received.

**Proposed fix (D10).**
- Upload per sub-campaign: PDF, DOCX or TXT, converted to text, size-capped, and stored per
  tenant.
- The caller's script is shown **as written**. The AI adds a side panel of verified prospect and
  company context, signals and likely objections, and fills only marked placeholders
  (`{{company}}`, `{{trigger}}`).
- Dispositions and categories become per-workspace configurable on top of the built-in ones, once
  the team's lists arrive. The built-in ones stay, because requeue logic and the classifier depend
  on them.

---

## 7. Plan, in the team's priority order

| Phase | Contents | Depends on |
|---|---|---|
| 0 | Feedback files. Which environment and screens. Approved paid probe (D-measure) on a **DB copy** to size LinkedIn's self-labels, Find-contacts reasons and email verdicts for about 20 Oil & Gas companies | owner |
| 1 Data accuracy | Remove the stamp; refuse vendor domains; industry from evidence (D1); dry-run cleanup of stamped accounts and shared rows (D2); a LinkedIn read replaces derived text; provenance model and UI (D7); split person and email source; Find contacts returns reasons; level and title normalisation; InfoJoy contact step (D6) | D1, D2, D6, D7 |
| 2 Mandatory ICP | `mandatory` per criterion; one `icp_gate` across every path, including LinkedIn and database candidates; unknown handling (D3); HQ or any-office rule (D3b); tech chips, aliases and data source (D4) | phase 1, D3, D3b, D4 |
| 3 Signals in ICP | Signal criteria and windows; `expansion` kind; hiring keeps role titles; optional signal-led discovery (D5) | phase 2, D5 |
| 4 Personalization | Verified fact sheet; weak news out; stricter check; facts shown beside the draft; sub-campaign offer; model comparison (D8b) | phases 1 and 5, D8 |
| 5 Clients and sub-campaigns | Agency parent and shared credits; Campaign → Sub-campaign with ICP, offer and audience; the setup flow (D9, D9b) | D9 |
| 6 Scripts and dispositions | Upload and side panel (D10); configurable dispositions and categories from the team's files | phase 5, files |

Each phase follows the standing rules:
- its own branch, with a failing test first;
- the CI-equivalent run on the exact commit before any push;
- dry-run scripts for data changes, run on a DB copy before anything live;
- nothing deployed over `nexus-gtm` without saying so first.

## 8. Decisions for the owner

The questions go out in the session with trade-offs stated. Answers are recorded below as they
arrive.

| Id | Decision | Answer |
|---|---|---|
| Files | Where the team's feedback files are | — |
| Env | Which environment they tested on; read-only access to count stamped rows there | — |
| D1 | How a company's industry is decided | — |
| D2 | Cleanup of stamped data | — |
| D-measure | Paid probe on a DB copy | — |
| D3 | Unknown value on a mandatory field | — |
| D3b | Country rule: HQ or any office | — |
| D4 | Tech-stack data source | — |
| D6 | InfoJoy in Find contacts | — |
| D7 | Provenance: per field or per record | — |
| D5 | Signals in the ICP | — |
| D8 | What drafts may be grounded on | — |
| D8b | Drafting model | — |
| D9 | Several clients in one account | — |
| D9b | Same company or person under two clients | — |
| D10 | How an uploaded script is used | — |

---

## Appendix: how it was measured (read-only)

```sql
-- stamped label: industry and country equal the ICP's first entries
select t.name, a.source, a.industry, a.country, count(*)
from accounts a join tenants t on t.id = a.tenant_id
group by 1,2,3,4 order by 5 desc;

-- shared rows carrying a stamp, never read by LinkedIn
select count(*) from companies c
where c.source = 'account_backfill' and c.linkedin_fetched_at is null
  and c.industry in ('Manufacturing','Software & SaaS') and c.country = 'United States';

-- a search vendor's domain filed as an account
select t.name, a.name, a.domain from accounts a join tenants t on t.id = a.tenant_id
where a.domain = 'exa.ai';

-- verified LinkedIn page coverage by source
select a.source, count(*), count(c.linkedin_url) from accounts a
left join companies c on c.id = a.company_id where a.archived_at is null group by 1;

-- signal mix, and accounts whose 8 most recent signals are only generic news
select kind, count(*) from signal_events group by 1;
```

The score distribution comes from `RelevanceEngine.score_icp_fit` run over a JSON export of
`relevance_profiles` and live `accounts` for Nexus Platform, Infojoy, SDR Demo and Plan Check, in
`nexus-gtm:latest` with `--network none` and the worktree mounted read-only.
