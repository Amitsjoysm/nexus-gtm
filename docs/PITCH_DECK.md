# NEXUS GTM — Pitch Deck

*10 slides. Compact. Every feature.*

---

## Slide 1 — NEXUS GTM

### Signals in. Approved, grounded outreach out. CRM in sync.

**The AI Go‑To‑Market workbench** that turns buying signals into a prioritized daily list, drafts grounded outreach your reps approve, sends from your mailbox, and keeps your CRM current — automatically.

*A Pocus/Clay‑class platform, production‑grade and multi‑tenant.*

---

## Slide 2 — The problem

GTM teams lose **30–50% of selling time** to non‑selling work:

- 🔍 Manual account research across a dozen tabs
- ⏰ No reliable "who's in‑market **now**" signal → mistimed outreach
- ✉️ Generic, ungrounded emails → low replies, domain‑reputation risk
- 🗃️ Manual CRM entry → stale data, unreliable forecasts
- 🧩 Tool sprawl: signals + enrichment + sequencer + sync, glued together

**Result:** reps sell less; pipeline is spray‑and‑pray; leadership flies blind.

---

## Slide 3 — The solution

One workbench that runs the whole loop:

> **Signal → Fit score → Inbox task → AI draft → Approve → Send → CRM sync → Outcome → smarter scoring**

The rep keeps the three decisions that matter — **who**, **what**, **whether** — NEXUS does the research, scoring, drafting, sequencing, and busywork in between. **Nothing sends without human approval.**

---

## Slide 4 — How it works

1. **Detect** — real buying signals (funding, hiring, product, news) land on your accounts.
2. **Score** — every account gets a 0–100 **Fit** score vs. *your* ICP (tunable + self‑learning).
3. **Prioritize** — relevant signals on Fit‑qualified accounts become a ranked **Inbox**.
4. **Draft** — AI agents research the account and write a **grounded**, personalized message.
5. **Approve** — you review, redraft with AI, pick a mailbox, and approve (or reject with a reason).
6. **Send & sync** — it sends from your mailbox and logs to your CRM.

---

## Slide 5 — Product: target & prioritize

- **Accounts** — Fit score, location, employees, LinkedIn; filter & search; push‑to‑CRM; archive. **Account 360** with firmographics, signals, contacts.
- **Contacts** — workspace‑wide, with verified email status (valid/risky/invalid) and one‑click re‑verify.
- **Signals** — the buying‑event feed that drives timing.
- **Inbox** — keyboard‑first daily task list (`J/K/E/D`), Open/Completed views, recover any closed task.
- **Relevance** — your ICP (industries, size, geo, tech) + value props; **Fit‑weighting sliders**; weights **auto‑learn** from won deals.

---

## Slide 6 — Product: draft, approve & send

- **AI Agents** — research, messaging, Q&A, contact‑rec, discovery — all **grounded** in your context.
- **Orchestrator** — multi‑step AI runs (research→score→compose→send) + **discovery** of net‑new accounts/contacts + CSV import + live run console.
- **Approvals** — the human gate: edit, **AI‑redraft with instructions**, **send‑from mailbox** picker, approve, or **reject with reason**. Ungrounded/undeliverable sends are **refused**.
- **Outreach** — **Campaigns** (personalized at scale) + **Cadences** (multi‑touch, review‑each‑touch) sending from **your own multiple SMTP mailboxes**; email verification gates risky sends.

---

## Slide 7 — Product: automate & sync

- **CRM sync (HubSpot, live)** — idempotent upsert of **companies (by domain)** + **contacts (by email)** + associations + activity **notes**; change‑aware **auto‑sync**; manual push; live status. *(Verified against a real portal: 12 companies + 15 contacts, no duplicates.)*
- **Continuous automation** — a heartbeat re‑scores stale accounts and advances cadences on schedule.
- **Plays / Lists / Alerts** — signal‑triggered rules, saved segments feeding campaigns, priority routing.
- **Analytics** — live activity feed, KPIs, manager **attribution + funnel**, outcome capture.

---

## Slide 8 — Built for production & trust

- **Multi‑tenant isolation, two layers:** application‑level scoping **and** Postgres Row‑Level Security; the API runs as a **least‑privilege, non‑superuser** DB role.
- **RBAC** (owner > admin > manager > rep) on every endpoint; reject reasons and run logs for audit.
- **Safety by design:** grounded‑only sends + human approval + deliverability gate; production rejects insecure defaults; secrets never committed.
- **Reliable & scalable:** durable runs, idempotent sync, queue‑backed workers that scale independently; connectors never break a send.
- **Stack:** FastAPI + async SQLAlchemy + Postgres; React 18 + TypeScript; Valkey queue; Caddy auto‑HTTPS.

---

## Slide 9 — Why we win

| | Point tools | NEXUS GTM |
|---|---|---|
| Signals → action | separate tool | **one loop to a sent, approved email** |
| Message quality | templates | **grounded AI drafts, redraftable, gated** |
| Targeting | static slide | **tunable + self‑learning Fit scoring** |
| CRM hygiene | manual | **automatic, idempotent bi‑directional sync** |
| Safety | trust the rep | **enforced approval + grounding + deliverability** |
| Stack | 4–5 vendors | **one consolidated, multi‑tenant platform** |

**Time‑to‑value:** runs in a **deterministic pilot mode with zero API keys**, then flips to live providers with one config change.

---

## Slide 10 — Deploy & get started

- **One command, any domain:** `./deploy.sh your.domain.com` — generates secrets, builds, migrates, provisions the secure DB role + RLS, and gets automatic HTTPS.
- **Pilot today** in stub mode (no keys); **go live** by adding LLM + search + verify + CRM + SMTP keys and connecting mailboxes.
- **Already proven live:** real HubSpot sync, real AI redraft, real signal‑driven inbox, verified tenant isolation.

### Spend your reps' time on conversations — let NEXUS handle everything around them.

*Contact: your NEXUS team · Handbook, PRD, and BRD available in `/docs`.*
