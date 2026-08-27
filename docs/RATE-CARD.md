# NEXUS GTM — Rate Card

**For the sales team.** Current as of 2026-08-27. Prices verified against the live billing tables.

Every margin below is computed at **worst-case COGS** — the price we pay if every single credit a
customer spends goes to the most expensive capability we sell. Real measured usage runs at less
than half that. So each number here is the pessimistic case, not the hoped-for one.

---

## 1. The published ladder

What a customer can buy themselves, without talking to us.

| Plan | Price | Credits | $/credit | Seats |
|---|---|---|---|---|
| **Free** | $0 | 200 / month | — | 1 |
| **Launch** | **$99** / month | 2,000 / month | $0.0495 | up to 25 |
| **Launch — annual** | **$950** / year | 24,000 / year | $0.0396 | up to 25 |
| **Accelerate** | **$199** / month | 8,000 / month | $0.0249 | up to 100 |
| **Accelerate — annual** | **$1,910** / year | 96,000 / year | $0.0199 | up to 100 |

Annual is **20% off** — twelve months for the price of nine and a half. Credits are exactly 12× the
monthly allowance: the discount is taken on price, never on credits.

**Beyond the allowance, usage bills at $0.05 per credit** with no commitment. That is deliberately
the most expensive way to buy — it must never be cheaper to overflow a small plan than to move up.

### Margin on the ladder

| Plan | Revenue | COGS (expected) | GM | COGS (worst case) | GM |
|---|---|---|---|---|---|
| Launch | $99 | $3.76 | 96.2% | $8.00 | **91.9%** |
| Launch annual | $950 | $45.12 | 95.3% | $96.00 | **89.9%** |
| Accelerate | $199 | $15.04 | 92.4% | $32.00 | **83.9%** |
| Accelerate annual | $1,910 | $180.48 | 90.6% | $384.00 | **79.9%** |

A **Free** workspace costs at most **$0.80** to serve for its whole life. That is what makes an open
signup funnel safe at any volume — the constraint free tiers actually fail on.

---

## 2. Volume rate card — 25K to 100K credits

For customers whose consumption outgrows Accelerate. Sold as a monthly commitment; annual available.

| Credits / month | **Monthly** | $/credit | vs. Accelerate annual | vs. no-commitment |
|---|---|---|---|---|
| **25,000** | **$425** / mo | $0.0170 | **15% better** | 66% off |
| **50,000** | **$750** / mo | $0.0150 | **25% better** | 70% off |
| **75,000** | **$1,000** / mo | $0.0133 | **33% better** | 73% off |
| **100,000** | **$1,250** / mo | $0.0125 | **37% better** | 75% off |

### Annual commitment — 10% off, paid up front

| Credits / month | **Annual (ACV)** | Effective / mo | $/credit | Credits / year |
|---|---|---|---|---|
| 25,000 | **$4,590** | $382 | $0.0153 | 300,000 |
| 50,000 | **$8,100** | $675 | $0.0135 | 600,000 |
| 75,000 | **$10,800** | $900 | $0.0120 | 900,000 |
| 100,000 | **$13,500** | $1,125 | $0.0112 | 1,200,000 |

> **Why 10% here and 20% on the published ladder.** The volume unit price is already discounted
> 15–37%. Stacking a full term discount on top of a volume discount pushes the largest tier below
> the margin floor at worst-case usage. One discount or the other, not both.

---

## 3. What you may discount — the floor

**The floor is 50% gross margin at worst-case COGS.** Below it the deal is not worth signing.

| Credits / month | List MRR | **Floor MRR** | **Headroom** | Gross margin at list | Contribution at list |
|---|---|---|---|---|---|
| 25,000 | $425 | **$213** | **50%** | 73.5% | 64.7% |
| 50,000 | $750 | **$425** | **43%** | 70.4% | 60.4% |
| 75,000 | $1,000 | **$638** | **36%** | 67.1% | 55.8% |
| 100,000 | $1,250 | **$850** | **32%** | 65.1% | 53.1% |

- **Gross margin** = revenue − credit COGS − payment processing. This is the line the billing engine
  itself enforces on every rate card.
- **Contribution** = the above, minus customer support (0.5–2.0 hrs/month at a $75/hr loaded rate).

