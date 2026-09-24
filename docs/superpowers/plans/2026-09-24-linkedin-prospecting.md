# LinkedIn prospecting — implementation plan

> TDD per task (failing test, run, implement, run, commit). Spec:
> `docs/superpowers/specs/2026-09-24-linkedin-prospecting-design.md`.

**Goal:** ICP companies, similar companies and contacts come from the shared database first, then the
harvestapi LinkedIn actors, then Exa — with every company tied to a verified domain, credits charged
per company delivered, and discovery that keeps finding new companies at any workspace size.

## Phase 1 — ICP companies

| # | Task | Pinned by |
|---|---|---|
| 1 | Vendored industry codes, in-memory cache, `map_industries` (deterministic, then LLM shortlist) | `tests/test_linkedin_industries.py` |
| 2 | Migration 0061: LinkedIn fields on `companies`, `prospect_cursors`, `prospect_runs` | `tests/test_migrations_replay.py`, model tests |
| 3 | Actor registry + parsers + the domain gate, against the captured output's shape | `tests/test_linkedin_actors.py` |
| 4 | The company chain: database anti-join → actor pages from a cursor (store all, deliver the shortfall, split past 1,000) → Exa | `tests/test_prospect_chain.py` |
| 5 | ICP save maps industries; `POST/GET /discovery/populate`; job; preflight + per-company charge | `tests/test_prospect_populate.py` |
| 6 | The daily discovery sweep uses the chain | `tests/test_prospect_chain.py` |
| 7 | Relevance page: "How many companies now?" with cost, balance and progress | typecheck, build, source tests |

## Phase 2 — Find similar

| 8 | Account LinkedIn page (stored or found by name, domain-verified) → similar organisations → websites in one `linkedin-company` run → gate → dedupe → Exa fallback | `tests/test_prospect_similar.py` |

## Phase 3 — Contacts

| 9 | Employees actor on the verified page, ICP titles, short mode; row must prove company and title; dedupe; verified finder for email; Exa fallback | `tests/test_prospect_contacts.py` |
| 10 | CLAUDE.md | — |

Verification: each task's tests; ruff; frontend typecheck and build; the full suite; the running app.
