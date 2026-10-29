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

**This is the plan as proposed, before the decisions.** The owner's answers changed it: there are no
sub-campaigns and no agency parent, discovery is contact-first, and drafting gets a Superadmin model
control. The plan that stands is "The plan after the decisions" in section 8.

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

Asked and answered 2026-10-26. The owner's words are quoted where they added to an option.

| Id | Decision | Answer |
|---|---|---|
| Files | The team's feedback files | The **Dispositions and Categories** table was given in the session (below), with "best UX with easy drop downs for various categories". The detailed-feedback file has not been seen. |
| Env | Where the team tested | **Production.** Not touched; the local stack stands in for measurement. |
| D1 | How a company's industry is decided | **Evidence + website**: "ensure they are highly accurate and the source is traceable". |
| D2 | Cleanup of stamped data | **Fix forward only.** Existing accounts keep their values. |
| D2b | Stamped shared rows (202 locally) | **Quarantine**: not offered for new deliveries until a real source re-labels them. |
| D-measure | Paid probe on a DB copy | **Approved** (LinkedIn page, about 20 Find-contacts runs, Exa, Reacher, InfoJoy records). |
| D3 | Mandatory fields | "Location, tech stack, industry, size, titles etc. can be marked Mandatory **so only exact matches**." No data means no match. |
| D3a | Meaning of "exact" | **Same meaning**: LinkedIn sub-industries count; titles match after abbreviations and word order, not loose keyword overlap; size inside the band; tech by product alias. |
| D3b | Country rule | **Headquarters.** |
| D3c | New: search for companies **that have contacts** | **Pool, then InfoJoy**, "but add others as well for fallback; we also don't want to waste companies research; best effective ways". Cost-effective, accurate and precise, with duplicate controls. A company researched with no buyer found yet is kept for a later retry, not re-bought. |
| D4 | Tech-stack evidence | **InfoJoy + website**, product names normalised, each with source and date. |
| D5 | Signals in the ICP | **Filter + new kinds + discover by signal.** Also a **signals filter on Accounts**, and the **Signals page opens the account page**. "Ask details wherever needed": the exact discovery queries and their cost go to the owner before they are built. |
| D7 | Provenance | **Per field** (source, link, date), following from D1's "traceable". |
| D8 | Drafts grounded on | **Verified facts only, minus wrong ones, plus previous emails, the prospect's LinkedIn profile, posts and signals.** Campaign drafts do not use the person brief today; only the composer and call scripts do. |
| D8b | Drafting model | **A Superadmin control** choosing the model for emails and for call scripts: **Claude by default and first**, then the next best provider available or added. |
| D9 | Several clients | **Client = workspace, no structural change.** Make sure SDRs can create separate workspaces. |
| D9a | Sub-campaigns with their own ICP and offer | **No: one ICP and one offer per workspace.** |
| D9b | Same company or person under two clients | **Allowed, never mixed.** |
| D9c | Plan of a new client workspace | **As today**: each new workspace is billed to the owner who creates it. |
| D10 | Uploaded call scripts | **Shown verbatim, with an AI side panel**, "and also an option to fine-tune or enhance the uploaded script". |
| D12 | What counts as an industry (from the probe) | **Activity decides.** What the company does decides its industry, for every industry: producers, midstream, refining and oilfield services are Oil & Gas; equipment and machinery makers are Manufacturing even when they call themselves Oil & Gas. An ICP can tick "include suppliers to this industry" to add them back. A self-label (LinkedIn, Exa, InfoJoy) is evidence, not the verdict. |
| D11 | Disposition actions | **Record + safe actions**: DNC/Opt-out → do-not-contact; Bad Number and Number Disconnected → phone bad; Bounce → email bad; Callback and Follow-Up Required → dated task; Outside ICP, Existing Customer and Duplicate Lead → account flagged, nothing deleted; Wrong Person and Reference Shared → ask for the right contact; the rest record only. |

**The team's Dispositions and Categories** (one list for Email and Calling reps; channel-only values,
such as Bounce and Busy Signal, appear only on their channel):

