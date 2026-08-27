# NEXUS GTM — Pricing, Packaging and Unit Economics

**Status:** current as of 2026-08-25. Every figure marked *measured* comes from the production
database and is reproducible from the queries in Appendix A. Every figure marked *assumed* is a
judgement call, and the reasoning is given so it can be argued with.

**Who this is for:** the finance and investor conversation. It answers four questions in order —
what we charge, why that number, how the money is actually collected, and what happens when a
customer does not fit the ladder.

---

## 1. Executive summary

We sell a **credit-metered SaaS subscription**. A plan buys a monthly allowance of credits; every
billable action in the product costs a fixed number of credits; overage is charged or blocked
according to the plan.

Four numbers matter:

| | Value | Basis |
|---|---|---|
| Blended cost of one credit | **$0.00188** | *Measured* — real COGS at the observed usage mix |
| Price of one credit sold | **$0.0248 – $0.0587** | *Measured* — varies by tier, on purpose |
| Gross margin at observed mix | **92.4% – 96.8%** | *Measured* |
| Gross margin at worst-case mix | **83.9% – 93.2%** | *Measured* — if every credit went to our most expensive capability |

The margin floor is **enforced in code, not in a policy document**: `rates.validate_rate()` refuses
to store any price below 50% gross margin unless finance records an explicit, audited exception, and
it runs on the seed itself, so a bad price cannot reach the database even by deploy.

**The honest caveat, stated up front.** These margins are *gross margin on cost of goods sold* —
the third-party API spend attributable to a unit of usage. They do not include infrastructure,
salaries, or support. They are the right number for "does the metering model work"; they are not
company-level margin, and this document does not claim they are.

**The second honest caveat.** The observed usage mix is derived from **18 usage events across 4
workspaces**. That is enough to see the *shape* of demand and to sanity-check a price. It is not
enough to forecast. Section 12 says what would change our minds.

---

## 2. Why credits, and not seats or flat rate

Three models were available. We chose credits because our costs are variable and per-action, and the
other two break when costs are variable.

**Per-seat** is the SaaS default and it is wrong for this product. Our marginal cost is driven by
what a user *does*, not by the existence of the user. A rep who runs 400 enrichments a month costs
us roughly 40× a rep who reads the dashboard. Under per-seat pricing those two pay the same, which
means the light user subsidises the heavy one and the heavy user is unprofitable at the margin.
Worse, the customer's incentive is to buy fewer seats and share logins — which suppresses exactly
the adoption metric we would be selling on.

**Flat rate** has the same defect, unbounded. One customer running bulk enrichment can consume more
COGS in a week than their annual contract value.

**Credits** align price with cost because they *are* cost, marked up. Every capability has a rate
card (`credits_per_unit`) and a cost rate (`unit_cost_usd`), and the ratio between them is the
margin on that action. The customer sees one simple currency; we see a per-action P&L.

We keep a **seat component** as a secondary lever (see §4), because seats are how customers reason
about budget and because some cost genuinely does scale with users — support, onboarding, and the
per-user data we store.

### What we did not do, and why

We do not price per API call at the HTTP level. A credit is attached to a **capability** — a
business action like "draft an email" or "verify an address" — not to an endpoint. Endpoints change
when we refactor; capabilities do not. Pricing against endpoints would mean a routing change could
silently reprice a customer.

---

## 3. Unit economics (measured)

70 billable capabilities are defined. **All 55 priceable ones now carry a rate card and a cost
rate**; the other 15 are module gates (on/off, no per-unit cost) or are exempt by name with a stated
reason — chiefly `seat.member`, which is billed as a seat price and would otherwise be charged twice.

That was not true a week ago, and the gap is worth recording because it is the kind that hides.

> **33 capabilities had no rate card.** A capability with no rate card is metered and then *rated at
> nothing*: usage events accumulate, quotas count down, and no revenue line is ever produced. It
> looks handled. The largest by far was **`ai.scoring` — 4,090 runs, 98% of all agent activity,
> metered correctly at the call site, and free.** `ai.tokens` was in the same state: the hook for
> token-metered billing existed and had never been priced.
>
> A test now refuses any capability that is neither priced nor named as exempt, so this cannot
> recur by omission.

