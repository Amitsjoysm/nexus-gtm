# NEXUS GTM — Product Requirements Document (PRD)

**Status:** Production · **Owner:** Product · **Last updated:** 2026‑06‑17

---

## 1. Overview

NEXUS GTM is a multi‑tenant, AI‑powered Go‑To‑Market intelligence platform (a Pocus/Clay‑class product). It ingests buying signals, scores accounts against each customer's ICP, runs AI agents (research, messaging, enrichment, discovery), and drives the full rep workflow (inbox, lists, plays, cadences, approvals, CRM sync) — with a human approval gate before any outbound message sends.

**One‑line:** *Turn buying signals into approved, grounded, sent outreach — and keep your CRM in sync — without the SDR busywork.*

---

## 2. Goals & non‑goals

### Goals
- G1. Surface the **right account at the right time** via signal‑driven, ICP‑scored prioritization.
- G2. Produce **grounded, personalized** outreach with AI that a rep edits and approves, never auto‑spams.
- G3. **Automate the busywork** (research, scoring, enrichment, sequencing, CRM logging) end‑to‑end.
- G4. Be **production‑grade and multi‑tenant‑safe** — strict isolation, RBAC, and security by default.
- G5. Run **fully offline/deterministically** for tests and pilots (no key required), and switch to real providers with one env change.