| Category | Dispositions |
|---|---|
| Positive Outcome | LEAD; Interested - Send Information (MQL); Reference Shared |
| Follow-Up | Callback; Follow-Up Required |
| Call Attempt | No Answer; Voicemail; Busy Signal; Opening Hang-Up |
| Contacted - Negative Outcome | Not Interested; Hung Up - No Interest; Not Interested - Reason Shared; No Budget / No Plans |
| Contact Issues | Wrong Person; Bad Number; Number Disconnected |
| Company Qualification | Existing Customer; Decision at Another Location; Using Competitor - Not Switching; In-House Solution; Outside ICP; Duplicate Lead; Already Engaged with Colleague; Supplier Registration Required |
| Compliance | DNC / Opt Out / Unsubscribe |
| Gatekeeper | Gatekeeper |
| Other | Other |
| Bounce | Bounce |

### The plan after the decisions

| Phase | Contents |
|---|---|
| 0 | Paid probe on a DB copy (approved). Ask for the detailed-feedback file. |
| 1 Data accuracy | Remove the stamp. Refuse search-vendor domains. Quarantine stamped shared rows for new deliveries. A LinkedIn read replaces derived text. Industry from evidence (LinkedIn code, InfoJoy IndustryType/SIC, website classification), each with source, link, date and confidence; the website classifier is enabled only after a precision bar is met on a hand-checked sample. Per-field provenance for accounts and contacts, with the person's source kept apart from the email's, shown in the UI. Find contacts returns its reasons; job levels stop being sent as titles; titles are normalised; the contact chain becomes pool → InfoJoy → LinkedIn → web. **Contact-first discovery**: a company is a match only with a matching buyer. Researched companies with no buyer yet are kept for retry. Dedupe by domain and by person. |
| 2 Mandatory ICP | `mandatory` on Industry, Size, HQ Country/Region, Tech and Titles. Exact means same meaning; no data means no match. One gate on every path, including LinkedIn and database candidates. Tech from InfoJoy and the website, with aliases and chip input. Fit % shown only for accounts that pass. |
| 3 Signals | ICP signal criteria with windows. New kinds (expansion, new facility, new location; hiring with role titles). A signals filter on Accounts. Signals page → account page. Signal-led discovery once the owner approves the exact queries and cost. |
| 4 Personalization | Verified facts only, plus previous emails, LinkedIn profile, posts and strong signals, in **campaign drafts as well**. Weak news out. A title alone no longer counts as a fact. Facts used are shown beside each draft. A Superadmin model control per purpose (email, call script): Claude first, then fallbacks. |
| 5 Workspaces | Verify end to end that an SDR can create a separate workspace and switch between them. Billing stays per workspace, to the owner. No sub-campaigns. |
| 6 Scripts and dispositions | Upload a script (PDF, DOCX or TXT) per campaign. Shown verbatim with an AI side panel, plus "Enhance" (a suggested revision, the original kept, the SDR accepts or rejects). Two-level Category → Disposition dropdowns with the team's 29 values; the safe actions above; the AI reply classifier mapped into the same categories. |

---

## 9. Probe results (2026-10-26, approved, run on the DB copy `nexus_sdrprobe`)

Run on the production image (`docker compose run --rm --no-deps --entrypoint python worker`) with
the database URL pointed at the copy, the in-memory queue and billing enforcement off. The live
`nexus` database, the queue and the running app and worker were not touched. Script and outputs:
`probe.py` and `out/*.json` in this session's scratchpad. Spend: a few Exa searches (about $0.20).
Apify and InfoJoy spent nothing because both refused, as below.

**Both primary data sources are down on this stack.**
- **Apify (LinkedIn):** key 0 answers 403 *"Monthly usage hard limit exceeded"* and key 1 answers
  401 *"User was not found or authentication token is not valid"*. Every LinkedIn step fails:
  company search, the page lookup and the employee search.
- **InfoJoy:** every search answers **HTTP 500**, including a one-company lookup by website and a
  titles-only contacts search (18-49 s cold, then about 1 s). The provider is failing as a whole,
  not on particular filters.
- **Consequence, if production shares this Apify account and this InfoJoy service:** every ICP
  account there comes from the web step that stamps labels (1a), Find contacts runs on web search
  alone (1b), and Prospector fails. That alone explains much of what the team saw. **The owner
  needs to check production's Apify usage and keys, and InfoJoy's service.**

**A. The SDR complaint, reproduced.** The web company step with US, 51-5000 employees:
- **ICP "Oil & Gas" alone.** The query is "Oil & Gas companies in United States". 23 hits, all
  genuine producers (SM Energy, Mewbourne, Jonah Energy, Petro-Hunt, ...), all labelled
  "Oil & Gas / United States". The stamp happens to be right here.