**Cost per credit, by capability** — the spread is deliberate. A credit is a unit of *price*, not a
unit of cost; capabilities that cost us more consume more credits.

| Cost per credit | Capabilities | Examples |
|---|---|---|
| $0.0040 (most expensive) | 5 | `ai.research_brief`, `discovery.lookalike_company`, `search.web`, `platform.storage` |
| $0.0030 – $0.0038 | 6 | `enrich.phone`, `enrich.contact`, `enrich.account`, `calling.minutes` |
| $0.0010 – $0.0024 | 9 | `ai.account_qa`, `ai.chat_turn`, `workflow.orchestration_run` |
| $0.0001 – $0.0009 | 17 | `outreach.email_send`, `verify.email`, `integration.crm_sync` |

- **Catalog mean:** $0.00161 per credit
- **Median:** $0.00100 per credit
- **Worst case:** $0.00400 per credit
- **Blended at observed usage mix: $0.00188 per credit** ← the number to use

The blended figure is higher than the mean because real demand skews toward the expensive end.
That is the single most important empirical finding in this document, and it is why we price off
the blended figure rather than the mean.

### What customers actually spend credits on (measured)

| Capability | Share of all credits burned |
|---|---|
| `enrich.contact` | 40.1% |
| `verify.email` | 34.8% |
| `ai.email_draft` | 14.2% |
| `ai.research_brief` | 4.8% |
| `ai.account_qa` | 4.7% |
| everything else | 1.4% |

**Three-quarters of all credit consumption is enrichment and verification.** Not AI generation,
which is what the product leads with in marketing. This has two consequences:

1. **Pricing must be anchored to data cost, not token cost.** LLM inference is cheap and falling;
   contact data is neither. A pricing model built around AI cost would be built around 14% of the
   bill.
2. **Our COGS reduction roadmap should target enrichment**, and it does — the shared company and
   people stores (`nexus/companies/`, `nexus/people/`) exist so forty workspaces tracking the same
   company pay for one crawl. A recorded `not_found` is never re-purchased. Both directly attack the
   40% line.

Note that **a cache hit is still metered**. The customer received an answer and is billed for the
answer; what the shared store improves is COGS, not price. Billing only on a miss would hand the
saving to whichever customer happened to ask second, and make revenue depend on crawl ordering.

### How AI is priced, and why not by the token

Per-action, at a flat rate, with two token-metered escape hatches.

The case for flat pricing is a measurement. Across **4,174 real agent runs**, token consumption
within a single agent spreads widely at the extremes but tightly around the middle:

| Agent | Runs | Median tokens | Max | Median → max |
|---|---|---|---|---|
| `scoring` | 4,090 | 226 | 794 | 3.5× |
| `research` | 21 | 765 | 1,425 | 1.9× |
| `messaging` | 21 | 275 | 647 | 2.4× |
| `contact_rec` | 12 | 413 | 1,694 | 4.1× |
| `qa` | 5 | 321 | 951 | 3.0× |

**The worst case is about 4× the median, and a 4× token cost is absorbed at these margins.** Against
that, a flat rate buys the customer a bill they can predict — which is worth more to them than the
few percent of precision token-metering would add, and worth more to us than the support load of
explaining a variable line item.

Two capabilities exist for where that stops holding:

- **`ai.tokens`** (0.01 credits per 1,000 tokens) — for work a customer can make arbitrarily large:
  long-context analysis, document ingestion. The flat-rate argument depends on a bounded
  distribution, and these have none.
- **`ai.premium_model`** (4 credits per call) — a frontier model costs an order of magnitude more
  than the default. Charging the same for both means the customers who ask for the better model are
  subsidised by the ones who do not.

---

## 4. The plan ladder and why each price

> **Sections 5–7 below describe the eight-tier ladder as it was designed.** That ladder was
> collapsed to Free / Launch / Accelerate on 2026-08-26 and resized on 2026-08-27; the retired tiers
> are kept here because the *reasoning* is what an investor is buying, and it carried over intact.
> **For current prices use the table immediately below, or `docs/RATE-CARD.md`.**