### Non‑goals
- Replacing the rep's judgment on targeting and messaging (NEXUS assists; the human decides).
- Being a full CRM (it *syncs to* the customer's CRM, system of record stays theirs).
- Sending without human approval (the approval gate is a product principle, not a toggle to remove).

---

## 3. Personas

| Persona | Needs | Primary surfaces |
|---|---|---|
| **SDR / Rep** | A prioritized day, grounded drafts, fast triage, one‑click send. | Inbox, Accounts, Approvals, Orchestrator |
| **Manager** | Pipeline visibility, attribution, ICP control, team governance. | Relevance, Analytics, Members, Settings |
| **RevOps / Admin** | Integrations, automation policy, data hygiene, security. | Settings, Integrations, CRM sync, Members |
| **Owner** | The above + workspace provisioning and billing‑level control. | All + workspace management |

---

## 4. Core concepts

- **Tenant / Workspace** — the isolation boundary; one customer org. Users hold memberships in one or more tenants and switch between them.
- **Account** — a target company, enriched with firmographics, scored for Fit, carrying signals and contacts.
- **Contact** — a person at an account, with title, seniority, and a verified email status.
- **Signal** — a detected buying event (funding, hiring, product, news) with strength and source.
- **Relevance profile / ICP** — the customer's definition of a good account + value props + product context; drives scoring and grounds messaging.
- **Fit score (0–100)** — deterministic ICP match across Industry / Size / Geo / Tech, with tunable weights.
- **Run** — an orchestrated multi‑agent workflow (research → score → compose → send) with an approval gate.
- **Approval** — the human gate holding a drafted send until a reviewer approves, redrafts, or rejects.

---

## 5. Functional requirements

### 5.1 Accounts & contacts
- List accounts with Fit score, industry, location, employees, LinkedIn, and source; filter (industry/location/source/min‑Fit) and search.
- Per‑account actions: push to CRM, archive/remove, enrich, find contacts, find lookalikes.
- Account 360: firmographics, Fit breakdown, signals, contacts, and an **AI Actions** tab (research / messaging / Q&A / contact recommendation).
- Workspace‑wide contacts list with email status, verify action, LinkedIn, and CRM push. **Tenant‑scoped at the query layer** (never leaks across tenants).

### 5.2 Signals & Inbox
- Ingest buying signals from a real web/news source (Exa‑backed), classified by kind and strength; demo/fabricated signals disabled in production.
- Convert relevant signals on Fit‑qualifying accounts into prioritized **Inbox tasks** (account‑centric title + headline reason + suggested action).
- Keyboard‑first triage; **Open/Completed** views; complete and **reopen** (recover) tasks; gating so junk news never becomes a task.

### 5.3 Relevance & scoring
- Edit ICP (industries, countries, employee band, required tech), value props, product context.
- Deterministic Fit scoring with **user‑tunable weights** (Industry/Size/Geo/Tech) via sliders; engine normalizes weights.
- **Outcome‑learned weights** that nudge defaults toward winning traits; explicit user weights take precedence.

### 5.4 AI agents & orchestration
- Agents: **research, scoring, messaging, discovery, contact‑rec, Q&A** — all grounded in the relevance context.
- Orchestrator plans and runs a DAG of agents with durable run state, a live event stream, and an approval gate before send.
- **Discovery** surfaces net‑new companies/contacts matching an ICP; CSV import for bulk load.
- LLM provider is pluggable (Anthropic / Groq / OpenAI‑compatible / deterministic stub) with automatic fallback.

### 5.5 Approvals
- Human gate for every outbound draft: review, **edit**, **AI redraft with instructions**, **send‑from mailbox** selection, **approve & send**, or **reject with reason**.
- Hard gates: refuse **ungrounded** drafts and **undeliverable** addresses regardless of the reviewer's click.

### 5.6 Outreach: campaigns, cadences, sending
- Campaigns: multi‑account personalized drafting → approve → send, with live progress.
- Cadences: multi‑touch sequences with scheduling, stop conditions, and optional review‑each‑touch.
- **Sending** via the customer's own SMTP mailbox(es): multiple mailboxes, per‑mailbox test, default selection, and per‑send routing. No external SEP required.
- Email **verification** (Reacher‑class) gates risky/invalid sends.

### 5.7 CRM sync (HubSpot)
- Bi‑directional connector: outbound upsert of **companies (by domain)** + **contacts (by email)** + associations + activity **notes**; idempotent (no duplicates on re‑sync); inbound company fetch.
- Change‑aware auto‑sync via a continuous heartbeat; manual per‑account push; live sync status.
- Pluggable provider (`stub | salesforce | hubspot`); HubSpot shipped and verified against a live portal.

### 5.8 Plays, lists, alerts, analytics
- Plays: signal‑triggered automation rules (incl. surfacing new accounts).
- Lists: saved firmographic/Fit segments feeding campaign targeting.
- Alerts: high‑priority routing.
- Analytics: live activity feed, KPIs, manager attribution + funnel, outcome capture.

### 5.9 Identity, teams, multi‑tenancy
- Email/password auth (JWT); workspaces; RBAC roles **owner > admin > manager > rep**; member invite/role management; last‑owner protection.
- Workspace switcher; create new workspaces.

---

## 6. Non‑functional requirements

- **Security / isolation:** two‑layer tenant isolation — application `TenantSession` scoping **and** Postgres Row‑Level Security; the API connects as a **least‑privilege, non‑superuser** role; RBAC on every endpoint; production rejects the insecure default JWT secret; secrets only in gitignored env.
- **Reliability:** durable run state; idempotent CRM sync and approvals; connectors never raise across the boundary (a flaky CRM can't break a send); self‑healing sweeps (failed pushes stay due).
- **Scalability:** Valkey‑backed work queue; app and worker scale independently; change‑aware sweeps avoid full scans; SSE for live progress.
- **Performance:** bulk/aggregate queries on hot paths (counts, steps) instead of N+1.
- **Portability:** runs fully offline (SQLite + deterministic stub LLM + in‑memory queue) for tests/pilots; single‑command Docker deploy to any domain with automatic HTTPS.
- **Accessibility:** semantic HTML, labelled controls, full keyboard support, visible focus, reduced‑motion fallbacks; contrast ≥ 4.5:1.
- **Observability:** activity feed, CRM sync status, run event log.

---

## 7. Architecture (summary)

- **Backend:** Python 3.11, FastAPI, async SQLAlchemy 2.0, Pydantic v2, Alembic. In‑process EventBus; Valkey/Redis queue; worker process for automation heartbeat + jobs.
- **Frontend:** React 18 + TypeScript (strict) + Vite; CSS‑token design system; owned component library; SPA served by FastAPI.
- **Data:** Postgres 16 (prod) / SQLite (offline tests). RLS on all tenant‑scoped tables.
- **Integrations:** LLM (Anthropic/Groq/OpenAI‑compat/stub), Exa search, Reacher email verification, HubSpot CRM, SMTP send (Gmail/Outlook), Caddy TLS.

---

## 8. Acceptance criteria (production readiness)

- [x] Every endpoint is tenant‑scoped and RBAC‑gated; cross‑tenant isolation proven (app layer + DB RLS).
- [x] No fabricated data in production paths; real providers selected via env (LLM, search, verify, CRM); demo signals off.
- [x] Approval gate enforced; ungrounded/undeliverable sends refused.
- [x] HubSpot sync verified against a live portal (companies + contacts + associations, idempotent).
- [x] Full automated test suite green (offline, deterministic).
- [x] Single‑command deploy to any domain with HTTPS; secrets generated, never committed.

---

## 9. Out of scope / future

- Additional CRM adapters (Salesforce interface exists, adapter to be completed), inbound‑reply handling and meeting booking, dialer/voice, advanced sequence branching, deeper analytics/forecasting, SSO/SCIM.
