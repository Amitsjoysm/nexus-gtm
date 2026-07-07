# Relationship Graph (A1–A5) — Design

> **Status:** Approved design (brainstorming output). Next step: implementation plan via writing-plans.
> **Date:** 2026-06-29
> **Scope:** The Happenstance "relationship-first" half of the competitive brief — features A1
> (NL network search), A2 (multi-network aggregation), A3 (team pooling), A4 (warm-intro mapping),
> A5 (relationship-grounded profiling). The Gojiberry signal/automation half (B1–B6) already exists
> in NEXUS and is **out of scope** here.

## 0. Decisions locked in brainstorming

| Decision | Choice |
|---|---|
| Graph substrate | **Approach A** — relational source-of-truth (Postgres/SQLite) + per-tenant cached graph projection for ranking. No graph DB (keeps offline-SQLite + few-deps rules). |
| Integration realism | **Stub-first**: `NetworkConnector` interface + offline `FixtureConnector` so the whole subsystem runs in tests with zero external services. Real OAuth adapters are clean seams; reaching "one real adapter" then "all real" requires only filling `fetch()`/auth bodies. |
| Reference connector shape | **Google + Microsoft** (both are *mailbox + contacts + calendar via OAuth* — one interface covers both). LinkedIn is a later/harder adapter. |
| Privacy / pooling | **Private-by-default, opt-in pooling, always attributed.** A member's network is invisible to teammates until they enable pooling; pooled results always name the broker + strength. |

## 1. Goals & non-goals

**Goals**
- Turn each member's real relationship network into a searchable, deduped, tenant-scoped graph.
- Answer "**who do we already know** who matches X" (A1) and "**who on the team can broker the intro**, ranked by strength" (A4).
- Produce relationship-grounded person briefings (A5).
- Pool networks across a team with explicit consent + attribution (A3).
- Honor every NEXUS rule: multi-tenant by construction, runs fully offline, few external deps,
  everything swappable, async-first.

**Non-goals (v1)**
- Deep multi-hop (friend-of-friend beyond the team) traversal. In the team-pool model the warm-intro
  graph is effectively **1-hop** (target person → which *members* hold an edge); that is the value and
  it is tractable relationally. The edge model is general enough to add person→person edges later.
- Real LinkedIn/Instagram ingestion (ToS/scraping risk) — interface seam only.
- Slack integration and CSV export (excluded by the brief).
- Billing / shared-credit accounting.

## 2. System architecture

A new, **fully additive** subsystem `nexus/network/` alongside the existing account-intelligence
loop. It touches no existing schema or endpoint and reuses every platform primitive: `TenantSession`
+ Postgres RLS (isolation), the `TaskQueue` (sync jobs), `LLMProvider` (NL parse + profiling), and
`lookalike/similarity.py` (fuzzy dedup).

```
Member connects source ─▶ NetworkSourceAccount (OAuth seam; stub = instant "connected")
        │ enqueue sync_network_account
        ▼
NetworkConnector.fetch(cursor) ─▶ NetworkSyncBatch (identities + touchpoints + next_cursor)
        │
        ▼
Identity Resolution ─▶ upsert NetworkPerson  ◀── lookalike/similarity.py (fuzzy dedup)
        │              upsert NetworkIdentity (raw per-source record)
        ▼
Strength Scorer (deterministic 0–100) ─▶ materialize NetworkEdge.strength
        │
        ▼   reads, visibility-filtered:  owner_member_id == me  OR  pooling_enabled
   ┌────────────┬──────────────────┬─────────────────────┬───────────────┐
   ▼            ▼                  ▼                     ▼               ▼
NL Search    Intro-Path Map   Relationship Profile   Person 360     Team Stats
 (A1)           (A4)             (A5, cached)          (A5-lite)       (A3)
```

## 3. Component structure (`nexus/network/`)

