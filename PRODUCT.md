# NEXUS GTM — Product

## What it is
NEXUS GTM is an AI-powered Go-To-Market intelligence platform for B2B revenue teams
(a Pocus.com-class product). It ingests buying **signals** (funding, hiring, tech adoption,
news), scores **account** relevance against an Ideal Customer Profile, runs AI **agents**
(research, messaging, enrichment), and turns all of it into a focused rep **workflow**:
a prioritized inbox, saved lists, automated plays, and real-time alerts.

## Who uses it
- **Reps / AEs** — live in the Inbox: work prioritized accounts, run research, draft outreach,
  push contacts to the sales-engagement platform. Speed and focus matter most.
- **Managers** — build lists and plays, watch analytics, tune the relevance profile.
- **Admins / Owners** — manage workspace members, roles, and integrations (CRM, SEP).

Multi-tenant SaaS. RBAC: owner > admin > manager > rep. A rep must never see another tenant's data.

## Core screens (the app we are building)
1. **Auth** — sign up (create workspace) / sign in. First impression; must feel trustworthy and fast.
2. **Dashboard** — at-a-glance: pipeline/analytics KPIs, recent signals, open alerts, top inbox tasks.
3. **Inbox** — prioritized task queue. Each task: account, reason, priority; actions: Research,
   Draft message, Done. The rep's daily driver.
4. **Accounts** — searchable/filterable table of accounts with relevance; row → Account 360.
5. **Account 360** — one account: firmographics, contacts, signals timeline, agent actions.
6. **Signals** — the signal library: filter by kind/account, newest-first, strength.
7. **Alerts** — list + acknowledge; severity (info/warning/critical), source, channel.
8. **Members / Settings** — workspace members, invite, role change, remove (last-owner protected).

## Product principles
- **Signal → action in one move.** Every insight has a next step attached.
- **Trust the data.** Show source, recency, and confidence; never a black box.
- **Speed is a feature.** Reps triage dozens of accounts a day — no wasted clicks, no spinners
  without skeletons, keyboard-friendly.
- **Calm density.** Lots of information, organized so nothing feels noisy.

## Constraints
- Backend is FastAPI; the SPA is served as static files with client-side routing.
- Runs offline in dev (SQLite + stubbed LLM). The UI must degrade gracefully on empty data.
- Reduce external dependencies; we own our component library.