- **ICP "Oil & Gas, Manufacturing".** The query is "Oil & Gas Manufacturing companies in United
  States". 24 hits, mostly **oilfield equipment manufacturers** (Process Manufacturing, Lincoln
  Manufacturing, JMI Manufacturing, Machine Specialty & Manufacturing, Fabritech, TSI Flow
  Products, ...), **every one labelled "Oil & Gas"**. This is exactly "pulled Manufacturing
  companies and labelled them Oil & Gas". The ICP's industries are joined into one keyword query
  (`company_search.icp_to_query`), and the first one is stamped on every result.
- **ICP of four industries** (Oil & Gas, Manufacturing, Energy, Industrial Machinery). Exa's own
  profile text calls 5 of 24 "Machinery Manufacturing" or "Industrial Machinery Manufacturing"
  (LeTourneau, Fisher Energy, United Engines, Reel Power, Entro), and they are still labelled
  "Oil & Gas".
- **Identity errors:** Dyna-Drill Technologies is filed under `slb.com`, its parent, and
  GEODynamics under `perf.com`.
- **Evidence we already receive and throw away:** Exa's company results state an industry
  ("X is a Oil and Gas company") in the snippet. That is a free, traceable source for D1.
- **Definition matters:** most equipment makers *call themselves* Oil and Gas (in Exa and on
  LinkedIn). Whether a supplier to the industry counts as "Oil & Gas" is a business definition,
  and the classifier must follow the owner's answer (D12).

**C. Find contacts on 20 of those genuine producers.** ICP titles: Operations, Procurement, Supply
Chain, HSE, Maintenance, Plant Manager, VP Operations, Director of Operations.
- **LinkedIn `failed` on all 20** (Apify), so every run took the web fallback.
- **4 of 20 accounts (20%) got nobody**, which the page reports only as "No new contacts".
- **37 people found; 21 have an email** (17 valid, 4 catch-all). **16 (43%) have none**:
  - 6 had all 10 patterns proven invalid, which points to a different mail domain or format;
  - 10 got no conclusive verdict, so policy kept no address.
- **Titles drift from the ICP:** "Lead Operator" (twice), "Land Manager", "Manager", "President",
  "Inventory Manager", "Senior Exploitation Manager". Loose token overlap ("manager",
  "president") lets them through. D3a's same-meaning rule fixes this.
- **The person's source is overwritten, confirmed:** all 21 contacts with an email read
  `pattern_verified`, and only the 16 without one still read `sourcing:search` (1c).
- Median 23 s per account, 63 s at most.

**D. InfoJoy.** It could not be measured (HTTP 500 throughout), so InfoJoy's industry and SIC
precision and its contact coverage remain unmeasured. Re-run `PROBE_PARTS=bd` once Apify and
InfoJoy answer.

The copy `nexus_sdrprobe` is left on `nexus-gtm-postgres-1` for re-runs. It holds the 20 probe
accounts in "SDR Demo" with an Oil & Gas ICP. Drop it when no longer needed.

---

## 10. Signal changes awaiting approval (2026-10-27)

Built without approval because they change no collection: each signal names and links its account,
and the Accounts list filters on "has a recent signal" or one kind within a window (`845e491`).
What follows changes what we collect or pay for, so it waits for the owner.

**S1. Expansion as its own signal (new facility, plant, office or location).**
- *Today*: "expands", "expansion" and "opens new" make a generic `news` signal at 0.6, and the
  `expansion` dork is **never run**: `select_dorks` takes the first `signal_dork_max_queries` (4)
  of 9 in declaration order, and it is eighth. For an account with a domain the four that run are
  `funding_press`, `funding_wire`, `hiring_ats` and `hiring_careers`; `hiring_linkedin`,
  `exec_change`, `tech_adoption`, `expansion` and `newsroom` never do.
- *Proposal*: subtype `expansion` on `news` (no new kind, so intent weights, plays and alert rules
  keep working), with the needles "opens new facility", "new plant", "new manufacturing facility",
  "new distribution center", "new headquarters", "opens office", "new location", "breaks ground",
  "expands into"; and run the `expansion` dork.