| Module | Responsibility | Depends on |
|---|---|---|
| `connectors/base.py` | `NetworkConnector` Protocol + `NetworkSyncBatch`/`RawIdentity`/`Touchpoint` DTOs | — |
| `connectors/fixture.py` | Offline adapter: canned batches / accepts inline records (runs tests) | base |
| `connectors/google.py` | Real Google adapter **seam** (People + Gmail metadata + Calendar). Bodies = TODO. | base |
| `connectors/microsoft.py` | Real Microsoft Graph adapter **seam**. Bodies = TODO. | base |
| `connectors/registry.py` | `get_network_connector(provider)` (mirrors `ingestion.crm.get_crm_connector`) | all connectors |
| `resolution.py` | Identity resolution: email-exact → fuzzy(similarity) → create/merge; idempotent | `lookalike/similarity` |
| `strength.py` | Deterministic `score_edge(stats) -> int` | — |
| `search.py` | NL query → `NetworkQuery` (LLM/stub) → candidate fetch → rank | `agents` LLM, projection |
| `intro.py` | Warm-intro mapping: visible edges for a person, ranked + attributed | service |
| `profiling.py` | Relationship-briefing agent (LLM/stub) → cached `NetworkProfile` | `agents` runtime |
| `projection.py` | Per-tenant cached graph index (Redis prod / dict dev) + invalidation | cache abstraction |
| `service.py` | Orchestrates connect/sync/import; hosts the `visible_edges()` privacy helper | models, connectors |

Each module has one purpose, a typed interface, and is independently testable — keeping files focused
per the project's isolation/clarity guidance.

## 4. Database schema

All tables are `IdMixin + TimestampMixin + TenantScoped + Base` (string-UUID `id`, `created_at`,
`updated_at`, non-null `tenant_id`). New RLS policies mirror existing tenant tables in prod.

### 4.1 `network_source_accounts` — a member's connected provider account
| Column | Type | Notes |
|---|---|---|
| `member_id` | FK memberships.id | who owns this connection |
| `user_id` | FK users.id | denormalized for convenience |
| `provider` | str(16) | `google` / `microsoft` / `linkedin` / `fixture` |
| `external_account_id` | str(255) | provider account id / email |
| `display_email` | str(255) | |
| `status` | str(16) | `connected` / `syncing` / `error` / `disconnected` |
| `pooling_enabled` | bool, default **False** | opt-in team pooling (privacy) |
| `oauth` | JSON, default `{}` | **write-only**, encrypted-at-rest seam; **never serialized out** (like `tenants.email_settings.password`) |
| `sync_cursor` | str(255), null | provider delta token for incremental sync |
| `last_synced_at` | TZDateTime, null | |
| `last_error` | str(500), null | |

`UNIQUE(tenant_id, member_id, provider, external_account_id)` · `INDEX(tenant_id, member_id)`

### 4.2 `network_persons` — resolved, deduped person (dedupe anchor)
| Column | Type | Notes |
|---|---|---|
| `primary_email` | str(255), null | the dedupe anchor |
| `full_name` / `first_name` / `last_name` | str | |
| `title` / `company` / `company_domain` | str | |
| `location` / `country` | str | |
| `linkedin_url` / `twitter_handle` / `photo_url` | str, null | |
| `profile` | JSON | extra merged attributes |
| `search_text` | str | denormalized lowercased `name + title + company` |
| `identity_count` / `edge_count` | int | denormalized counters (avoid joins on hot reads) |

`INDEX(tenant_id, primary_email)` · `INDEX(tenant_id, company_domain)` ·
prod: GIN/trigram index on `search_text`; SQLite dev: `LIKE`.

### 4.3 `network_identities` — raw per-source record → resolved person
| Column | Type | Notes |
|---|---|---|
| `source_account_id` | FK network_source_accounts.id | |
| `person_id` | FK network_persons.id, null | null until resolved |
| `provider` / `external_id` | str | provider's contact id |
| `email` / `name` / `title` / `company` / `handle` | str, null | raw values |
| `raw` | JSON | full source payload |
| `resolution_key` | str | normalized email, else `hash(name|company)` |

