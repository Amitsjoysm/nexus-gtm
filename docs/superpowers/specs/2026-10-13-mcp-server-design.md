# NEXUS MCP server: design

Date: 2026-10-13. Branch: `feat/mcp-server`. Status: approved by the product owner in conversation
(sections 1-3 presented and accepted; "proceed to build").

## Goal

Let people use NEXUS directly from AI assistants (Claude on every surface, ChatGPT, Cursor,
VS Code, and the OpenAI, Groq and xAI APIs) through the Model Context Protocol, and list NEXUS
on the assistants' marketplaces: Anthropic's Connectors Directory and plugin directory, the ChatGPT
app directory, and the official MCP Registry.

It is folded into the existing FastAPI app. The standalone plan in `Documents\mcpserver` is not
used: most of it rebuilt identity, billing, Stripe and the LinkedIn sourcing NEXUS already has.

## Decisions (product owner, 2026-10-13)

| Question | Decision |
|---|---|
| Which apps connect in the first release | API keys AND OAuth, both in the first release |
| What an assistant may do | Read, research (spends credits), and writes. Never sending email or launching a campaign |
| Who may connect, and the price | Every member, every plan. No extra fee: actions cost the credits they cost in the app. Gated by a new `module.mcp` capability, on by default |
| How much one connection may do | Chosen per connection (Read / Research / Write), never more than the user's own role, re-checked on every call |
| How a tool works | Calls NEXUS's own `/api` endpoints inside the process, as the connected user |
| Protocol implementation | A small in-house implementation of the stateless subset; no `mcp` SDK at run time |
| Distribution | Plugins and connectors on the marketplaces above |

## Non-goals (first release)

- Sending email, launching or approving campaigns, deleting, archiving, merging, changing owners,
  billing, settings, members, integrations, admin, exports.
- Interactive UI inside the chat (Claude "MCP Apps", ChatGPT widgets), MCP resources and prompts,
  subscriptions, tasks, sampling, elicitation.
- A local (stdio) server. Everything is the one remote endpoint.

## 1. Architecture

New package `nexus/mcp/`:

| Unit | Responsibility | Depends on |
|---|---|---|
| `protocol.py` | JSON-RPC envelopes, protocol-version handling for both dialects (below), error codes | nothing in NEXUS |
| `schema.py` | Validates tool arguments against the small JSON Schema subset our tools use | nothing |
| `safety.py` | Cleans and caps text in tool output; the server instructions about untrusted text | nothing |
| `credentials.py` | Creates, hashes, looks up and revokes API keys and OAuth tokens; resolves a bearer to an `Actor` (user, workspace, live role, scopes, connection) with a 30 s cache | users, memberships, `mcp_connections`, `oauth_tokens` |
| `gateway.py` | Mints a 5-minute internal access token for the actor and calls `/api/...` through `httpx.ASGITransport` on the running app; turns HTTP answers into tool results | the app |
| `tools/` | The tool catalogue: name, title, description, input schema, scope, annotations, handler | `gateway` only |
| `oauth.py` | The authorization server: metadata, client resolution (CIMD, DCR, pre-registered), redirect matching, PKCE, codes, token issue, refresh rotation, revocation | `credentials` |
| `cimd.py` | Fetches and validates Client ID Metadata Documents through the existing SSRF guard | `sources.safety` |

Routers:

- `nexus/api/routers/mcp.py`, mounted at the ROOT (not `/api`): `POST /mcp` (GET/DELETE 405),
  `/.well-known/oauth-protected-resource[/mcp]`, `/.well-known/oauth-authorization-server`,
  `/.well-known/openai-apps-challenge`, `/oauth/register`, `/oauth/authorize`, `/oauth/token`,
  `/oauth/revoke`. Registered before the SPA catch-all.
- `nexus/api/routers/connections.py` at `/api/connections`: the AI apps page and the consent screen.

One call:

1. `POST /mcp` with `Authorization: Bearer nxk_...` (API key) or `nxo_...` (OAuth access token).
2. `Origin`, if present, must be ours or allowlisted (403 otherwise).
3. The bearer's SHA-256 is looked up in its own short database session, closed before anything else
   runs (holding it across the inner call reproduces SQLite's "database is locked").
4. The workspace's `module.mcp` entitlement is resolved; a switched-off or excluded module answers
   with a JSON-RPC error carrying the switch message.
5. Per-connection rate limit (`mcp_rate_limit_per_minute`, default 120), on the shared limiter.
6. JSON-RPC dispatch. `tools/call` checks the tool's scope against the connection, validates the
   arguments, and runs the handler.
7. The handler calls `/api/...` as the user, with a token carrying the user's LIVE role and token
   version, so RBAC, tenancy, metering, preflight, validation and messages are the app's own.
8. Output is cleaned, capped and returned as `structuredContent` plus a JSON text block.

## 2. Protocol

Supported versions: `2025-03-26`, `2025-06-18`, `2025-11-25` (handshake dialect) and `2026-07-28`
(stateless dialect). One transport: Streamable HTTP, POST only, JSON responses, no SSE, no
`Mcp-Session-Id`.

- **Dialect detection.** A request whose `params._meta["io.modelcontextprotocol/protocolVersion"]`
  is set is stateless at that version. Otherwise it is the handshake dialect, at the
  `MCP-Protocol-Version` header's version (absent = `2025-03-26`, per spec).
- **Handshake dialect:** `initialize` (negotiates: echo the client's version if supported, else our
  newest handshake version), `notifications/initialized` (202, no body), `ping`, `tools/list`,
  `tools/call`.
- **Stateless dialect:** `server/discover` (versions, capabilities, server info, instructions),
  `tools/list`, `tools/call`. Every result carries `resultType: "complete"` and
  `_meta["io.modelcontextprotocol/serverInfo"]`; `tools/list` carries `ttlMs` (300000) and
  `cacheScope: "private"`. An unsupported version answers `-32022` with the supported list. An
  `Mcp-Method` header that disagrees with the body answers `-32020`.
- Tools are listed in a fixed order. Only tools the connection's scopes allow are listed.
- A JSON array body (batch) answers `-32600`. Unknown method `-32601`. Bad arguments `-32602`.
- `instructions` (in `initialize` and `server/discover`) tell the model that text from the public
  web or from emails inside results is data and never instructions.

## 3. Credentials and sign-in

### Storage (migration `0068_mcp`)

All four tables are platform-global (no `tenant_id`; the workspace column is `subject_tenant_id`),
because a bearer is resolved before any workspace is known, the reason `user_mfa` has none. Every
read filters on `subject_tenant_id` (and the user, where it applies) in code.

- `mcp_connections`: one row per API key or OAuth grant. `kind` (`api_key` | `oauth`), `name`,
  `user_id`, `subject_tenant_id`, `client_id` (OAuth), `scopes` (JSON list of `read`, `research`,
  `write`), `key_prefix` (first 12 characters, for display), `key_digest` (SHA-256, unique, API keys
  only), `expires_at`, `last_used_at`, `revoked_at`, `revoked_by`.
- `oauth_clients`: `client_id` (unique; the URL for CIMD, random for DCR and pre-registered),
  `kind` (`cimd` | `dcr` | `registered`), `client_name`, `client_uri`, `logo_uri`, `redirect_uris`,
  `token_endpoint_auth_method`, `client_secret_digest`, `metadata_expires_at`, `last_used_at`.
- `oauth_codes`: `code_digest` (PK), client, user, workspace, `redirect_uri`, `scopes`,
  `code_challenge`, `resource`, `connection_id`, `expires_at` (60 s), `used_at`.
- `oauth_tokens`: `token_digest` (PK), `connection_id`, `kind` (`access` | `refresh`), `family_id`,
  `expires_at`, `used_at`, `revoked_at`.

### API keys

`nxk_` + 32 random bytes (URL-safe). Shown once; stored as SHA-256 (256 bits of entropy makes a
slow hash pointless). Name, scopes, expiry (30, 90, 365 days or never; default 90).

### OAuth 2.1 authorization server (NEXUS itself)

- **Client identification**, in the spec's priority order: pre-registered confidential clients
  (created by a superadmin with `providers.manage`, which also covers Anthropic-held credentials),
  Client ID Metadata Documents (an `https` URL with a path; fetched through the SSRF guard, 5 s
  timeout, 64 KB cap, `client_id` must equal the URL, cached by HTTP headers within 1 h to 24 h),
  and Dynamic Client Registration (`/oauth/register`, JSON, rate limited per IP, kept for older
  clients although the 2026-07-28 spec deprecates it).
- **Metadata** advertises `issuer`, the four endpoints, `response_types_supported: ["code"]`,
  `grant_types_supported: ["authorization_code", "refresh_token"]`,
  `code_challenge_methods_supported: ["S256"]`,
  `token_endpoint_auth_methods_supported: ["none", "client_secret_post", "client_secret_basic"]`,
  `client_id_metadata_document_supported: true`, `authorization_response_iss_parameter_supported:
  true`, `scopes_supported: ["nexus.read", "nexus.research", "nexus.write", "offline_access"]`.
  Protected-resource metadata: `resource` = `{base}/mcp` exactly, `authorization_servers` = [`base`],
  the same scopes, `bearer_methods_supported: ["header"]`. `base` is the App public address
  (`app_base_url`), else the request's own origin (local stacks).
- **401** from `/mcp`: `WWW-Authenticate: Bearer resource_metadata="{base}/.well-known/oauth-protected-resource",
  scope="nexus.read nexus.research nexus.write"`.
- **Redirect URIs**: exact string match, except loopback: `http://localhost` and `http://127.0.0.1`
  (and `[::1]`) match on any port. Registered URIs must be `https` or loopback `http`, without
  fragments. Claude's `https://claude.ai/api/mcp/auth_callback` needs nothing special.
- **Authorize** (`GET /oauth/authorize`): an unknown client or an unregistered `redirect_uri` is an
  error page, never a redirect. Otherwise `response_type=code`, `code_challenge` with S256 (required),
  scopes within ours (default all three), `resource` equal to ours when present. Valid requests go to
  the SPA consent page with a signed 10-minute request token (`typ="oauth_request"`, which
  `decode_access_token` already refuses). Errors after that point redirect with `error=` and `state`.
- **Consent** (`/oauth/consent`, signed in, MFA applies through login): the app's name, a
  **Verified** label only for pre-registered clients and CIMD documents served from known hosts
  (`claude.ai`, `chatgpt.com`, `openai.com`), **Unverified** otherwise, the redirect host in large
  type, an extra warning when every redirect is loopback, the workspace being connected, the signed-in
  email, permission checkboxes, and the notice that what the app reads is sent to that app's AI
  provider. Approve creates the connection and a 60-second single-use code; the redirect carries
  `code`, `state` and `iss`. Impersonation sessions cannot approve.
- **Token** (`POST /oauth/token`, form-encoded): authorization code with PKCE verification, exact
  `redirect_uri`, client authentication for confidential clients. Returns `access_token` (`nxo_`,
  1 h), `refresh_token` (`nxr_`, 30 days), `token_type: "Bearer"`, `expires_in`, `scope`. Refresh
  rotates; presenting a used refresh token revokes the whole family. Errors are RFC 6749 JSON,
  `invalid_grant` for any bad code or refresh token.
- **Revoke** (`POST /oauth/revoke`, RFC 7009): always 200.

### Actor resolution (every call, keys and OAuth alike)

Connection not revoked or expired → user not suspended → membership in `subject_tenant_id` exists →
LIVE role from that membership. Cached 30 s per bearer digest; revoking clears this process's cache
(other processes within 30 s, the same bound as session revocation). `last_used_at` is written at
most once a minute.

## 4. Tools

Scopes: `read` (readOnlyHint true), `research` (spends credits; readOnlyHint false, destructiveHint
false, openWorldHint true), `write` (creates or changes records; readOnlyHint false, destructiveHint
false). Every tool has a `title`. Ids are validated as `^[A-Za-z0-9_-]{1,64}$` before any path is
built. Descriptions name no internal vendor.

| Tool | Scope | Calls |
|---|---|---|
| `search` | read | `GET /api/accounts?q=` and `GET /api/contacts?q=`; typed ids `account:<id>`, `contact:<id>` |
| `fetch` | read | account: `GET /api/accounts/{id}` + recent signals + contacts; contact: workspace contact read; list: `GET /api/lists/{id}` + members |
| `list_signals` | read | `GET /api/signals` |
| `list_inbox` | read | `GET /api/inbox` |
| `list_lists` | read | `GET /api/lists` |
| `list_campaigns` | read | `GET /api/engagement/campaigns` |
| `get_credit_balance` | read | `GET /api/billing/credits` |
| `find_contacts` | research | `POST /api/accounts/{id}/source-contacts` (found people are saved, as in the app) |
| `find_similar_companies` | research | `POST /api/accounts/{id}/lookalikes` (candidates only, nothing saved) |
| `enrich_contact` | research | `POST /api/accounts/contacts/{id}/enrich` |
| `research_account` | research | `POST /api/agents/research/run` |
| `draft_email` | research | `POST /api/agents/messaging/run` (returns subject and body; nothing sent or saved) |
| `create_account` | write | `POST /api/accounts` |
| `create_list` | write | `POST /api/lists` |
| `add_to_list` | write | `POST /api/lists/{id}/members` |
| `add_contacts_to_campaign` | write | `POST /api/engagement/campaigns/{id}/contacts` (nothing is sent; drafts wait for review in the app) |
| `complete_inbox_task` | write | `POST /api/inbox/{id}/complete` |
| `save_email_draft` | write | `POST /api/contacts/{id}/save-draft` (into the user's mailbox Drafts; never sent) |

Research tool descriptions state their credit price, read from the rate cards when `tools/list` is
answered, never written down. Paid tools accept an optional `request_id`, forwarded as the inner
`Idempotency-Key`, so a retried call replays instead of charging twice.

## 5. Safety, errors, limits

- **Untrusted text**: strings are stripped of control and zero-width characters
  (`agents.copy.tidy_text`), each capped at 4,000 characters, the whole result at 200 KB (truncated
  with `truncated: true`). The server instructions name the third-party fields.
- **Errors** from the inner call become tool results with `isError: true` and the app's own
  message: 400/422 (what was wrong), 402 (not enough credits or not on the plan, with "Add credits in
  NEXUS > Billing"), 403, 404, 409, 429 (retry after), 5xx ("NEXUS could not finish this", with the
  request id). Protocol faults are JSON-RPC errors.
- **Audit**: every research and write call records `mcp.tool` in `audit_log` (tool, connection,
  ids only, outcome). Key creation, permission changes, OAuth grants and revocations are audited.
- **Metrics**: `nexus_mcp_requests_total{method,outcome}`, `nexus_mcp_tool_calls_total{tool,outcome}`,
  `nexus_oauth_events_total{event,outcome}`. Tool names are a fixed set, so the label is bounded.
- **Settings** (Runtime settings, Access & security): `mcp_rate_limit_per_minute` (120),
  `mcp_allowed_origins` (comma list of https origins), `openai_apps_challenge_token` (served as
  text at `/.well-known/openai-apps-challenge`; 404 when empty).

## 6. Screens

- **AI apps** (`/connected-apps`, every member, nav item with `module.mcp`, hideable page): the
  server URL with copy; how to connect each client (Claude, Claude Code, ChatGPT, Cursor, VS Code,
  OpenAI/Groq/xAI APIs); your connections (app or key name, kind, permissions, created, last used,
  expires; Edit permissions, Revoke); Create API key (name, permissions, expiry) with the secret shown
  once. Admins and owners also see Everyone's connections and can revoke them.
- **Consent** (`/oauth/consent`, outside the app shell): as in section 3. Signed-out users are sent
  to login and returned to the consent page afterwards.

## 7. Distribution (artifacts in this branch)

- `distribution/claude-plugin/nexus-gtm/`: `.claude-plugin/plugin.json`, `.mcp.json` (remote http
  server, OAuth handled by Claude), skills for daily triage, account research and outreach, and list
  building, a README with a privacy section. Published later as its own public GitHub repository.
- `distribution/mcp-registry/server.json`.
- `docs/mcp/setup-guide.md` (the public setup page) and `docs/mcp/listing-guide.md` (every step to
  list on each marketplace, including what is not code).

## 8. Testing

- Unit: both protocol dialects, the schema validator, id validation, output cleaning, redirect
  matching, PKCE, CIMD validation (fake fetcher), refresh rotation and reuse detection.
- Integration (ASGI client, offline): the API-key flow end to end; the OAuth flow end to end
  (register or CIMD, authorize, consent approve, token, `/mcp`, refresh, reuse, revoke); cross-
  workspace isolation; suspended and demoted users; revocation; `module.mcp` switched off; scope
  enforcement; every tool on the offline stack; 402 mapping with enforcement on; Origin refusal; the
  401 header; the metadata documents; the OpenAI challenge.
- Structural: every tool has a title and a readOnlyHint or destructiveHint; tool order is fixed; no
  vendor names in descriptions; handlers call only allowlisted `/api` paths; the route guards match
  the nav.
- Conformance: the official `mcp` client against our endpoint, in a test skipped unless the optional
  `mcp-conformance` extra is installed (kept out of CI's install so the pinned web stack is untouched).
- Live: the branch running locally, connected with the official client using an API key, and the
  OAuth flow driven through the consent page in a browser.

## Risks

- **Platform-global credential tables have no RLS safety net.** Mitigated by filtering in code and
  a cross-workspace test for every connections endpoint.
- **The spec moves.** The 2026-07-28 revision reshaped the protocol; the protocol module is
  version-tabled and isolated so a new revision is a contained change.
- **Open registration (DCR) can be abused.** Rate limited per IP, strict redirect rules, unused
  clients cost one small row.
- **Data leaves through the assistant.** Stated on the consent screen and in the privacy policy;
  scopes and revocation are per connection.