### The current ladder

| Plan | Price | Credits | $/credit sold | COGS at observed mix | Gross margin | Break-even burn |
|---|---|---|---|---|---|---|
| **Free** | **$0/mo** | **200** | — | $0.38 | — | n/a |
| **Launch** | **$99/mo** | **2,000** | $0.0495 | $3.76 | **96.2%** | 52,660 cr |
| **Launch (annual)** | **$950/yr** | **24,000** | $0.0396 | $45.12 | **95.3%** | 505,319 cr |
| **Accelerate** | **$199/mo** | **8,000** | $0.0249 | $15.04 | **92.4%** | 105,851 cr |
| **Accelerate (annual)** | **$1,910/yr** | **96,000** | $0.0199 | $180.48 | **90.6%** | 1,015,957 cr |
| Overage / no commitment | metered | — | $0.0500 | — | — | n/a |

Above the ladder sits a **volume rate card at 25K–100K credits/month** ($0.0170 down to $0.0125 per
credit) and **custom enterprise plans** built per tenant in the Control plane. Both are in
`docs/RATE-CARD.md`, with the discount floors.

### The superseded ladder, for reference

| Plan | Price | Credits | $/credit sold | COGS at observed mix | Gross margin | Break-even burn |
|---|---|---|---|---|---|---|
| **Pay as you go** | **$0/mo** | **0** | metered | — | — | n/a |
| **Credit Pack (annual)** | **$999/yr** | **25,000** | $0.0400 | $47.01 | **95.3%** | 531,244 cr |
| Core | $19/mo | 400 | $0.0475 | $0.75 | **96.0%** | 10,104 cr |
| Starter | $44/mo | 1,000 | $0.0440 | $1.88 | **95.7%** | 23,398 cr |
| Growth | $79/mo | 2,000 | $0.0395 | $3.76 | **95.2%** | 42,010 cr |
| Professional | $129/mo | 4,000 | $0.0323 | $7.52 | **94.2%** | 68,599 cr |
| **Scale** | **$149/mo** | **5,000** | $0.0298 | $9.40 | **93.7%** | 79,235 cr |
| Business | $199/mo | 8,000 | $0.0249 | $15.04 | **92.4%** | 105,823 cr |
| **Scale Annual** | **$1,490/yr** | **60,000** | $0.0248 | $112.83 | **92.4%** | 792,346 cr |

"Break-even burn" is how many credits a customer would have to consume in a period before the plan
stops making money at the blended cost. Every tier has **an order of magnitude of headroom** over
its included allowance. That is the answer to "what if a customer abuses it": they cannot, at these
prices, without consuming 15–20× their allowance, and the entitlement engine blocks or charges
overage long before that.

### The volume curve is deliberate

Credits per dollar rise monotonically across every monthly tier:
21.1 → 22.7 → 25.3 → 31.0 → **33.6** → 40.2, with annual at **40.3**.

**Starter was the exception and has been fixed.** It shipped at 17.0 credits per dollar — *below*
Core's 21.1 — which meant a Core customer was better off buying overage than upgrading. That is an
inversion that punishes exactly the behaviour a ladder exists to reward, and it was found by drawing
the curve rather than by reading the prices. The allowance rose from 750 to 1,000 credits at the
same $44, giving 22.7. Raising the allowance rather than cutting the price keeps revenue intact on
new business, and there were no existing subscribers to regrandfather.

A rising curve is standard volume discounting and it does three things: it makes upgrading rational
for the customer, it keeps our margin roughly flat in absolute dollars per account while growing
revenue, and it means the customers who cost us most are on the tiers with the most headroom.

### Seats

Seat price runs alongside the base price and is set per tier:

| Core | Starter | Growth | Professional | **Scale** | Business | **Scale Annual** |
|---|---|---|---|---|---|---|
| $19 | $39 | $79 | $129 | **$129** | $199 | **$1,290/yr** |