`UNIQUE(tenant_id, source_account_id, external_id)` *(idempotent re-sync)* ·
`INDEX(tenant_id, resolution_key)` · `INDEX(tenant_id, person_id)`

### 4.4 `network_edges` — owner↔person relationship with **materialized** strength ★
| Column | Type | Notes |
|---|---|---|
| `owner_member_id` | FK memberships.id | who holds the relationship |
| `owner_user_id` | FK users.id | denormalized |
| `person_id` | FK network_persons.id | |
| `source_account_id` | FK network_source_accounts.id | provenance |
| `provider` | str(16) | |
| `relation` | str(16) | `contact` / `email` / `calendar` / `linkedin_1st` / `follower` |
| `strength` | int 0–100 | **materialized** at sync (deterministic) |
| `email_count` / `sent_count` / `received_count` | int | reciprocity inputs |
| `meeting_count` | int | calendar co-attendance |
| `first_touch_at` / `last_touch_at` | TZDateTime, null | |
| `mutual_count` | int, default 0 | optional |
| `pooling_enabled` | bool | mirrored from source account for fast visibility filter |

`UNIQUE(tenant_id, owner_member_id, person_id, provider)` ·
**`INDEX(tenant_id, person_id, pooling_enabled, strength)`** ← warm-intro hot path (single seek) ·
`INDEX(tenant_id, owner_member_id)`

### 4.5 `network_profiles` — cached AI briefing (TTL'd, like AgentRun output)
| Column | Type | Notes |
|---|---|---|
| `person_id` | FK network_persons.id | |
| `generated_by_member_id` | FK memberships.id | |
| `summary` | text | |
| `highlights` / `best_intro_path` | JSON | |
| `model` / `tokens` | str / int | audit |
| `stale_after` | TZDateTime | freshness TTL |

`INDEX(tenant_id, person_id)`

## 5. Key algorithms

### 5.1 Identity resolution (`resolution.py`, idempotent)
```
resolution_key = normalize(email) if email else sha1(normalize(name) + "|" + normalize(company))
match order:
  1. exact primary_email (tenant-scoped)               → reuse person
  2. fuzzy: candidates sharing company_domain / name-tokens,
     scored via lookalike/similarity.py >= THRESHOLD    → reuse best
  3. else                                               → create new person
```
Ties → create new (conservative; never bad-merge). Re-sync upserts identities by
`(source_account_id, external_id)` and re-points `person_id`. Manual merge/split is a later affordance.

### 5.2 Connection strength (`strength.py`, deterministic — mirrors `score_icp_fit`)
```python
def score_edge(s: EdgeStats) -> int:
    tier = {"linkedin_1st": 40, "contact": 30, "calendar": 25, "email": 20, "follower": 10}
    score = tier.get(s.relation, 15)
    days = age_days(s.last_touch_at)
    if   days is None:    pass
    elif days <= 30:      score += 30
    elif days <= 90:      score += 20
    elif days <= 365:     score += 10
    score += min(25, 2 * s.email_count + 5 * s.meeting_count)   # frequency
    if s.sent_count > 0 and s.received_count > 0: score += 15    # reciprocity
    return max(0, min(100, score))
```
No LLM. Materialized on the edge at sync; reads never recompute.

### 5.3 NL search (`search.py`, A1)
1. **Parse** NL query via `LLMProvider` into a structured `NetworkQuery{titles[], seniorities[],
   industries[], companies[], locations[], keywords[]}`. The `StubLLMProvider` returns a deterministic
   keyword/regex extraction so offline + tests work.
2. **Candidate fetch** — tenant-scoped query over `network_persons` filtered by `search_text` +
   attribute match, restricted to persons with ≥1 **visible** edge.
