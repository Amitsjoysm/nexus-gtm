# NEXUS MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A remote MCP endpoint in the NEXUS app, reachable with per-user API keys or NEXUS's own
OAuth 2.1 server, exposing curated read / research / write tools that run through the existing
`/api` endpoints as the connected user, plus the screens and marketplace artifacts to list it.

**Architecture:** `nexus/mcp/` holds pure units (protocol, schema, safety), credential resolution,
an in-process gateway to `/api`, the tool catalogue and the OAuth server. Two routers: one at the
root (`/mcp`, `/oauth/*`, `/.well-known/*`) and `/api/connections` for the screens. Four
platform-global tables in migration `0068_mcp`. Spec:
`docs/superpowers/specs/2026-10-13-mcp-server-design.md`.

**Tech Stack:** FastAPI <0.116, async SQLAlchemy 2, Pydantic v2, python-jose (existing), httpx
`ASGITransport` (existing), React 18 + TypeScript + CSS Modules. No new runtime dependency.

**Execution note:** executed inline by the same agent that wrote the spec (the user asked for no
subagents). Steps give exact files, interfaces, and test assertions; code follows the conventions of
the files named. Every task ends with its tests green and a commit on `feat/mcp-server`.

---

## File map

| File | Responsibility |
|---|---|
| `nexus/core/config.py` (modify) | `mcp_rate_limit_per_minute`, `mcp_allowed_origins`, `openai_apps_challenge_token`, `oauth_access_token_ttl_s`, `oauth_refresh_token_ttl_days` |
| `nexus/runtime_config/catalog.py`, `service.py` (modify) | three Access & security settings and their validators |
| `nexus/billing/catalog.py` (modify) | `module.mcp`, default enabled, on no plan |
| `nexus/models/mcp.py` (create), `nexus/models/__init__.py` (modify) | `McpConnection`, `OAuthClient`, `OAuthCode`, `OAuthToken` |
| `migrations/versions/0068_mcp.py` (create) | the four tables |
| `nexus/mcp/__init__.py`, `protocol.py`, `schema.py`, `safety.py` (create) | pure units |
| `nexus/mcp/credentials.py` (create) | secrets, digests, `Actor`, resolution cache |
| `nexus/core/ratelimit.py` (modify) | `hit(key, window, limit)` public helper over the existing counters |
| `nexus/mcp/gateway.py` (create) | internal token + ASGI call + HTTP-to-tool-result mapping |
| `nexus/mcp/tools/__init__.py`, `registry.py`, `catalog.py` (create) | tool specs and handlers |
| `nexus/mcp/server.py` (create) | JSON-RPC dispatch for one authenticated request |
| `nexus/mcp/oauth.py`, `cimd.py` (create) | authorization server logic |
| `nexus/api/routers/mcp.py` (create) | root routes |
| `nexus/api/routers/connections.py` (create) | `/api/connections` |
| `nexus/api/routers/contacts.py` (modify) | `GET /api/contacts/{contact_id}` for `fetch` |
| `nexus/api/routers/__init__.py`, `nexus/main.py` (modify) | register routers (root router before the SPA mount) |
| `nexus/core/metrics.py` (modify) | `record_mcp_request`, `record_mcp_tool`, `record_oauth_event` |
| `frontend/src/lib/api.ts`, `types.ts` (modify) | connections client |
| `frontend/src/pages/ConnectedAppsPage.tsx` + `.module.css` (create) | AI apps page |
| `frontend/src/pages/OAuthConsentPage.tsx` + `.module.css` (create) | consent screen |
| `frontend/src/app/nav.tsx`, `App.tsx` (modify), `nexus/features/pages.py` (modify) | nav item, routes, hideable page, return-to after login |
| `distribution/claude-plugin/nexus-gtm/**`, `distribution/mcp-registry/server.json` (create) | marketplace artifacts |
| `docs/mcp/setup-guide.md`, `docs/mcp/listing-guide.md` (create), `CLAUDE.md` (modify) | docs |
| `tests/test_mcp_*.py` (create) | tests per task |

---

### Task 1: Settings, capability, models, migration