- *Cost, two ways to run it*:
  - **(a) Fifth query** (`signal_dork_max_queries` 5, `expansion` moved to fifth): one more search
    per hot account per 6 h (a `news` dork, so the fast cache TTL), about $0.026 a day, $0.77 a
    month per hot account at Firecrawl's $0.0064. Nothing else changes.
  - **(b) Swap** `expansion` in for `hiring_careers`: no extra spend; hiring then rests on the ATS
    board dork and the keyless ATS source, which already reads every open role.
  Classification itself is free (headlines already fetched).
- *Recall trade-off*: "breaks ground" and "new location" also match a company's customer projects
  and store openings; the title-only classification and the name-in-title rule limit that, but the
  first week should be read by hand.

**S2. Hiring for the ICP's buyer roles.** The ATS source already reads every open role (keyless,
free). Proposal: subtype `buyer_role` on `hiring` when an open role's title passes `title_fits`
for the ICP's buyer titles, carrying the matched titles. Cost $0; recall is the ATS coverage.

**S3. Signal criteria in the ICP.** A "Recent signal" must-match field (kinds plus a window). It can
only judge companies we hold, because signals are collected for held accounts alone; applied to
the Accounts list's must-match filter and to campaign enrolment. At discovery it needs S4.

**S4. Discover by signal.** One search per ICP industry per day by the daily sweep, results through
the same gates as every web candidate. Exact queries, for an ICP of "Oil & Gas":
`"Oil & Gas" ("raises" OR "funding round" OR "secures $") {year}` (funding) and
`"Oil & Gas" ("opens new facility" OR "new plant" OR "expands into") {year}` (expansion).
Cost: 2 searches per industry per day, about $0.39 a month per industry per workspace, plus
enrichment of what they find (the sweep's existing cap). Risk: news search returns articles, so
each company still needs its own domain from the article, by the existing web-candidate rules.

---

## 11. What is built on `fix/sdr-data-accuracy` (2026-10-27), and what waits

Twenty commits on `de3d2fd`, nothing pushed, merged, deployed or applied to the live database.
Migrations `0073`-`0075` are additive and replay from empty (`tests/test_migrations_replay.py`).

| Point | Built | Waits on |
|---|---|---|
| 1 Data accuracy | No stamped labels on web finds; vendor domains refused; stamped shared rows quarantined; LinkedIn read replaces derived labels; 60 industry terms mapped and stale mappings re-mapped; per-field provenance on accounts and contacts, shown in the UI; Find contacts says why it found nobody; real titles to LinkedIn | Industry-from-website reader is built and **off**: its measurement could not finish (Groq rate-limited every run). Needs an Anthropic key, then a re-run against the 95% / 80% bar before `industry_evidence_mode` goes on |
| 2 Mandatory ICP | Must-match Industry, Size, Country, State, Tech, Titles; same-meaning matching; no data is no match; every discovery source, the Accounts list and Account 360 ("Fails Industry"), a must-match filter, campaign enrolment; tech groups in brackets; include suppliers; any/all tech | |
| 1/2 Contacts | Find contacts: pool, then InfoJoy, then LinkedIn, then web, each row proven; contact-first discovery (`require_buyer`, off by default) | InfoJoy answers 500 and both Apify keys are refused locally: replace keys, check InfoJoy |
| 3 Signals | Each signal names and links its account; Accounts filters by recent signal kind and window | **Section 10 (S1-S4) awaits approval** |
| 4 Personalization | Events only, no plumbing tech, no stamped labels, title a last-resort fact; facts shown under each draft on Review; Superadmin chooses provider and model for emails and call scripts, Claude first; Anthropic reads Provider keys | `personalization_before_campaign_draft` is **off**: one paid actor run per person |
| 5 Workspaces | Verified end to end on the preview: create, list, switch both ways, each on its own Free plan. No change needed | |
| 6 Scripts, dispositions | The team's 10 categories / 28 dispositions, Category then Disposition, safe actions; reply desk in the same list; campaign script upload (.docx, .txt) shown as written with placeholders, plus Enhance | PDF needs a PDF reader added (`pypdf`, pure Python) |

---

## 12. New-account accuracy, measured on the DB copy (2026-10-27/28)

Workspace `sdrdemo1096` (Oil & Gas, United States, 51-5,000 staff) on `nexus_sdrprobe`, with
Industry, Company size and Country set as must-match fields through `PUT /api/relevance/profile`,
then `POST /api/discovery/populate` for 10 and the job run as the worker runs it. The workspace was
moved from Free to Launch **on the copy only**: Free has no `module.discovery`, so with the local
deploy's `NEXUS_BILLING_ENFORCEMENT=on` the endpoint answered 402 `dependency`, which is correct.

| Run | Code | Delivered | Right company, own domain, inside the ICP |
|---|---|---|---|
| 1 | branch as it was | 6 of 10 | 2 (Plains, Murphy Oil); 4 acquired companies under the buyer's domain |
| 2 | + chain skips must-match failures | 10 of 10 | 6; 2 under another's domain (Intermoor, Pioneer), S&P Global Energy, API |
| 3 | + domain guard, associations, cleanup | 10 of 10 | 10; 3 of them suppliers (training, equipment) |

What was wrong, and what was done (owner decisions 2026-10-28):
- **The request ended short** while the page already bought held 25 that passed: the chain counted
  rows delivery then refused. Fixed in `companies._fails_mandatory` (commit 69d3c77).
- **An acquired or subsidiary company's LinkedIn page lists its owner's website**, and the shared
  store kept it for every workspace (chevron.com as "Hess Corporation"). Across the copy's 261
  LinkedIn-read companies the same check found link shorteners and pages that are not companies.
  Fixed by `prospecting/vetting.py` (name check, homepage check for the 6% that need it) and
  `scripts/vet_linkedin_companies.py` for rows stored before (15 of 261 held back on the copy).
- **Associations and institutes** (API, SPE) are left out of discovery.
- Existing accounts honour the gate: 0 of 36 pass; 16 fail on industry, and 20 real oil and gas
  companies stored with no industry or size read "No data for Industry, Company size" (D3). They
  pass once enriched. Campaign enrolment skipped both kinds as `outside_icp` (checked in a
  rolled-back transaction).

**Find contacts, old chain against new (2026-10-28).** Two byte-identical clones of the copy
(1,000 test credits granted first), the same 8 correct accounts with no contacts, the screen's own
request (`POST /api/accounts/{id}/source-contacts?limit=5`): master `de3d2fd` on one clone, this
branch on the other.

| | Old (pool, LinkedIn, web) | New (pool, InfoJoy, LinkedIn, web) |
|---|---|---|
| Contacts | 34 | 34 |
| At the right company (the proven page's own employees) | 34 | 34 |
| Email on the company's own domain, kept by the verifier | 23 (18 valid, 5 catch-all) | 23 (18 valid, 5 catch-all) |
| Title passes `title_fits` | 34 | 34 |
| Title is the buyer persona, judged by hand | 26 | 20 |

- **No more contacts, because InfoJoy answered HTTP 500 on all 8 calls**, so the new chain fell
  through to LinkedIn exactly as the old one did. Probed the same day with the product's own client:
  500 for one job title, for one domain, for the Find contacts request, and for a companies search.
  The provider is failing, not our request. The extra contacts InfoJoy was chosen for are unmeasured
  until it answers.
- **About one title in three is a loose match in both chains**: "Operations Accounting Manager",
  "Datacenter Operations Manager", "Manager - Total Rewards & HR Operations", "Maintenance
  Technician". `title_fits` accepts a title holding the words of a wanted one, and "Operations
  Manager" is inside "Operations Accounting Manager"; marking Job titles must-match does not tighten
  that (`same_meaning_title` asks for the same words). The difference between the two runs (26 vs
  20) is which people the LinkedIn actor returned, not the code.
- DuckDuckGo HTML answered 403 to the web fallback in both runs.

Still open, not built:
- Suppliers labelled with their customers' industry (PetroSkills, SPM, Oil States, S&P Global
  Energy) wait on `industry_evidence_mode`, i.e. an Anthropic key and the re-measurement.
- The Apify B2B enrichment actor `teodor_banea/b2b-lead-enrichment-free` answers 404: the actor
  is gone, so that enrichment step finds nothing.
- Web enrichment stored LinkedIn's band "1001-5000" as exactly 5,000 staff (S&P Global Energy),
  which passes a 5,000 cap by construction.
- A vanity domain that redirects to the company's real one (figma.bot, world-kinect.com) is kept
  under the vanity domain: canonicalising would need a homepage fetch for every row.
- One shared row is named the string "None" (normansmilecenter.com).

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