3. **Rank** — `relevance(attr_match 0..1) × best_visible_strength(0..100)`, descending.
4. **Return** person + `intro_paths` (top brokers) + `why`.

### 5.4 Warm-intro mapping (`intro.py`, A4)
`GET /network/people/{id}/intro-paths` → visible edges for the person ranked by `(strength desc,
last_touch_at desc)`, each `= {broker member name/email, relation, strength, last_touch_at, provider}`.
Attribution always present — "who can get me in, ranked."

### 5.5 Relationship profile (`profiling.py`, A5)
Assemble `RelationshipContext` (identities, visible edges, touchpoints, best path, mutuals) → feed the
profiling agent (LLM/stub deterministic) → narrative briefing → cache in `network_profiles` with
`stale_after`. GET returns cached if fresh; POST forces regeneration.

## 6. API design (`/network` router — tenant-scoped + RBAC, mirrors existing routers)

| Method | Path | Purpose | Permission |
|---|---|---|---|
| POST | `/network/accounts` | Connect a source (stub→instant; real→returns `oauth_url`) | own (any role) |
| GET | `/network/accounts` | My connected sources + status + pooling flags | own |
| PATCH | `/network/accounts/{id}` | Toggle `pooling_enabled` / disconnect | own |
| POST | `/network/accounts/{id}/sync` | Enqueue incremental sync | own |
| POST | `/network/accounts/{id}/import` | **Inline** contacts/touchpoints (offline + fixture; mirrors `/integrations/crm/sync`) | own |
| POST | `/network/search` | **A1** — NL query → ranked known people + intro paths | member |
| GET | `/network/people/{id}` | **A5-lite** — person + visible edges + touchpoints | member |
| GET | `/network/people/{id}/intro-paths` | **A4** — ranked brokers, attributed | member |
| POST | `/network/people/{id}/profile` | **A5** — AI relationship briefing (cached) | member |
| GET | `/network/stats` | **A3** — graph size, pooled coverage, per-member contribution | manager+ |

All list endpoints are paginated. OAuth tokens are never returned. Cross-member reads pass the
visibility predicate (§8).

## 7. Connector interface (the swappable seam)