**Scale carries Professional's seat price while including 25% more credits.** That is intentional
and is the tier's whole proposition: a team that has outgrown Professional's allowance but not its
headcount pays for the credits, not for the seats. It also means Scale does not undercut Business,
which is priced for teams that have outgrown both.

Seats are a **gauge, not a counter** — `seat.member` resolves to live membership count rather than
summing events, because summing would only ever climb and a customer could never get back under a
limit after removing someone.

---

## 5. Pay as you go

Two entry points that carry no monthly commitment, for the two customers a ladder always misses:
the one evaluating us who will not sign up to a subscription, and the one whose usage is lumpy
enough that any allowance is either wasted or exceeded.

| | Price | Allowance | Billing |
|---|---|---|---|
| **Pay as you go** | $0/mo | none | Every action metered, invoiced monthly in arrears |
| **Credit Pack** | $999/yr | 25,000 credits | Prepaid, valid twelve months |

**Pay as you go is the true metered plan.** No base fee, no allowance, 55 capabilities each rated
individually onto a monthly invoice. It is the lowest-friction way to start and the natural landing
place for a self-serve trial that ran out.

**The Credit Pack is the commitment-free annual.** 25,000 credits at $999 is 25.0 credits per dollar
— deliberately *between* Growth (25.3) and the monthly tiers rather than at the top. A customer who
prepays but commits to no monthly floor gets a modest volume discount, not the best rate on the
sheet; Scale Annual, which does commit, gets 40.3.

> **The implementation detail that makes this work, and nearly broke it.** Overage is only charged
> where a quota is *set*. A capability with `quota = None` reads as unlimited and is skipped
> entirely. So a pay-as-you-go plan built the obvious way — clone a plan, set the allowance to zero
> — inherits unlimited entitlements and **bills the customer nothing**, while metering happily. It
> would look correct until the first invoice came out at zero.
>
> Two further traps were found by running it. Cloning the base plan's entitlements covered five
> capabilities out of fifty-five, because base plans carry few explicit rows and the rest fall back
> to catalog defaults — so the first build billed for five things and gave away sixty. And zeroing
> every quota zeroed `seat.member`, which is billed as a seat price rather than in credits, meaning
> the plan allowed **no members at all**.

### Where PAYG sits against the ladder

At $0.04–0.0475 per credit, pay-as-you-go is priced at roughly the Core rate — the *worst* rate on
the sheet, which is correct. Committing to a plan should always beat not committing, or the ladder
has no pull. The customer trades price for flexibility, knowingly.

---

## 6. What we pay, and the ceiling it sets

### Provider list prices, August 2026

Checked against the providers we are actually configured to call, not a generic blend.

| Provider | What we use it for | List price |
|---|---|---|
| **Exa** | `search_provider` — all general search, lookalikes, ICP discovery | **$7 / 1,000** standard search ($0.007). Agentic $12/1k, Deep $15/1k. $10/mo free credit |
| **Firecrawl** | `signal_search_provider` — signal collection, page crawls | **1 credit per page**; search ≈ 2 credits per 10 results. Hobby $16/mo → 5,000 credits ($0.0032/credit); Standard $83/mo → 100,000 ($0.00083) |
| **Apify** | Phone lookup, LinkedIn personalisation | **$0.20 / compute unit** on Starter ($29/mo prepaid), $0.16 on Scale, $0.13 on Business. 1 CU = 1 GB-hour |
| **Groq** (`openai/gpt-oss-120b`) | Every LLM call | **$0.15 / M input, $0.60 / M output**. Caching halves input; batch halves everything |
| Serper | Alternative SERP | $0.30–1.00 / 1,000 |
| Brave | Alternative SERP | $5.00 / 1,000 |

**The LLM is not the expensive part.** At Groq's rates the worst observed agent run — 1,694 tokens
— costs **$0.00056**. A single Exa search costs $0.007, twelve times more. Anyone modelling this
business as an AI-inference cost structure is modelling the wrong line.

### Where that left us mispriced

Two capabilities were **below the 50% margin floor** once costed against real prices, and the audit
that found them is the reason to do this exercise against invoices rather than assumptions:

| Capability | Recorded cost | Real cost | Was | Now |
|---|---|---|---|---|
| `search.web` | $0.004 "blended" | **$0.007** (Exa) | 30% margin | 2 credits → 65% |
| `signal.news_scan` | $0.004 | **$0.0064** (Firecrawl) | 36% margin | 2 credits → 68% |
| `ai.account_qa` | $0.012 | **$0.0143** (2 Exa + model) | 52% margin | 4 credits → 64% |

Everything else is costed **conservatively** — the LLM capabilities by 4–16×, the Apify actors by
about 1.5×. Those are deliberately left alone: over-stating cost under-states margin, which is the
safe direction to be wrong in.

### The break-even ceiling

The most expensive capability costs **$0.00400 per credit**. One dollar therefore buys 250 credits
at cost, which sets a hard ceiling on how generous any plan can be:

| Credits per dollar | Meaning |
|---|---|
| **250 cr/$** | Zero margin at worst-case COGS. Never cross this at any volume |
| **125 cr/$** | The 50% gross-margin floor the code already enforces |
| **100 cr/$** | **The design rule** — 60% margin, with buffer |
| **50.3 cr/$** | Where our most generous *published* plan sits — 5.0× inside the survival limit |
| **80.0 cr/$** | The most generous thing sold anywhere — the 100K volume tier — 3.1× inside |

**When designing a plan, divide included credits by the dollar price and keep the answer under 100.**

### Infrastructure, and why it changes the answer at low volume

The minimum production shape on Azure costs **$110/month**: Container Apps $72 (app, worker, cache),
Postgres B1ms $18, registry, logs and egress $20. Fixed cost per credit falls as volume rises, so
the *fully loaded* break-even moves:

| Credits sold / month | Fully-loaded break-even |
|---|---|
| 5,000 | **38 cr/$** — below our richest plan |
| 25,000 | 119 cr/$ |
| 100,000 | 196 cr/$ |
| 1,000,000 | 243 cr/$ → converging on 250 |

**Below roughly 5,300 credits/month of total platform consumption, the richest tiers are underwater
once infrastructure is counted.** Above it, every tier is profitable even at worst-case COGS. The
platform has burned 3,094 credits all-time, so we are currently below that line — which is what
pre-revenue looks like, and it resolves with one customer.

Infrastructure is a step function, and the steps are shallow:

| Shape | Fixed / month | Revenue to cover it |
|---|---|---|
| Minimum (1 app replica) | $110 | 1.1 Accelerate customers |
| 3 app replicas (declared max) | $198 | 1.0 |
| + Postgres D2ds, 128 GB | $371 | 1.9 |

**Two Accelerate customers cover all infrastructure at full declared scale** — or a fraction of one
volume customer: any tier on the 25K–100K card covers the entire platform in under a month of its
own MRR. This is why infrastructure is not loaded into an individual deal's economics; allocating
all of it to one account is only correct if that account is the only one we have.

> **Closed 2026-08-26.** This previously read "there is no admin surface for cost rates, so when a
> provider raises prices the margin floor goes on validating against a stale number" — which is how
> `search.web` sat at 30% margin without anything complaining. `PUT /admin/billing/costs/{id}` now
> exists, and **recording a cost is never refused**: a price is a decision, a cost is an observation
> about what a provider charges, and refusing to record a vendor price rise leaves the floor
> checking against a number we know to be wrong. The response returns a work list of every
> capability the change pushed under the floor.

---

## 7. Why Scale is $149 and Scale Annual is $1,490

This is the section an investor should push on hardest, because it is where judgement enters.

**The gap we found is empirical.** Our busiest workspace burned **3,056 credits**. Growth includes
2,000. That customer would exhaust their allowance and hit overage every month — the worst possible
experience, because it converts a growth signal into a billing complaint. The next tier up,
Professional at 4,000 credits, leaves only 31% headroom over observed peak demand, which is thin
for a customer whose usage is growing.

**5,000 credits gives 64% headroom over measured peak demand.** That is the sizing decision, and it
comes from the data.