**Approval:** discounts inside the headroom column are a sales-leader decision. Anything past the
floor MRR needs finance, and needs a reason that is not "the customer asked".

---

## 4. What the price has to cover

| Cost | Amount | Notes |
|---|---|---|
| **Credit COGS — expected** | **$0.00188** / credit | Measured, share-weighted at real usage mix |
| **Credit COGS — worst case** | **$0.00400** / credit | Every credit on the priciest capability |
| **Payment processing** | 2.9% + $0.30 | Stripe, per charge |
| **Support** | $37–$150 / month | 0.5–2.0 hrs at $75/hr loaded, scaling with account size |
| **Infrastructure** | **$110–$371** / month | Platform-wide, *not* per customer — see below |

### Infrastructure is a platform cost, not a deal cost

| Shape | $/month | $/year | Covered by |
|---|---|---|---|
| Minimum (1 app replica) | $110 | $1,320 | 0.1 × one 100K customer |
| 3 app replicas (declared max) | $198 | $2,376 | 0.2 × one 100K customer |
| + Postgres D2ds, 128 GB | $371 | $4,452 | 0.3 × one 100K customer |

**Any single volume customer covers the entire platform in under one month of their own MRR.** Do
not load infrastructure into an individual deal's economics — it is shared, and allocating all of
it to one account only makes sense if that account is the only one we have.

---

## 5. Break-even — the number never to cross

Credits per dollar. **Divide included credits by the dollar price; keep the answer low.**

| Line | cr/$ |
|---|---|
| **Survival limit** — pure COGS, worst case | **250** |
| Pure COGS at expected mix | 532 |
| Fully loaded @ 25,000 cr/mo sold | 119 |
| Fully loaded @ 100,000 cr/mo sold | 167 |
| Fully loaded @ 500,000 cr/mo sold | 211 |
| **The 50% margin floor the code enforces** | **125** |
| **Design rule for any new plan** | **under 100** |

**The most generous thing on this entire card is the 100K tier at 80 cr/$ — 3.1× inside the
worst-case survival limit.** The published ladder tops out at 50.3 cr/$, which is 5× inside.

---

## 6. Enterprise and partial-product deals

**Custom plans are built by a superadmin in the Control plane**, not by editing code. A custom plan
is a real plan row scoped to one tenant: its own price, its own credit allowance, and per-module
entitlements.

**Selling part of the product is a supported motion.** Every area of the app is behind a module
gate — Signals, Lists, Plays, Relevance, Agents, Network, Campaigns, Calling, Integrations,
Outreach. A deal can switch any of them off and the price comes down accordingly. Modules a
customer does not buy also cost us nothing to serve: the gates carry their underlying capabilities,
so a plan without Signals does not run signal collection for that tenant.

**Six things are never gateable** — Dashboard, Accounts, Contacts, Members, Settings and Billing.
Billing especially: gating the page where a customer changes their plan locks them out of fixing
the plan that locked them out.

Custom and enterprise plans are **invoiced**, not self-serve checkout. They are deliberately refused
by the hosted checkout flow, so a customer cannot accidentally buy a negotiated deal with a card.

### Quoting an enterprise deal

1. Start from the volume tier nearest their expected consumption.
2. Subtract for modules they are not buying.
3. Check the result against the **floor MRR** in section 3. That check is not optional.
4. Anything below the floor goes to finance with a written reason.

---

## 7. Questions a prospect will ask

**"What is a credit?"** A unit of work. An enriched account, a research brief, a phone lookup, a
web search, a minute of calling. The rate for every action is published in-product under Billing,
and usage is visible per capability in real time.

**"What happens when we run out?"** Nothing breaks. Usage continues at $0.05/credit and appears on
the next invoice. We do not cut off a customer mid-quarter.

**"Can we pay as we go?"** Yes — that is the $0.05 rate with no commitment. It is 66–75% more
expensive than committing, which is the point.

**"Do unused credits roll over?"** No. Allowances reset each period. Annual plans front-load the
whole year's credits, so seasonality inside the year is already handled.

**"Can we start free?"** Yes. 200 credits, every feature, one seat, no card.

---

*Cost inputs measured from the live rate and cost tables. Margin arithmetic is reproducible —
the same figures drive the billing engine's own 50% floor validation, so a price that passes here
is a price the system will accept.*
