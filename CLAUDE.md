# NEXUS GTM — AI-Powered GTM Intelligence Platform

## 🎯 What We're Building

**NEXUS GTM** is an AI-powered Go-To-Market intelligence platform (a [Pocus.com](https://pocus.com)-class product):
a multi-tenant SaaS for B2B revenue teams that:

1. **Ingests buying signals** — funding rounds, hiring, tech stack changes, news mentions
2. **Scores account relevance** — deterministic ICP-fit using Relevance Engine (no LLM)
3. **Runs AI agents** — research, messaging, enrichment, contact recommendations
4. **Automates rep workflows** — prioritized inbox, saved lists, signal-triggered plays, alerts

**Key principle**: Signal → Action in one move. Reps triage dozens of accounts daily; speed matters.

**Users**:
- **Sales Reps** — work prioritized inbox, research accounts, draft outreach
- **Sales Managers** — build lists, create automated plays, track analytics
- **Admins** — manage team, roles, integrations (CRM, sales engagement platform)

**Tech**: FastAPI backend + React/TypeScript frontend, multi-tenant (Postgres RLS), 
RBAC (owner → admin → manager → rep). Runs fully offline in dev (SQLite + stub LLM).

---

## 🚀 Every New Task: Reference the Code-Review-Graph & Superpowers

**This project has comprehensive analysis ready. Before starting any work:**

1. **Use the code-review-graph** to understand scope and impact:
   ```bash
   code-review-graph search "YourSymbol"              # Find functions/classes
   code-review-graph detect-changes --brief           # See what changed (before review)
   ```
   Open the interactive visualization: `.code-review-graph/graph.html`

2. **Reference superpowers analysis** (generated in scratchpad):
   - **SUPERPOWERS-ANALYSIS.md** — Complete architecture deep-dive + code review methodology
   - **NEXUS-GTM-QUICK-START.md** — Quick commands and project structure
   - Use `/code-review` command — automatically uses graph for focused analysis

3. **Read CLAUDE.md, PRODUCT.md, DESIGN.md** — Grounding for every decision

4. **For architecture questions**: Check code-review-graph, then read relevant SUPERPOWERS-ANALYSIS section

**Why?** The graph cuts code-review token usage by 90% (only affected symbols, not full files).
Superpowers analysis summarizes all major systems so you don't re-derive architecture every task.

## 🛠️ Development Workflow (Using Code-Review-Graph & Superpowers)

### Adding a Feature

1. **Understand the system** (5 min):
   ```bash
   open .code-review-graph/graph.html    # Visualize architecture
   # Read: SUPERPOWERS-ANALYSIS.md (relevant section)
   # Read: PRODUCT.md (requirements)
   ```

2. **Find related code** (2 min):
   ```bash
   code-review-graph search "RelevantComponent"
   # or in Claude: /code-review medium
   ```

3. **Implement** — follow conventions below

4. **Review yourself** (2 min):
   ```bash
   /code-review    # Graph-assisted focused review
   ```

5. **Run tests**: `pytest tests/ -v`

### Fixing a Bug

1. **Reproduce & locate**:
   ```bash
   code-review-graph search "BuggyFunction"
   # Read SUPERPOWERS-ANALYSIS.md risk areas section
   ```

2. **Write a test** that fails

3. **Fix** — verify test passes

4. **Check for ripples**:
   ```bash
   /code-review    # Graph shows all callers/dependents
   ```

### Code Review (Every PR)

1. Use `/code-review` — automatically leverages graph
2. Read SUPERPOWERS-ANALYSIS.md (review checklist + risk areas)
3. Check the summary:
   - Is this security-critical? (multi-tenancy, auth, agents)
   - Does it touch data model? (migrations OK?)
   - Performance impact? (N+1 queries, timeouts?)

### Security & Architecture Review

**Before approving ANY change** to these areas, read the relevant section in SUPERPOWERS-ANALYSIS.md:
- Multi-tenancy (line ~150)
- Agent system (line ~250)
- API authentication (line ~350)
- Database migrations (line ~400)

---

## Repository layout

- `nexus/` — Python backend: FastAPI + async SQLAlchemy 2.0, Pydantic v2, multi-tenant
  (TenantSession + Postgres RLS), RBAC (owner > admin > manager > rep), in-process EventBus.
  Runs fully offline (SQLite + stub LLM + in-memory queue) for tests.
- `frontend/` — **the production React + TypeScript + Vite frontend** (the UI). `npm run build`
  emits the static bundle to `nexus/web/dist/`, which FastAPI serves with SPA fallback.
- `tests/` — pytest (`asyncio_mode=auto`). Keep the suite green.
- `docs/` — specs and design docs.

## Backend conventions

- Env config uses the `NEXUS_` prefix (pydantic-settings). Production must reject the insecure
  default JWT secret (enforced by a config validator).
- Reduce external dependencies. Prefer the standard library and what's already vendored.
- Every endpoint is tenant-scoped and RBAC-gated. Never bypass `TenantSession`.

## Relationship Graph (`nexus/network/`)

A tenant-scoped, deduped graph of each rep's real network (contacts + calendar), used for
NL "who do we know" search (A1) and warm-intro path mapping (A4). Additive subsystem — never
touches Account/Contact/Inbox/Cadence.

- **Ingestion is provider-based**: `nexus/network/connectors/` — `google.py` / `microsoft.py`
  are real OAuth connectors (Contacts + Calendar, incremental sync, token refresh);
  `fixture.py` is the **offline test double only**, never user-selectable in the product.
  LinkedIn has no compliant live API — it ingests via a member-supplied `Connections.csv`
  export (`nexus/network/linkedin_csv.py`).
- **OAuth security**: `nexus/network/oauth.py` (PKCE + signed short-TTL state JWT) and
  `nexus/network/crypto.py` (Fernet token-at-rest encryption, key derived from `secret_key`
  unless `NEXUS_NETWORK_TOKEN_ENC_KEY` is set). Tokens are never serialized to the client.
- **Resolution/scoring are deterministic, no LLM**: `resolution.py` (identity dedupe by
  normalized email, else name+company) and `strength.py` (0–100 connection strength from
  relationship tier + recency + frequency + reciprocity).
- **Privacy**: pooling is private-by-default per source account; `service.visible_edges_where`
  is the single predicate gating all cross-member reads (search, intro-paths, person lookup).
- API: `nexus/api/routers/network.py` (`/network/...`). Frontend: `frontend/src/pages/NetworkPage.tsx`.
- New provider credentials go in `NEXUS_NETWORK_*` env vars — inert (clear 400) until set,
  never a fake fallback.

## Migrations

Alembic under `migrations/versions/`. Head: `0026_billing_webhooks`. The chain is
`0020_baseline_schema` (a **frozen, literal-DDL squash** of the old 0001–0020) → `0021`–`0026`
(the Billing tables below). Every tenant-scoped table automatically gets RLS via
`scripts/apply_rls.py` on deploy — no manual policy work needed for new tables.

Migrations are **additive only**, and the chain **is** replayable onto an empty database —
`tests/test_migrations_replay.py` builds one from nothing but `alembic upgrade head` and diffs
the result against `Base.metadata`. Do not reintroduce a `create_all()` inside a revision: the
old `0001_initial` did that, so it materialized whatever models existed *at run time* rather
than a frozen historical schema, and the chain could never be replayed. Production still runs
`bootstrap_db.py` → `alembic upgrade head` → `apply_rls.py`.

### Two traps that only bite against real Postgres

The suite runs on SQLite, which has **no RLS**. Both of these pass every test and fail in prod:

- **A hand-built `TenantSession` must call `apply_rls(session, tenant_id)` first**, or writes are
  rejected. `tests/test_rls_binding_guard.py` is an AST guard against this.
- **Genuinely cross-tenant reads return ZERO ROWS, not an error**, under the app's RLS-bound
  role. Use `get_platform_sessionmaker()` (owner role) for those — the staff console and the
  payment webhook both need it — and only behind `require_platform_admin` or signature
  verification.

Also: `apply_rls` sets the tenant GUC **transaction-locally**, so a read after `commit()` has no
binding and silently returns nothing.

## Billing & Entitlements (`nexus/billing/`)

A commercial operating system: any capability can be priced, quota'd, or gated **without
touching application code**. Designed in `docs/billing/` (19 docs); milestone plans in
`docs/superpowers/plans/2026-07-28-billing-m*.md`.

- **One seam.** Application code calls `check_and_meter(ts, capability_id=...)` — or the
  ergonomic `metered()` context manager — and never mentions a plan or a price. Plans, quotas,
  and prices are rows, not branches.
- **Regression-proof by construction.** Unknown capability → allow. Tenant with no subscription
  → allow. Engine raises → allow. Default `NEXUS_BILLING_ENFORCEMENT=shadow` evaluates and
  records but never blocks; `off` is a full kill switch. Every pre-existing tenant maps to the
  `legacy-unlimited` plan.
- **Config, not constants.** `catalog.py` (capabilities), `plans.py` (plans + entitlements),
  `rates.py` (prices + COGS). Seeds run on startup and never overwrite a live value — once
  shipped, pricing belongs to Admin, not to a redeploy.
- **The margin floor is enforced, not aspirational.** `rates.validate_rate()` refuses any price
  below 50% gross margin unless finance records an explicit exception, and it runs on the seed
  itself so a bad price cannot reach the database.
- **Usage is an append-only event stream**; rollups are derived and rebuildable. Quota reads =
  period rollup + events still marked unrolled, so they stay exact between sweeps and degrade to
  slower — never to undercounting — if the rollup worker stops. Corrections are compensating
  negative rows, never deletes.
- **Platform admin is separate from tenant RBAC.** `require_platform_admin` (env allowlist or
  the `platform_admins` table) fails closed; no tenant role grants it. Every admin mutation is
  captured in `billing_audit_log` with before/after snapshots.
- **Money flows through one seam.** `metered()` → quota → credits → overage price → block.
  Credits are pre-paid, so rating deducts what a period's burns already covered — otherwise the
  customer pays twice for one overage. Collection is keyed by invoice id at the provider, so a
  retry can never double-charge. Dunning (`nexus/billing/dunning.py`) retries on a config
  schedule and escalates to `past_due`; it never silently voids a debt.
- **Payments are a provider seam** (`payments.py`): `noop` by default so the whole lifecycle runs
  offline; `stripe` is inert until keyed and raises rather than faking success. Webhooks verify
  an HMAC over the **raw** body, enforce a freshness window, and dedupe on the provider event id
  as a primary key.
- **Gauges are not counters.** `seat.member` resolves to live membership count; summing events
  would only ever climb, so a customer could never get back under a seat limit.
- Platform-global tables carry no `tenant_id` and no RLS policy: `billing_capabilities`,
  `billing_plans`, `billing_plan_entitlements`, `billing_rate_cards`, `billing_cost_rates`,
  `platform_admins`, `billing_audit_log`, `billing_webhook_events`. The audit and webhook tables
  deliberately name their tenant column `subject_tenant_id` so `apply_rls.py` — which enrolls any
  table having `tenant_id` — does not hide them from the operators who must read them.

## Frontend skills — USE THESE for any UI work

Four design/frontend skills are installed in `~/.claude/skills/`. Invoke them via the `Skill`
tool when designing or building UI. Pick by task:

| Skill | Invoke for | Notes |
|-------|-----------|-------|
| **impeccable** | Dashboards, product UI, app shells, components, forms, settings, onboarding, empty states, UX audits, design systems/tokens, responsive behavior, theming. | **Primary skill for NEXUS UI.** Reads `PRODUCT.md` / `DESIGN.md` for grounding. |
| **ui-ux-pro-max** | Style/visual direction (50+ styles, 161 palettes), and the 99 UX guidelines. Accessibility is priority #1: contrast ≥ 4.5:1, visible focus rings, full keyboard nav, ≥ 44×44px touch targets. | Use for palette/style decisions and UX-rule checks. |
| **framer-motion** | Motion, transitions, micro-interactions in React. Respect `prefers-reduced-motion`. | Animate transform/opacity; motion must communicate, not decorate. |
| **design-taste-frontend** (taste-skill) | Landing pages, marketing sites, portfolios **only** — NOT product/dashboard UI. | Rarely needed here; the app is product UI. |

Grounding docs for the design skills:
- `PRODUCT.md` — what NEXUS is, who uses it, the screens.
- `DESIGN.md` — the design system: tokens, type scale, components, accessibility rules.

## Frontend conventions (`frontend/`)

- Stack: React 18 + TypeScript (strict) + Vite. Curated deps only: `react`, `react-dom`,
  `react-router-dom`, `framer-motion`. No UI kit — we own our component library.
- Styling: CSS custom-property design tokens in `src/styles/tokens.css` + CSS Modules.
  No inline magic numbers; consume tokens (`var(--space-4)`, `var(--color-accent)`).
- Components: reusable primitives in `src/components/ui/` (typed props, `forwardRef`,
  loading/disabled/empty/error states). Screens compose primitives — never re-style ad hoc.
- Accessibility is non-negotiable: semantic HTML, labelled controls, keyboard support,
  visible focus, `aria-*` where needed, reduced-motion fallbacks.
- Every data view handles **loading (skeletons), empty, and error** states explicitly.
- Build output (`frontend/dist`) is served by FastAPI as static with SPA fallback.

When you touch UI, invoke `impeccable` (and `ui-ux-pro-max` for visual/UX decisions,
`framer-motion` for animation) before writing code, and follow `DESIGN.md`.

## 🔍 Code Navigation & Analysis — Code-Review-Graph & Superpowers

This repo is indexed by **code-review-graph** (a Tree-sitter knowledge graph in `.code-review-graph/`).
It is registered as an MCP server and cuts code-review token use by ~90% (returns scoped slices
instead of full files).

### How to Use Code-Review-Graph for Every Task

**Before implementing or reviewing:**
```bash
# Find where a function/class is defined and what calls it
code-review-graph search "TenantSession"

# See what changed (for code review context)
code-review-graph detect-changes --brief

# View the interactive graph (start here!)
open .code-review-graph/graph.html
# or on Windows:
start .code-review-graph/graph.html
```

**In Claude** (automatic):
- `/code-review` — automatically uses graph to focus on affected files only
- `/code-review medium` — medium effort (less token use, uses graph)
- `/code-review ultra` — deep review (multi-agent, still uses graph for context)

### Superpowers Analysis — Always Available

Three comprehensive guides were generated and are always available for reference:

| File | What It Contains | Use When |
|------|------------------|----------|
| **SUPERPOWERS-ANALYSIS.md** | Complete system architecture, all 6 subsystems, security review, code review methodology, risk areas | Understanding any part of the system; before major changes |
| **NEXUS-GTM-QUICK-START.md** | Commands, structure, environment setup, testing, Docker, code quality standards | Quick reference, local dev setup, running tests |
| **nexus-gtm-code-review-setup.md** | Graph statistics, initialization details, MCP config, entry points | Understanding graph setup; API entry points |

**Read these first** before implementing or reviewing — saves re-deriving architecture.

### Graph Maintenance

- **After pulling code**: `code-review-graph update` (incremental re-index)
- **After major refactoring**: `code-review-graph build` (full re-index)
- The `.code-review-graph/` directory is gitignored; never commit it.
- If MCP tools aren't visible, restart Claude to load the server.