**$149 comes from the curve, not from a feel.** Interpolating credits-per-dollar between
Professional (31.0) and Business (40.2) for a 5,000-credit allowance lands at 33.6 cr/$, which is
$149. Had we priced it at $139 it would sit at 36.0 and undercut Business on rate while offering
less; at $159 it would sit at 31.4 and offer barely more than Professional. $149 is the price that
keeps the ladder monotonic — which is what stops customers gaming the tiers.

**Scale Annual is $1,490 — ten months for twelve.** A 16.7% discount for a twelve-month commitment
is the conventional band (typically 15–20%) and we chose the middle of it. The trade we are making
is explicit: **we give up 16.7% of revenue per customer to convert monthly churn risk into a
twelve-month commitment and to pull twelve months of cash forward.** For a company at our stage the
cash timing is worth more than the margin — annual prepay funds the enrichment spend that the
customer will incur over the year, so working capital moves in our favour rather than against it.

At $1,490 for 60,000 credits the annual plan sits at 40.3 cr/$ — deliberately level with Business
rather than beyond it, so annual commitment is rewarded with **discount, not with a better unit
rate that would cannibalise the top monthly tier.**

**Margin holds.** Scale at 93.7% and Scale Annual at 92.4% (observed mix), 86.6% and 83.9%
respectively in the worst case where every credit is spent on our most expensive capability. Both
clear the 50% floor by a wide margin.

---

## 8. How we actually charge — the five routes to revenue

The billing engine has one seam. Application code calls `check_and_meter(...)` and never mentions a
plan or a price; plans, quotas and prices are **rows, not branches**. That is what makes everything
below configuration rather than engineering.

### Route 1 — Self-serve subscription (the default)

Customer picks a plan from the in-product price list → hosted Stripe Checkout → Stripe creates the
subscription → we learn about it by webhook, never by writing it ourselves. Card data never touches
our infrastructure, so PCI scope stays at the provider.

Recurring revenue, collected automatically, no human in the loop.

### Route 2 — Usage overage

When a customer exceeds their included credits, the entitlement engine either blocks, throttles, or
charges overage, per the plan's configuration for that capability. Overage is rated into an invoice
at period close.

**Overage is priced at 5c per credit, above the dearest in-plan rate.** It was 1c, and that inverted
the ladder: in-plan credits sell for 2.48c to 4.75c, so exceeding your plan was two to five times
cheaper per credit than upgrading to a tier that covered the same usage. A customer acting rationally
would sit on the smallest plan and overflow forever, and the tier they were nominally on would stop
meaning anything. The pressure is deliberately uneven — a Core customer feels a 5% premium, a Scale
Annual customer feels 2x — because the customer on the cheapest rate has the most to gain from moving
up. A plan can still override the rate per capability, which is how a negotiated enterprise deal
avoids forking the catalog.

**Credits are pre-paid, so rating deducts what a period's burns already covered** — otherwise the
customer pays twice for one overage. Corrections are compensating negative rows, never deletes, so
the ledger stays auditable.

### Route 3 — Metered invoicing

Usage and overage are rated by us and then **raised as a real invoice at Stripe**, with our line
items, a hosted payment page and a PDF. Our rating remains the source of truth for what is owed; the
provider prices nothing. Collection is keyed by invoice id, so a retry can never double-charge.

Dunning retries on a configured schedule and escalates to `past_due`. It never silently voids a
debt.

### Route 4 — Pay as you go

No subscription, no allowance. Every action is rated onto a monthly invoice for exactly what was
used. Mechanically it is the overage path with the quota set to zero, which is why it needed no new
billing machinery — only a plan whose entitlements all start at nought. See §5.

### Route 5 — Enterprise invoice (see §10)

---

## 9. Selling part of the product

**"What if a customer only wants specific functionality?"** This is a configuration change, not a
build. It is the question the whole entitlement model exists to answer.

Eleven **module gates** are defined, each independently sellable on or off:

`module.agents` · `module.api` · `module.calling` · `module.discovery` · `module.integrations` ·
`module.lists` · `module.network` · `module.outreach` · `module.plays` · `module.relevance` ·
`module.signals`

