# Infojoy GTM — Business Requirements Document (BRD)

**Status:** Production · **Audience:** Executive, Sales leadership, RevOps, Finance · **Last updated:** 2026‑06‑24

---

## 1. Executive summary

Go‑to‑market teams waste the majority of their selling time on low‑value work: researching accounts, guessing who's in‑market, writing one‑off emails, copying data into the CRM, and chasing stale sequences. Infojoy GTM is an AI GTM intelligence platform that **detects buying signals, scores accounts against the customer's ICP, drafts grounded outreach, and keeps the CRM in sync** — while keeping a human in the loop for every send.

The business outcome: reps spend their time on **timely, well‑targeted, personalized** conversations instead of busywork, pipeline is built from *signals* rather than spray‑and‑pray, and revenue leadership gets clean attribution from first touch to outcome.

---

## 2. Business problem

| Problem | Cost today |
|---|---|
| Reps research accounts manually across many tabs | 30–50% of selling time lost to non‑selling work |
| No reliable "who's in‑market now" signal | Outreach is mistimed; reply rates suffer |
| Generic, ungrounded emails | Low conversion, brand/domain reputation risk |
| CRM data entry is manual and lagging | Forecasts and reporting are unreliable |
| ICP lives in a slide, not in the workflow | Inconsistent targeting across the team |
| Tool sprawl (signal tool + enrichment + sequencer + CRM) | High cost, brittle integrations, poor adoption |

---

## 3. Vision & value proposition

**Vision:** every rep starts the day with a short, ranked list of *who to contact now and why*, a grounded draft ready to approve, and a CRM that updates itself.

**Value pillars**
1. **Timing** — signal‑driven prioritization puts in‑market accounts at the top.
2. **Relevance** — ICP‑based Fit scoring, tunable and self‑learning from wins.
3. **Quality** — AI drafts grounded in real research; ungrounded/undeliverable sends are blocked.
4. **Control** — nothing sends without human approval; reject/redraft with one click.
5. **Hygiene** — bi‑directional CRM sync keeps the system of record current automatically.
6. **Multi‑channel** — the same grounded intelligence powers **email and the phone**: a prioritized call queue with AI talk tracks and a **sourced pre‑call research brief**, so calls are as well‑prepared as emails.
7. **Net‑new pipeline** — **daily ICP auto‑discovery** surfaces fresh, strictly‑matched accounts every morning, so the funnel refills itself.
8. **Consolidation** — signals + enrichment + scoring + sequencing (email **and** call) + approvals + CRM sync in one workbench.

---

## 4. Stakeholders

| Stakeholder | Interest | Success looks like |
|---|---|---|
| **SDR/AE** | Hit quota with less grind | More meetings booked per hour worked |
| **Sales manager** | Team output & coaching | Consistent targeting; visible pipeline & attribution |
| **RevOps** | Clean data & integrations | CRM stays current with zero manual entry |
| **CRO / VP Sales** | Predictable pipeline & ROI | Signal‑sourced pipeline up; cost per opportunity down |
| **Security/IT** | Data protection | Tenant isolation, least‑privilege, RBAC, no secret sprawl |
| **Finance** | Tool ROI & consolidation | One platform replacing several point tools |

---

## 5. Business requirements

- **BR1 — Prioritized rep day:** the product must convert buying signals on in‑ICP accounts into a ranked daily task list, and let reps clear it fast with full recoverability.
- **BR2 — Targeting consistency:** ICP and Fit weighting must be configurable centrally and applied uniformly to every account; scoring must improve as wins are logged.
- **BR3 — Message quality & safety:** all outbound must be grounded in research and pass a human approval gate; ungrounded or undeliverable sends are refused.
- **BR4 — Outreach execution:** support 1:1 and at‑scale campaigns plus multi‑touch cadences, sending from the customer's own mailboxes.
- **BR5 — CRM as source of truth:** scored accounts, contacts, and activity must sync to the customer's CRM (HubSpot) automatically and idempotently.
- **BR6 — Multi‑team safety:** strict isolation between teams/customers; role‑based access; auditable decisions (reject reasons, run logs).
- **BR7 — Fast, low‑risk adoption:** deployable to a VM **or the cloud (AWS/Azure)** in one command; usable in a deterministic pilot mode with no API keys; switch to live providers via configuration.
- **BR8 — Multi‑channel outreach:** SDRs must be able to run a prioritized **calling** workflow — queue, AI scripts, and a sourced pre‑call brief — alongside email, with calls and emails sequenced together in cadences.
- **BR9 — Net‑new pipeline:** the product must proactively surface new ICP‑matching accounts daily (strict match), not only score the ones already loaded.
- **BR10 — Secure self‑serve onboarding:** sign‑up must be email‑verified (OTP), support self‑service password reset, and resist abuse (rate limiting, no account enumeration) — enterprise‑acceptable from day one.

