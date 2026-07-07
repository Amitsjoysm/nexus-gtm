# NEXUS GTM — Project Guide for Claude

NEXUS GTM is an AI-powered Go-To-Market intelligence platform (a Pocus.com-class product):
multi-tenant SaaS that ingests buying signals, scores account relevance, runs AI agents
(research / messaging / enrichment), and drives a rep workflow (inbox, lists, plays, alerts).
All work is reviewed by Codex and must be production-grade — "built like it's going into a
real app used by millions."

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

Alembic under `migrations/versions/`. Latest: `0019_verification_icp_controls` (contact
verification timestamp + per-tenant ICP daily-discovery count) on top of
`0018_relationship_graph` (the Network tables above). Every tenant-scoped table automatically
gets RLS via `scripts/apply_rls.py` on deploy — no manual policy work needed for new tables.

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

## Code navigation — use code-review-graph to save tokens

This repo is indexed by **code-review-graph** (a Tree-sitter knowledge graph stored in the
gitignored `.code-review-graph/`). It is registered as an MCP server for this project. Prefer
its tools over reading whole files when you need to understand impact, find callers, or get
review context — they return scoped slices instead of full files, cutting token use by ~90%.

- **Before reviewing a change:** call the `get_review_context_tool` / `detect_changes_tool`
  MCP tools (or run `code-review-graph detect-changes --brief`) to get only the affected
  symbols and their blast radius, rather than re-reading the touched files end-to-end.
- **To trace impact / callers:** use `get_impact_radius_tool` and the graph's semantic search
  instead of grepping the whole tree.
- **Keep the graph fresh:** after pulling or making structural changes, run
  `code-review-graph update` (incremental) or `code-review-graph build` (full re-index).
  The index lives in `.code-review-graph/` and must never be committed (already gitignored).
- If the MCP tools aren't visible, the AI tool needs a restart to load the server; fall back to
  normal reads until then.