Turning one off does three things at once, which is the point:

1. **The navigation item disappears** for users who cannot buy it, or becomes an upsell for admins
   who can — decided by whether the person has the agency to change the plan.
2. **The route is guarded**, so the page is not reachable by typing the URL or following a bookmark
   from a colleague on a richer plan.
3. **The server refuses**, which is the actual boundary. The first two are presentation.

**A module gate that only hides a menu item is a discount with no cost saving.** So gates carry
their dependent capabilities: `module.signals` carries the signal scans and inbox tasks;
`module.agents` carries orchestration runs and AI chat; `module.plays` carries automated play runs.
Disabling a module genuinely stops the spend, which is what makes a cheaper plan cheaper to *serve*
rather than merely cheaper to buy.

### Worked example — `core` at $19

Eight modules off, leaving the ungateable floor plus Lists and Relevance. Two deliberate exceptions
worth understanding:

- **`ai.scoring` is not tied to `module.relevance`.** Relevance scores are the most useful column on
  the Accounts page, which every plan includes. Cascading the gate would sell a page with its point
  removed.
- **`automation.account_refresh` is tied to nothing**, for the same reason.

The general principle: **gate the module, not the thing that makes the included pages worth using.**

### What is deliberately never sellable

Dashboard, Accounts, Contacts, Members, Settings and **Billing** cannot be gated by any plan, and a
test enforces it. Billing is the load-bearing one: gating it behind a plan would lock a customer out
of the only page where they could change the plan that locked them out.

### Selling API access specifically

`module.api` exists as its own gate, so "API access" is a line item that can be sold, withheld, or
priced separately at any tier. For a customer who wants *only* programmatic access, the packaging
is: a plan with `module.api` on, the UI modules off, and a credit allowance sized to their expected
call volume. That is a custom plan (§10), assembled from checkboxes, with no code change.

---

## 10. Enterprise

Enterprise is a **different sales motion with different mechanics**, not a bigger number on the same
form.

**Custom plans are per-tenant.** A negotiated deal becomes a plan row with `plan_class="custom"`,
built by selecting modules and setting a price and allowance. Only *changed* rows are stored — so a
module we add to the base plan next quarter still reaches that customer, rather than freezing them
in the past.

**Custom and enterprise plans are refused by self-serve Checkout with a 409.** This is deliberate: a
tenant admin must not be able to re-buy their own bespoke contract at whatever a public price row
happens to say. They are billed by `collect_invoice` instead — our rating, raised as a Stripe invoice
with a hosted payment page, or sent for payment on terms.

**Enterprise subscriptions often have no provider object at all.** A contract signed on paper and
invoiced never passes through Stripe's subscription lifecycle. Reconciliation therefore skips any
subscription with no `psp_subscription_id`, so real drift findings are not buried under deals that
were never supposed to be there.

**Reconciliation reports; it never repairs.** A missed webhook is otherwise invisible until a
customer complains. Which side is right depends on what the customer agreed to — an automated writer
would resolve that wrongly and destroy the evidence.

### Grandfathering

Subscriptions carry a `grandfathered` flag and frozen terms. **Editing a plan must never reprice an
existing subscriber.** A price rise applies to new business and to renewals we choose to move, not
retroactively to everyone on the plan.

---

## 11. Margin governance — what stops a bad price

For a finance reader, these are the controls, and they are code rather than process:

| Control | What it prevents |
|---|---|
| `rates.validate_rate()` refuses < 50% gross margin | A capability priced below cost. Runs on the seed too, so a bad price cannot arrive by deploy |
| Explicit, audited margin exception | Deliberate loss-leaders remain possible, but never silent |
| Plan-level margin **warning** | Flags a plan whose credits look expensive against its price — warns rather than blocks, because acquisition tiers and `free` are legitimately unprofitable |
| Every admin mutation audited with before/after | Price changes are attributable and reconstructable |
| Prices are rows | Repricing needs no deploy, so a pricing error is minutes to fix, not a release cycle |
| New plans start as **drafts** | A half-configured tier cannot become purchasable the moment it is saved |
| `plan_class` is set by the service, never by the request | Nobody can mint an `unlimited` or `internal` plan by typing a string |