**Files:** config.py, runtime_config catalog/service, billing/catalog.py, models/mcp.py,
models/__init__.py, migrations/versions/0068_mcp.py. Test: `tests/test_mcp_storage.py`.

- [ ] **Step 1: Failing tests** in `tests/test_mcp_storage.py`:
  - `test_the_mcp_tables_carry_no_tenant_id`: for each of `mcp_connections`, `oauth_clients`,
    `oauth_codes`, `oauth_tokens`, `"tenant_id" not in Base.metadata.tables[t].columns` and
    `mcp_connections` has `subject_tenant_id`.
  - `test_module_mcp_is_enabled_by_default_and_on_no_plan`: `CAPABILITY_SEED` has `module.mcp`
    with `default_mode == "enabled"`; no plan in `plans.py` seeds an entitlement for it.
  - `test_the_three_settings_are_in_the_panel`: `catalog.BY_KEY` (or the module's lookup) holds
    `mcp_rate_limit_per_minute`, `mcp_allowed_origins`, `openai_apps_challenge_token` in group
    `ACCESS`.
  - `test_allowed_origins_validator`: `https://a.example.com, https://b.example.com` passes;
    `http://x.com`, `https://x.com/path`, `*` are refused (raises the validator's error type).
  - `test_challenge_token_validator`: letters, digits, `-`, `_`, `.` up to 200 chars pass; spaces,
    `<`, 201 chars are refused.
- [ ] **Step 2:** run `pytest tests/test_mcp_storage.py -n0 -q`; expect failures (no module/table).
- [ ] **Step 3: Implement.**
  - `config.py` fields with comments (int 120, str "", str "", int 3600, int 30).
  - Catalog specs in `ACCESS`: `mcp_rate_limit_per_minute` (int, 10..2000, effect "How many tool
    calls one connected AI app may make per minute.", risk low), `mcp_allowed_origins` (str, risk
    medium, warning that an origin listed here can call /mcp from a browser page with a user's key),
    `openai_apps_challenge_token` (str, effect "Served at /.well-known/openai-apps-challenge for the
    ChatGPT app directory's domain check.", risk low).
  - Validators `_validate_origin_list` (each entry `https://host[:port]`, no path, no `*`) and
    `_validate_challenge_token` (`^[A-Za-z0-9._-]{1,200}$` or empty) in `_VALIDATORS`.
  - `_cap("module.mcp", "module", "AI apps (MCP)", default_mode="enabled", description="Connect
    AI assistants to NEXUS through MCP.")` beside the other module gates.
  - `nexus/models/mcp.py` per spec section 3 (`IdMixin, TimestampMixin, Base`; `TZDateTime`;
    `JSON` for lists; `key_digest` `String(64)` unique nullable; `oauth_codes.code_digest` and
    `oauth_tokens.token_digest` `String(64)` primary keys; `oauth_tokens.connection_id` FK with
    index; `oauth_clients.client_id` `String(512)` unique). Docstring states why there is no
    `tenant_id`.
  - `0068_mcp.py`: `create_table` for each, literal columns (no `create_all`), `down_revision =
    "0067_stripe_manage"`, indexes on `subject_tenant_id`, `user_id`, `connection_id`.
- [ ] **Step 4:** run the new tests plus `tests/test_migrations_replay.py tests/test_runtime_control_plane.py
  tests/test_new_module_gates*.py -n0 -q` (whichever exist); all pass. (The "every catalog key is read
  outside config.py" guard passes once Task 6 reads them; if it fails here, finish Task 6 before
  committing this task's settings, or commit them together.)
- [ ] **Step 5:** commit `feat(mcp): storage, settings and the module.mcp gate`.

### Task 2: Pure units: protocol, schema, safety

**Files:** `nexus/mcp/{__init__,protocol,schema,safety}.py`. Tests: `tests/test_mcp_protocol_unit.py`.

Interfaces:

```python
# protocol.py
HANDSHAKE_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")   # newest first
STATELESS_VERSIONS = ("2026-07-28",)
SUPPORTED_VERSIONS = STATELESS_VERSIONS + HANDSHAKE_VERSIONS
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = (
    -32700, -32600, -32601, -32602, -32603)
HEADER_MISMATCH, UNSUPPORTED_VERSION = -32020, -32022

@dataclass(frozen=True)
class Request: id: str | int | None; method: str; params: dict; is_notification: bool

class ProtocolError(Exception):  # code, message, data
def parse(body: bytes) -> Request            # raises ProtocolError(PARSE_ERROR / INVALID_REQUEST)
def dialect(req: Request, header_version: str | None) -> tuple[str, str]
    # ("stateless", v) when params._meta carries the version; else ("handshake", header or "2025-03-26")
    # raises ProtocolError(UNSUPPORTED_VERSION, data={"supported": [...]}) for an unknown version
def negotiate(requested: str | None) -> str   # handshake initialize: echo if supported, else newest
def result(req_id, payload: dict, *, stateless: bool, server_info: dict) -> dict
    # adds resultType="complete" and _meta serverInfo when stateless
def error(req_id, code: int, message: str, data=None) -> dict
```

```python
# schema.py — the subset our tools use
class SchemaError(ValueError): ...
def validate(schema: dict, args: dict) -> dict   # returns args with defaults applied
# supports: type object/string/integer/number/boolean/array, properties, required,
# additionalProperties: false, enum, minimum, maximum, minLength, maxLength, pattern,
# items (one schema), maxItems, default
```

```python
# safety.py
INSTRUCTIONS: str        # names title/summary/snippet/body fields as untrusted data
MAX_FIELD_CHARS = 4000
MAX_RESULT_BYTES = 200_000
def clean(value): ...    # recursive; strings via agents.copy.tidy_text + control-char strip + cap
def cap_result(data: dict) -> dict   # if json size > MAX_RESULT_BYTES, trim the longest list and set truncated=True
```

- [ ] **Step 1: Failing tests** (unit, no app):
  - parse: `b"[]"` → INVALID_REQUEST (batch); `b"{"` → PARSE_ERROR; missing `method` →
    INVALID_REQUEST; no `id` → notification.
  - dialect: `_meta` version 2026-07-28 → stateless; header 2025-06-18 → handshake 2025-06-18; no
    header → handshake 2025-03-26; `_meta` version `2099-01-01` → UNSUPPORTED_VERSION with
    `supported` list.
  - negotiate: `2025-06-18` → same; `2024-01-01` → `2025-11-25`.
  - result: stateless adds `resultType` and `_meta[META_SERVER_INFO]`; handshake does not.
  - schema: required missing, wrong type, enum miss, pattern miss, maxLength, extra key with
    `additionalProperties: false`, default applied, integer bounds.
  - safety.clean: removes `​`, `\x07`; caps a 10,000-char string to 4,000 + `…`; leaves
    numbers/bools; cap_result marks `truncated`.
- [ ] **Step 2:** run, expect ImportError.
- [ ] **Step 3:** implement the three modules.
- [ ] **Step 4:** `pytest tests/test_mcp_protocol_unit.py -n0 -q` green.
- [ ] **Step 5:** commit `feat(mcp): protocol, argument schema and output safety units`.

### Task 3: Credentials and the per-connection limit

**Files:** `nexus/mcp/credentials.py`, `nexus/core/ratelimit.py`. Tests: `tests/test_mcp_credentials.py`.

Interfaces:

```python
SCOPES = ("read", "research", "write")
OAUTH_SCOPE = {"read": "nexus.read", "research": "nexus.research", "write": "nexus.write"}
KEY_PREFIX, ACCESS_PREFIX, REFRESH_PREFIX = "nxk_", "nxo_", "nxr_"

def new_secret(prefix: str) -> str            # prefix + secrets.token_urlsafe(32)
def digest(secret: str) -> str                # sha256 hex

@dataclass(frozen=True)
class Actor:
    user_id: str; tenant_id: str; role: str; token_version: int
    connection_id: str; kind: str; scopes: frozenset[str]; name: str

class CredentialError(Exception):  # reason: "invalid" | "expired" | "revoked" | "suspended" | "no_membership"

async def create_api_key(session, *, user_id, tenant_id, name, scopes, expires_in_days) -> tuple[McpConnection, str]
async def resolve(bearer: str) -> Actor       # own short session; 30 s cache by digest; raises CredentialError
def forget(connection_id: str | None = None)  # clear the cache (all, or entries of one connection)
async def touch(connection_id: str)           # last_used_at, at most once a minute per process
```

`ratelimit.hit(key: str, *, window: int, limit: int) -> int | None` returns retry-after seconds
when over, using `_shared_count` then `_local_exceeded` (same never-fail-closed rule).

- [ ] **Step 1: Failing tests** (using `client`, `signup`, members invite as other tests do):
  - a created key resolves to the creator, workspace, LIVE role and its scopes; the stored row
    holds only the digest and a 12-char prefix (the secret appears nowhere in the row).
  - a revoked key raises `revoked` once `forget()` is called; an expired key raises `expired`.
  - a suspended user's key raises `suspended`; after a role change (owner demotes a manager to rep)
    the next resolve (after `forget`) reports `rep`.
  - a key for a user removed from the workspace raises `no_membership`.
  - an unknown or malformed bearer raises `invalid`.
  - `ratelimit.hit` returns None for the first `limit` hits and a positive retry-after after.
- [ ] **Step 2:** run, fail. **Step 3:** implement (resolution in `get_sessionmaker()` session
  without tenant binding: the tables are platform-global and `users`/`memberships` lookups by
  primary key on the owner/app role work as the login path does; membership read MUST filter on
  `tenant_id == connection.subject_tenant_id`; on Postgres the membership read uses
  `apply_rls(session, subject_tenant_id)` first, because `memberships` is tenant-scoped).
- [ ] **Step 4:** green. **Step 5:** commit `feat(mcp): API keys, OAuth bearer resolution and the per-connection limit`.

### Task 4: Gateway

**File:** `nexus/mcp/gateway.py`. Test: `tests/test_mcp_gateway.py`.

```python
@dataclass
class ToolOutcome: ok: bool; data: object; message: str = ""; status: int = 200

class Gateway:
    def __init__(self, app, actor: Actor): ...
    async def get(self, path: str, params: dict | None = None) -> ToolOutcome
    async def post(self, path: str, json: dict | None = None, params: dict | None = None,
                   idempotency_key: str | None = None) -> ToolOutcome
```

- Path must start with `/api/` and match `_ALLOWED` (a tuple of compiled patterns, one per path the
  catalogue uses); anything else raises `ValueError` before a request is made.
- Token: `create_access_token(user_id=..., tenant_id=..., role=actor.role,
  token_version=actor.token_version)`; headers `Authorization`, `X-Request-ID` (new id),
  `User-Agent: nexus-mcp`, `Idempotency-Key` when given (prefixed with the connection id so two
  connections cannot collide). Timeout 150 s. Base URL `http://nexus.internal`.
- Mapping: 2xx → ok with JSON; 402 → message built from the payload's capability, balance and
  "Add credits in NEXUS > Billing, or ask an admin to change the plan."; 400/403/404/409/422 → the
  `detail` (422 lists field errors); 429 → "Too many requests; try again in N seconds."; 5xx →
  "NEXUS could not finish this. Reference: <request id>."

- [ ] **Step 1: Failing tests:** a real signed-up workspace; `Gateway(app, actor).get("/api/accounts")`
  returns ok list; `get("/api/admin/billing/overview")` raises ValueError (not allowlisted);
  `get("/api/accounts/does-not-exist")` → ok False, status 404, message mentions not found; a rep
  actor calling an admin-only allowlisted path returns 403 with the app's message.
- [ ] **Step 2-4:** fail, implement, green. **Step 5:** commit `feat(mcp): in-process gateway to the API as the connected user`.

### Task 5: Tool catalogue

**Files:** `nexus/mcp/tools/{__init__,registry,catalog}.py`, `nexus/api/routers/contacts.py`
(`GET /contacts/{contact_id}`). Tests: `tests/test_mcp_tools.py`.

```python
# registry.py
@dataclass(frozen=True)
class ToolSpec:
    name: str; title: str; description: str; scope: str   # read|research|write
    input_schema: dict; handler: Callable[[Gateway, dict], Awaitable[ToolOutcome]]
    price_capability: str | None = None      # research tools: rate card that prices them
    price_unit: str = ""                     # "per contact found", ...
    def annotations(self) -> dict            # title + readOnlyHint/destructiveHint/idempotentHint/openWorldHint by scope
TOOLS: tuple[ToolSpec, ...]                  # fixed order: read, research, write
def by_name(name) -> ToolSpec | None
async def describe(spec, prices: dict[str, float]) -> dict   # tools/list entry; appends the price sentence
async def load_prices() -> dict[str, float]  # active rate cards for price_capability ids (platform session, never raises)
```

Tools and handlers exactly as spec section 4. `fetch` parses `kind:id` (`account`, `contact`,
`list`), `search` returns `{"results": [{"id", "type", "title", "subtitle", "url"}]}` where `url`
is `{base}/accounts/{id}` or `{base}/contacts?focus={id}` and `base` is `app_base_url` (empty
string when unset, then url is omitted). Paid tools take optional `request_id` (pattern
`^[A-Za-z0-9_-]{8,64}$`).

`GET /api/contacts/{contact_id}`: `require(Permission.manage_accounts)`, reuses `_query_contacts`
filtered by id (add an `ids` parameter to it), 404 when absent or deleted.

- [ ] **Step 1: Failing tests:**
  - structural: every tool has a non-empty `title`, `annotations()` has `readOnlyHint` and, when
    not read-only, `destructiveHint`; names are unique, ≤ 64 chars, snake_case; order is read →
    research → write; descriptions contain none of `exa, apify, groq, firecrawl, serper, brave,
    reacher, harvestapi, anthropic, openai` (case-insensitive).
  - every handler's paths match the gateway allowlist (call each with a recording fake gateway and
    assert the recorded paths match `_ALLOWED`).
  - offline behaviour through a real gateway on a signed-up workspace: `create_account` then
    `search` finds it, `fetch account:<id>` returns account + `signals` + `contacts` keys,
    `create_list` + `add_to_list` + `list_lists`, `get_credit_balance` returns `balance`,
    `list_inbox` and `list_signals` return lists, `list_campaigns` returns a list,
    `draft_email` on a contact returns `subject` and `body` (stub LLM), `fetch contact:<id>` works,
    a bad id (`../x`) is a schema error, not a request.
  - `describe()` for `enrich_contact` includes "credits" and the rate card's number.
- [ ] **Step 2-4:** fail, implement, green. **Step 5:** commit `feat(mcp): the tool catalogue over the existing API`.

### Task 6: The `/mcp` endpoint

**Files:** `nexus/mcp/server.py`, `nexus/api/routers/mcp.py` (MCP part), `nexus/api/routers/__init__.py`,
`nexus/main.py`, `nexus/core/metrics.py`. Tests: `tests/test_mcp_endpoint.py`.

Route behaviour:
- `POST /mcp`: Origin check (`_origin_allowed`: absent, or equal to `app_base_url` origin, or in
  `mcp_allowed_origins`; local/test also accepts `http://localhost:*` and `http://127.0.0.1:*`);
  bearer from `Authorization` (never a query string); missing/invalid → 401 with the spec's
  `WWW-Authenticate`; `credentials.resolve`; `module.mcp` via `resolve_entitlement` in a briefly
  opened TenantSession (`mode == "disabled"` and enforcement/switch rules as `_apply` uses:
  switched off always blocks; plan exclusion blocks only when enforcement is `on`) → JSON-RPC error
  `-32001` "AI apps are switched off for this workspace: <message>"; `ratelimit.hit("mcp:"+connection_id, window=60,
  limit=settings.mcp_rate_limit_per_minute)` → HTTP 429 with Retry-After; then `server.handle(app, actor, req, dialect)`.
- Notifications → 202 empty. GET/DELETE `/mcp` → 405 `Allow: POST`.
- `server.handle`: `initialize` (handshake only) → `{protocolVersion, capabilities: {tools: {listChanged: false}},
  serverInfo: {name: "nexus-gtm", title: "NEXUS GTM", version}, instructions}`; `server/discover`
  → `{supportedVersions, capabilities, serverInfo, instructions}`; `ping` → `{}`; `tools/list` →
  tools allowed by `actor.scopes` (+ `ttlMs`, `cacheScope` when stateless); `tools/call` → scope
  check (isError result naming the missing permission), `schema.validate` (INVALID_PARAMS),
  handler, `safety.clean` + `cap_result`, audit for research/write via `record_audit` in a short
  TenantSession (action `mcp.tool`), metrics.
- Result content: `{"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent":
  data, "isError": bool}` (structuredContent always an object: lists wrapped as `{"items": [...]}`).

- [ ] **Step 1: Failing tests** (raw JSON-RPC through the ASGI client):
  - no bearer → 401, header starts `Bearer resource_metadata="http://test/.well-known/oauth-protected-resource"`.
  - handshake: initialize with `2025-06-18` echoes it, `instructions` present; `tools/list` lists 18
    tools for a key with all scopes, 7 for read-only; `notifications/initialized` → 202.
  - stateless: `server/discover`; `tools/list` with `_meta` version carries `resultType`, `ttlMs`,
    `cacheScope`; unsupported version → `-32022`.
  - `tools/call create_account` with a read-only key → `isError` naming Write; with write → ok and
    the account exists via `/api/accounts`; audit row `mcp.tool` recorded.
  - bad arguments → `-32602`; unknown tool → `-32602`; batch → `-32600`; GET → 405.
  - Origin `https://evil.example` → 403; no Origin → fine.
  - `module.mcp` switched off (feature switch row) → error `-32001` with the message.
  - rate limit: with the setting at 2, the third call → 429.
  - a key from workspace A never sees workspace B's accounts (B seeded with an account; A's search
    returns none of B's).
  - 402: enforcement `on`, a Free workspace with 0 balance, `enrich_contact` → `isError` and
    "credits" in the text.
- [ ] **Step 2-4:** fail, implement, green (also re-run `tests/test_runtime_control_plane.py`).
- [ ] **Step 5:** commit `feat(mcp): the /mcp endpoint, both protocol dialects`.

### Task 7: Connections API

**File:** `nexus/api/routers/connections.py`. Tests: `tests/test_mcp_connections_api.py`.

- `GET /api/connections?view=mine|team` (`team` needs `manage_workspace`): rows `{id, kind, name,
  client_name, client_host, verified, scopes, key_prefix, created_at, last_used_at, expires_at,
  owner_name, owner_email, is_mine}`, revoked rows excluded, filtered on `subject_tenant_id` (and
  user for `mine`).
- `POST /api/connections/keys` (`require_writable`, `manage_accounts`): `{name 1..80, scopes
  non-empty subset, expires_in_days in (30, 90, 365, null)}` → `{connection, secret, server_url}`;
  audited `mcp.key_created`.
- `PATCH /api/connections/{id}`: own connection only; `{name?, scopes?}`; `forget(id)`; audited.
- `DELETE /api/connections/{id}`: own, or anyone's with `manage_workspace`; sets `revoked_at`,
  `revoked_by`, revokes its OAuth tokens, `forget(id)`; audited `mcp.connection_revoked`.
- `GET /api/connections/setup`: `{server_url, public_address_set, tools: [{name, title, scope,
  description}]}`.
- Endpoints sit under `module.mcp`'s server check (a module-gate dependency in the same shape the
  other gated routers use).

- [ ] **Step 1: Failing tests:** create returns the secret once and a later GET never contains it;
  a rep sees only their own; `team` view is 403 for a rep and lists everyone for an admin; a rep
  cannot revoke a colleague's (404, not 403, so ids don't leak); an admin can; workspace B's admin
  gets 404 for A's connection id and never sees it in `team`; PATCH narrows scopes and the next
  `/mcp tools/list` reflects it; revoke makes the next `/mcp` call 401.
- [ ] **Step 2-4:** fail, implement, green. **Step 5:** commit `feat(mcp): connections API for keys and connected apps`.

### Task 8: OAuth authorization server

**Files:** `nexus/mcp/oauth.py`, `nexus/mcp/cimd.py`, `nexus/api/routers/mcp.py` (OAuth +
well-known routes), `nexus/api/routers/connections.py` (consent routes). Tests:
`tests/test_mcp_oauth.py`.

Key rules (spec section 3):

```python
def redirect_matches(registered: list[str], presented: str) -> bool
    # exact, or loopback host (localhost, 127.0.0.1, [::1]) with http scheme, same path, any port
def valid_redirect(uri: str) -> bool          # https, or http loopback; no fragment; absolute
def pkce_ok(verifier: str, challenge: str) -> bool   # base64url(sha256(verifier)) without '=' == challenge; verifier 43..128 chars
def base_url(request) -> str                  # app_base_url, else request origin
async def resolve_client(client_id) -> OAuthClient   # registered/dcr row, or CIMD fetch+upsert for https URL ids
def sign_request(params: dict) -> str / read_request(token) -> dict   # typ "oauth_request", 600 s
async def approve(user, tenant_id, request_params, scopes) -> str     # creates connection + code; returns redirect URL with code, state, iss
async def exchange_code(form) -> dict / refresh(form) -> dict / revoke(token) -> None
```

`cimd.fetch(url)`: `https` only, path required, `_is_blocked_host` (no private unless local env),
`httpx` with `follow_redirects=False`, 5 s timeout, 64 KB cap, JSON object with `client_id ==
url`, `client_name`, `redirect_uris` (all valid) else `CimdError`; cache horizon from
`Cache-Control: max-age` clamped 3600..86400.

Verified label: `kind == "registered"` or CIMD host in `("claude.ai", "chatgpt.com",
"openai.com")` or a subdomain of those.

Routes: `GET /.well-known/oauth-protected-resource`, `/.well-known/oauth-protected-resource/mcp`,
`/.well-known/oauth-authorization-server`, `/.well-known/openai-apps-challenge`,
`POST /oauth/register` (JSON; `rate_limit("oauth_register")`), `GET /oauth/authorize`,
`POST /oauth/token` (form), `POST /oauth/revoke` (form). Consent API:
`GET /api/connections/oauth/request?request=` (display data), `POST /api/connections/oauth/approve`
`{request, scopes}` → `{redirect_to}`, `POST /api/connections/oauth/deny` `{request}` →
`{redirect_to}`; both `require_writable`. Admin: `POST /api/admin/oauth-clients` (pre-registered
confidential client; `providers.manage`) → `{client_id, client_secret}` once.

- [ ] **Step 1: Failing tests:**
  - metadata documents carry every field in the spec; `resource == base + "/mcp"`.
  - DCR: valid registration returns `client_id`; `http://example.com/cb` redirect refused;
    `https://a/cb#x` refused; loopback accepted.
  - authorize: unknown client → 400 page (no redirect); unregistered redirect → 400; missing PKCE →
    redirect with `error=invalid_request`; valid → 302 to `/oauth/consent?request=`.
  - full flow: DCR → authorize → consent approve as the signed-in user with scopes
    `["read"]` → redirect has `code`, `state`, `iss` → token exchange with the verifier → access
    token works on `/mcp` and lists only read tools → refresh returns new pair → old refresh again
    → `invalid_grant` and the new access token now fails (family revoked).
  - wrong verifier → `invalid_grant`; code reused → `invalid_grant`; redirect mismatch at token →
    `invalid_grant`; expired code (monkeypatched clock) → `invalid_grant`.
  - CIMD with a fake fetcher (monkeypatch `cimd._get`): valid document creates a `cimd` client;
    mismatched `client_id` refused; loopback redirect with a different port matches.
  - consent approve under an impersonation token → 403.
  - revoke endpoint returns 200 and the token stops working.
  - openai challenge: 404 when unset, the token as `text/plain` when set.
- [ ] **Step 2-4:** fail, implement, green. **Step 5:** commit `feat(mcp): OAuth 2.1 authorization server with CIMD, DCR and consent`.

### Task 9: Frontend

**Files:** api.ts, types.ts, ConnectedAppsPage(.tsx/.module.css), OAuthConsentPage(.tsx/.module.css),
nav.tsx, App.tsx, `nexus/features/pages.py`. Tests: `tests/test_mcp_ui.py` (source-reading, as the
other UI tests do).

- [ ] **Step 0:** invoke `impeccable` (product register) and read `DESIGN.md`; reuse `PageHeader`,
  `Card`, `DataTable`, `Button`, `Modal`, `Checkbox`, `Badge`, `EmptyState`, `Skeleton`, `ErrorState`,
  `Toast`.
- [ ] **Step 1: Failing tests:** nav has `{ to: "/connected-apps", label: "AI apps", capability:
  "module.mcp" }`; `HIDEABLE_PAGES` contains `/connected-apps`; the route is wrapped in
  `RequirePage` and `RequireCapability capability="module.mcp"`; `/oauth/consent` is a route
  outside `AppShell` wrapped so signed-out users are sent to login with the return path stored; the
  page never renders a stored secret (only the create response); the consent page shows the redirect
  host and "Unverified" copy; api.ts has `createMcpKey`, `listConnections`, `revokeConnection`,
  `updateConnection`, `oauthRequest`, `oauthApprove`, `oauthDeny`.
- [ ] **Step 2-4:** fail, implement, `npm run typecheck && npm run build` in `frontend/`, green.
- [ ] **Step 5:** verify in the browser pane (dev server) and screenshot both pages; commit
  `feat(mcp): AI apps page and OAuth consent screen`.

### Task 10: Distribution artifacts and docs

**Files:** `distribution/claude-plugin/nexus-gtm/.claude-plugin/plugin.json`, `.mcp.json`,
`skills/{triage-inbox,research-and-outreach,build-a-list}/SKILL.md`, `README.md`, `LICENSE`;
`distribution/mcp-registry/server.json`; `docs/mcp/setup-guide.md`; `docs/mcp/listing-guide.md`;
`CLAUDE.md` section "MCP server (`nexus/mcp/`)". Test: `tests/test_mcp_distribution.py`.

- [ ] **Step 1: Failing tests:** `plugin.json` parses with `name`, `version`, `description`;
  `.mcp.json` names one server of type `http` whose url ends `/mcp`; every SKILL.md starts with
  front matter holding `name` and `description` and mentions only tools in `TOOLS`; `server.json`
  parses and its remote URL ends `/mcp`; no file in `distribution/` names an internal vendor.
- [ ] **Step 2-4:** write the files; green; `claude plugin validate` if the CLI is available
  (record the output; skip if not installed).
- [ ] **Step 5:** commit `docs(mcp): plugin, registry entry, setup and listing guides`.

### Task 11: Conformance and full verification

- [ ] **Step 1:** `tests/test_mcp_conformance.py`: `pytest.importorskip("mcp")`; run the official
  client over streamable HTTP against a uvicorn server started on a free port in a thread (offline
  settings), with an API key header: initialize, list tools, call `get_credit_balance`. Add
  `mcp-conformance = ["mcp>=2.3,<3"]` to optional dependencies (not installed by CI).
- [ ] **Step 2:** run it locally in a scratch venv with the extra installed; record the result.
- [ ] **Step 3:** live: run the branch on a local preview server; create a key in the UI; connect
  with the official client; drive the OAuth flow through the consent page in the browser pane with
  a generated test account; screenshot.
- [ ] **Step 4:** full CI-equivalent run on the branch head (python:3.11-slim, unconstrained
  `.[dev,migrate,metrics]`, `-n 6`, signal timeout, coverage ≥ 60; integration job; node:20 build;
  gitleaks 8.24.3 over `master..HEAD`). Fix anything red.
- [ ] **Step 5:** commit fixes; write the final listing guide summary for the user.

---

## Self-review

- Spec coverage: protocol (T2, T6), storage (T1), keys (T3, T7), OAuth incl. CIMD/DCR/pre-registered,
  consent, metadata, challenge (T8), tools (T5), safety/errors/audit/metrics (T2, T4, T6), settings
  (T1), screens (T9), distribution and docs (T10), testing incl. conformance and live (all, T11).
- Interfaces used across tasks: `Actor`, `credentials.resolve/forget`, `Gateway.get/post`,
  `ToolOutcome`, `ToolSpec`, `TOOLS`, `protocol.parse/dialect/result/error` — names consistent.