```python
# connectors/base.py
class RawIdentity(BaseModel):
    external_id: str; email: str | None; name: str | None
    title: str | None; company: str | None; handle: str | None; raw: dict

class Touchpoint(BaseModel):
    person_external_id: str; kind: str  # "email_sent"|"email_received"|"meeting"
    at: datetime

class NetworkSyncBatch(BaseModel):
    identities: list[RawIdentity]; touchpoints: list[Touchpoint]; next_cursor: str | None

class NetworkConnector(Protocol):
    provider: str
    async def begin_auth(self, redirect_uri: str) -> AuthChallenge: ...   # real: OAuth URL
    async def complete_auth(self, code: str) -> OAuthTokens: ...          # real: token exchange
    async def fetch(self, account: SourceAccountRef, since: str | None) -> NetworkSyncBatch: ...
```
`FixtureConnector` returns canned batches (or the `/import` endpoint feeds inline records). The Google
and Microsoft adapters implement `begin_auth`/`complete_auth`/`fetch` — only those bodies are TODO;
schema, registry, and flow are unchanged. Reaching Q1 phase 2 ("one real adapter") and phase 3 ("all
real") therefore costs no schema/API change.

## 8. Privacy / pooling enforcement

- `pooling_enabled` defaults **False** on the source account and is **mirrored onto every edge** for a
  single-column visibility filter.
- One helper gates all cross-member reads:
  `visible_edges(ts, me) → WHERE owner_member_id == me OR pooling_enabled IS TRUE`.
  Used by search, intro-paths, profile, person-360, stats.
- Results always **attribute** the broker (name + strength). Disconnect / disable pooling → flag flip +
  projection-cache bust → edges hidden immediately.
- OAuth tokens write-only (never serialized). Tenant isolation already guaranteed by RLS +
  `TenantSession` — this adds the intra-tenant member-visibility layer on top.

## 9. Caching strategy

1. **Materialized strength** on edges — reads never score.
2. **Per-tenant graph projection** `network:graph:{tenant}` — compact people + visible-edge index for
   ranking; built lazily, busted on sync / pooling-toggle / disconnect. Redis prod, in-memory dict dev
   (same pattern as relevance-context caching). **Absent Redis → degrades to indexed SQL (Approach C)
   with no correctness change.**
3. **NL-parse cache** `network:parse:{hash(query)}` short TTL (parse shared across members).
4. **Profile cache** via `network_profiles.stale_after`.
5. **`search_text`** denormalized column → no per-query string building.

## 10. Scalability

- Tenant-partitioned; every hot path indexed. Warm-intro = one index seek on
  `(tenant_id, person_id, pooling_enabled, strength)`. Search = indexed candidate filter + bounded
  top-N in-memory rank.
- **Incremental sync** via `sync_cursor` delta tokens → O(changes), not O(graph), per run.
- **Idempotent upserts** (unique keys) → safe retries / exactly-once effect.
- Sync / resolve / score run on the existing `TaskQueue` (Redis prod) → stateless horizontal workers,
  sharded per source account; strength recompute is edge-local.
- Denormalized counters + mirrored `pooling_enabled` avoid joins on hot reads. All lists paginated.

## 11. Frontend (additive — no existing screen touched)

New nav item **Network**. New `NetworkPage`:
- **Connect sources** panel (provider buttons, status, pooling toggle).
- **NL search bar** → ranked people cards with intro-path chips ("via Alex · strong").
- **Person drawer**: relationship briefing + intro paths + touchpoints.
- **Team stats** (pooled coverage, per-member contribution) for managers.

Reuses existing `ui/` primitives (`DataTable`, `Card`, `Badge`, `EmptyState`, `Skeleton`, `Modal`)
with explicit loading/empty/error states. Build-time: invoke `impeccable` (+ `ui-ux-pro-max`) per
CLAUDE.md before writing UI.

## 12. Testing (offline: SQLite + in-memory queue + stub LLM)

- `test_network_resolution` — dedupe by email + fuzzy; idempotent re-sync.
- `test_network_strength` — deterministic strength scoring across tiers/recency/reciprocity.
- `test_network_search` — NL parse (stub) + ranking + visibility filter.
- `test_network_intro_paths` — ranked brokers, attribution, pooling on/off.
- `test_network_privacy` — non-pooled edges invisible to other members; cross-tenant impossible.
- `test_network_api` — connect → import (fixture) → search → intro → profile over HTTP.

## 13. Non-breaking guarantees & migration

- New package `nexus/network/`, new models `nexus/models/network.py`, new router `network.py`, new
  worker jobs (`sync_network_account`, `resolve_identities`, `recompute_strength`), one
  **additive-only** migration (new tables + new RLS policies). **Zero edits** to Contact / Account /
  Inbox / Cadence / Relevance schemas or endpoints.
- New nav item + page only. New `NEXUS_`-prefixed provider settings (client id/secret) are optional —
  absent → fixture/stub path.
- Optional later bridge: "Add network person to CRM as Contact" — purely additive action.

## 14. Phasing (for the implementation plan)

1. Models + migration + `TenantSession` wiring + `FixtureConnector` + `/import` + resolution + strength
   (offline core; full test coverage).
2. Search (NL-parse stub) + intro-paths + `visible_edges` + projection cache.
3. Profiling agent + profile cache.
4. Google + Microsoft OAuth connector seams (`begin_auth`/`complete_auth`/`fetch` skeletons +
   `NEXUS_` settings) — the "reach phase 2/3 easily" groundwork.
5. Frontend Network screen (invoke `impeccable`).
6. Team stats + RLS policies + production perf indexes.