**Enforcement has a kill switch and a shadow mode.** `NEXUS_BILLING_ENFORCEMENT` runs `off`,
`shadow` (evaluate and record, never block) or `on`. Shadow mode is what let us measure "what
happens if we turn enforcement on?" without turning it on — the metric
`nexus_billing_decisions_total{outcome="would_block"}` counts exactly that.

**The engine is biased toward allowing.** Unknown capability → allow. Tenant with no subscription →
allow. Engine raises → allow. A billing bug must degrade to giving service away, never to blocking a
paying customer. That bias is a deliberate commercial choice: the cost of a wrongly-blocked customer
exceeds the cost of a wrongly-served one, at our stage, by a wide margin.

---

## 12. What we do not know, and what would change our minds

An investor should discount the following.

**The sample is small.** 18 usage events, 4 active workspaces, 17 total. The *shape* of demand
(enrichment-heavy) is credible because it matches the product's cost structure. The *magnitude* is
not forecastable from this. Every margin figure here is a unit economic, not a revenue projection.

**We have no churn or conversion data.** No customer has yet completed a full billing cycle on a paid
self-serve plan. The 16.7% annual discount is therefore priced off convention, not off our own
observed monthly churn. If churn turns out to be low, we are over-discounting annual; if high, under.

**COGS is not fixed.** LLM inference cost is falling fast; contact-data cost is not, and it is 75%
of our burn. If a data provider raises prices, margin compresses on the line that matters most. The
mitigations are already built (shared stores, negative caching, source databases read ahead of paid
providers) but they are mitigations, not immunity.

**Gross margin is not company margin.** Infrastructure, salaries and support are not in these
numbers. At 92–96% gross margin the model has room for those; this document does not model them.

**What would change the plan ladder:** a customer segment consuming >20,000 credits/month (would
justify a tier above Business), enrichment COGS moving more than 30% in either direction, or
observed monthly churn materially outside 3–7% (would reprice the annual discount).

---

## Appendix A — Reproducing every figure

All figures come from the production database. The queries are in this repository's history; the
substantive ones are:

- **Cost per credit by capability:** join `billing_rate_cards.credits_per_unit` against
  `billing_cost_rates.unit_cost_usd` per `capability_id`; cost per credit is `unit_cost / credits_per_unit`.
- **Observed usage mix:** group `billing_usage_events` by `capability_id`, multiply `quantity` by the
  capability's `credits_per_unit`, express each as a share of the total.
- **Blended cost per credit:** the share-weighted average of per-capability cost per credit.
- **Plan margin:** `(base_price_cents/100 − included_credits × blended_cost) ÷ (base_price_cents/100)`.
- **Break-even burn:** `(base_price_cents/100) ÷ blended_cost`.

Source of truth for the model itself: `nexus/billing/` — `catalog.py` (capabilities), `plans.py`
(plans and entitlements), `rates.py` (prices, costs and the margin floor), `plan_authoring.py`
(creating a sellable tier), `custom_plans.py` (enterprise), `collection.py` (invoicing).

## Appendix B — Glossary

| Term | Meaning |
|---|---|
| **Capability** | A billable business action (`ai.email_draft`, `enrich.contact`). The unit pricing attaches to |
| **Credit** | The customer-facing currency. One capability consumes a fixed number per use |
| **Rate card** | Credits charged per unit of a capability |
| **Cost rate** | Our COGS per unit of a capability |
| **Entitlement** | What one plan grants for one capability: mode, quota, limits, overage price |
| **Module gate** | A coarse on/off capability (`module.signals`) that carries dependent spend |
| **Plan class** | `standard` (sellable), `custom`/`enterprise` (admin-managed), `free`, `trial`, `unlimited`, `internal` |
| **Shadow mode** | Enforcement evaluates and records every decision but never blocks |
| **Gauge** | A metric resolved live (seats) rather than summed from events |