---

## 6. Success metrics (KPIs)

| Category | Metric | Target direction |
|---|---|---|
| Efficiency | Non‑selling time per rep | ↓ (research/data‑entry automated) |
| Activity quality | % outreach grounded in research | → 100% (enforced) |
| Timing | Median time from signal to first touch | ↓ |
| Conversion | Reply / meeting rate per 100 sends | ↑ |
| Pipeline | Signal‑sourced opportunities | ↑ |
| Hygiene | CRM records updated automatically | ↑ (manual entry → ~0) |
| Adoption | Inbox‑to‑zero rate; weekly active reps | ↑ |
| Governance | Cross‑tenant data incidents | 0 |

---

## 7. Scope

**In scope (delivered):** signal ingestion; ICP scoring with tunable + learned weights; **daily ICP auto‑discovery** of net‑new accounts; inbox/triage with recovery; account/contact enrichment, discovery, lookalikes; AI research/messaging/Q&A/contact‑rec; **person‑level hyper‑personalization** (email + call, Apify social seam); orchestrated runs; approvals with AI redraft, mailbox selection, reject reasons; campaigns; cadences (email **and** call steps); multi‑mailbox SMTP sending; email verification; **cold‑calling workflow** (call queue, AI scripts, sourced pre‑call research brief, dispositions, click‑to‑dial); HubSpot bi‑directional sync; plays; lists; alerts; analytics/attribution; multi‑tenant RBAC; **secure self‑serve onboarding** (OTP registration, password reset, rate limiting); one‑command secure deploy to VM or cloud (AWS/Azure) with CI/CD quality gates.

**Out of scope (now):** additional CRMs beyond HubSpot (Salesforce interface exists); inbound reply handling/meeting booking; a live telephony provider (Twilio) past the offline‑safe seam, plus call recording/transcription; SSO/SCIM; advanced forecasting.

---

## 8. Assumptions & dependencies

- Customers provide their own provider keys for production output: an LLM key (Anthropic/Groq/OpenAI‑compatible), a search key (Exa) for signals/discovery, an email‑verification endpoint (Reacher‑class), a CRM token (HubSpot private app), and SMTP app passwords for sending — plus a **system transactional mailbox** (for OTP/reset email) and, optionally, a **telephony provider** for live dialing and an **Apify** token for social personalization.
- A **deterministic stub mode** runs the full loop with no keys for pilots and testing (telephony, personalization, and email all degrade to safe offline stubs).
- Deployment: single‑VM Docker (Compose + Caddy) **or** managed cloud — AWS ECS Fargate (optionally managed RDS + ElastiCache) or Azure Container Apps (managed data) — via one Terraform‑backed script.

---

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| AI sends low‑quality/unsafe email | Grounded‑only + human approval gate + deliverability gate (all enforced) |
| Cross‑tenant data leakage | Two‑layer isolation (app scoping + DB RLS) + least‑privilege DB role + RBAC |
| Provider/API outages | Connectors never raise across the boundary; LLM fallback chain; self‑healing sweeps |
| Cost of LLM/search at scale | Pluggable providers; deterministic stub for non‑production; change‑aware sweeps avoid waste |
| Low adoption | Keyboard‑first inbox, one‑click approvals, recovery of mistakes, fast time‑to‑value |
| Secret leakage | Secrets only in gitignored env; generated on deploy; never committed |

---

## 10. ROI thesis

If Infojoy returns even a fraction of the 30–50% of selling time lost to busywork, and lifts reply rates by sending **better‑timed, grounded** messages, the platform pays for itself by increasing meetings per rep while consolidating several point tools (signal, enrichment, sequencer, data‑sync) into one. Cleaner CRM data additionally improves forecast accuracy and reduces RevOps overhead.

---

## 11. Go‑to‑market & rollout

1. **Pilot (stub mode):** stand up on a subdomain, load a team's accounts, tune ICP — no keys required — to demonstrate the workflow.
2. **Live providers:** add LLM/search/verify/CRM/SMTP keys via configuration; connect mailboxes; turn on auto‑sync.
3. **Team rollout:** onboard reps to the Inbox‑first daily rhythm; managers own Relevance and review attribution weekly.
4. **Expand:** additional workspaces/teams, more cadences, more signal sources.
