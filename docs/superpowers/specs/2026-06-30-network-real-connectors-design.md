# Network — Real Connectors (Production) Design

> **Status:** Approved design (brainstorming output). Next: implementation plan via writing-plans.
> **Date:** 2026-06-30
> **Supersedes the demo/stub-only data path of** [the Phase 1–2 core](2026-06-29-relationship-graph-design.md).
> **Goal:** Replace the demo/sample scaffolding with **real** data ingestion — live Google (and
> Microsoft) OAuth sync + LinkedIn data-export upload — and make the connector layer
> production-ready. **No product mocks.** `FixtureConnector` survives only as the offline test seam.

## 0. Decisions locked in brainstorming

| Decision | Choice |
|---|---|
| Real sources this build | **Google** (live OAuth) + **Microsoft** (live OAuth, same base) + **LinkedIn** (official data-export CSV upload — no live API is possible/legal). |
| Google scope | **Contacts + Calendar** (both Google "sensitive" scopes → standard OAuth verification, no Gmail/CASA restricted-scope audit). Gmail deferred. |
| Mocks | **Removed from the product.** No `SAMPLE_IDENTITIES`, no "Import sample", no "Demo network" provider in the UI. `FixtureConnector` is test-only (not user-selectable). |
| New dependencies | **None.** `cryptography` (token encryption) ships transitively via `python-jose[cryptography]`; `httpx` + `python-multipart` are already core deps. |

## 1. Goals & non-goals

**Goals**
- Connecting Google/Microsoft runs a real OAuth consent flow and **fetches the member's real
  contacts + calendar** into the graph.
- LinkedIn connections enter via the member's **official LinkedIn data export** (`Connections.csv`).
- Production-grade: OAuth `state`+PKCE, **encrypted token storage at rest**, token refresh,
  least scopes, bounded retry/backoff, incremental delta sync, real error surfacing, no fake data.
- Offline test suite stays green (mock HTTP at the adapter boundary; fixture for the rest).

**Non-goals (this build)**
- Gmail ingestion (restricted scope / CASA audit) — explicit later phase.
- A live LinkedIn API (none exists within ToS).
- AI relationship profiling (A5), team-stats dashboard, Redis projection cache — separate later work.
- Provisioning the Google Cloud / Azure OAuth apps themselves (operator task; the code consumes
  `NEXUS_`-env credentials and is inert until they are set).

## 2. New settings (`nexus/core/config.py`, `NEXUS_` env — empty default, inert until set)

| Setting | Purpose |
|---|---|
| `network_google_client_id` / `network_google_client_secret` | Google OAuth app credentials |
| `network_microsoft_client_id` / `network_microsoft_client_secret` | Azure AD app credentials |
| `network_microsoft_tenant` (default `common`) | Azure tenant (`common` = multi-tenant consumer+work) |
| `network_oauth_redirect_base` (e.g. `https://localhost`) | base for the OAuth callback URL |
| `network_token_enc_key` (optional) | Fernet key override; **defaults to a key derived from `secret_key`** so tokens are always encrypted with no extra required secret |

A `model_validator` rejects a **prod** deploy that sets a provider's `client_id` without a
`secret_key` strong enough to derive an encryption key (reuses the existing insecure-secret guard).
A provider with no `client_id` → `/oauth/{provider}/start` returns `400 provider_not_configured`
(no silent fallback to fake data).

## 3. Architecture

```
Connect Google/Microsoft
  └─ GET /network/oauth/{provider}/start ─▶ signed state(JWT: member, provider, PKCE) + consent URL
        │  user consents at provider
        ▼
  GET /network/oauth/{provider}/callback?code&state
        ├─ verify state(JWT) → exchange code→tokens (PKCE) ─▶ encrypt(refresh+access) at rest
        ├─ upsert NetworkSourceAccount(provider, display_email, oauth=enc, status=connected)
        └─ enqueue sync_network_account ─▶ (existing ingest path)

sync_network_account ─▶ get_network_connector(provider).fetch(account, cursor)
  GoogleConnector.fetch:    People API contacts + Calendar events(attendees) → RawIdentity+Touchpoint
  MicrosoftConnector.fetch: Graph /me/contacts + /me/events(attendees)       → RawIdentity+Touchpoint
        │  refresh token if expired; persist new sync_cursor (+ rotated tokens)
        ▼
  ingest_batch (unchanged: resolve → strength → edges)

LinkedIn (no live API):
  POST /network/accounts/{id}/import-linkedin  (multipart Connections.csv)
        └─ parse → RawIdentity[] → ingest_batch
```

## 4. Components

| Module | Responsibility |
|---|---|
| `nexus/network/crypto.py` | `encrypt_tokens(dict)->str` / `decrypt_tokens(str)->dict` via Fernet; key from `network_token_enc_key` or derived (`HKDF/SHA256(secret_key)` → urlsafe-b64 32B). Pure, unit-tested. |
| `nexus/network/oauth.py` | OAuth state (sign/verify a short-TTL JWT carrying member_id+provider+PKCE verifier, via existing `python-jose`), PKCE helpers, redirect-URI builder. |
| `nexus/network/connectors/base.py` | (existing) Protocol + DTOs — unchanged. |
| `nexus/network/connectors/oauthbase.py` | `OAuthConnector` mixin: `begin_auth` (authorize URL), `complete_auth` (token exchange), `_ensure_fresh_token` (refresh), shared httpx client w/ timeouts + 429/5xx backoff. |
| `nexus/network/connectors/google.py` | Real Google adapter: People API `connections` (paged, `syncToken`) + Calendar `events` (attendees → touchpoints). Scope/endpoint constants. |
| `nexus/network/connectors/microsoft.py` | Real Microsoft Graph adapter: `/me/contacts` + `/me/events` (delta). |
| `nexus/network/connectors/fixture.py` | (existing) **test-only**; stays in the registry for the suite, not exposed in the UI. |
| `nexus/network/connectors/registry.py` | Maps `google`/`microsoft`→real adapters, `fixture`→FixtureConnector. `linkedin` has **no** fetch connector (import-only). |
| `nexus/network/linkedin_csv.py` | Parse a LinkedIn `Connections.csv` export → `RawIdentity[]` (First/Last Name, Company, Position, URL, Connected On). Tolerant of LinkedIn's notes preamble. |
| `nexus/api/routers/network.py` | + `/oauth/{provider}/start`, `/oauth/{provider}/callback`, `/accounts/{id}/import-linkedin`. `POST /accounts` restricted to `linkedin` (manual) + (test) `fixture`; OAuth providers must use the flow. |

## 5. Connector detail (Google, reference)

- **Auth:** authorization-code + PKCE. Scopes: `contacts.readonly`, `calendar.readonly`,
  `userinfo.email` (for `display_email`), `openid`. `access_type=offline`, `prompt=consent` to get a
  refresh token.
- **Contacts:** `GET people/v1/people/me/connections` (`personFields=names,emailAddresses,
  organizations,metadata`), paged via `pageToken`; persist `nextSyncToken` as the account
  `sync_cursor` → next run sends `syncToken` for an incremental delta.
- **Calendar → touchpoints:** `GET calendar/v3/calendars/primary/events` since the last sync;
  each attendee email (≠ the member) yields a `meeting` `Touchpoint` at the event start, plus a
  `RawIdentity` (relation `calendar`) so calendar-only people still enter the graph.
- **Mapping:** People/Graph fields → existing `RawIdentity` (email/name/title/company) and
  `Touchpoint`. Strength scoring + resolution are **unchanged** (already production-grade).
- **Refresh:** when the access token is expired, exchange the refresh token; persist the rotated
  bundle (re-encrypted). A revoked/again-expired refresh token → `status=error`, `last_error` set,
  surfaced in the UI with a "Reconnect" affordance.

Microsoft mirrors this against Graph (`/me/contacts`, `/me/events`, `@odata.deltaLink`).

## 6. Security / production-readiness (the checklist this build must satisfy)

- **Token encryption at rest** (Fernet); plaintext tokens never touch the DB or any response
  (`NetworkAccountOut` already omits `oauth`).
- **OAuth `state` (signed, expiring) + PKCE** — CSRF + auth-code-interception defense.
- **Least scopes** (Contacts + Calendar read-only); incremental sync (delta tokens), not full re-pull.
- **httpx** clients: explicit timeouts, capped retries with backoff on 429/5xx, connection reuse.
- **No fake fallback:** an unconfigured provider errors clearly; it never invents data.
- **Secrets** only via `NEXUS_` env (gitignored `deploy/.env`); the redirect URI is derived, not
  client-supplied. Callback validates `state` before any token exchange.
- **Multi-tenant + RLS** unchanged: every write goes through `TenantSession`; the new tables already
  carry RLS via the dynamic `apply_rls`.
- **Idempotent re-sync** (already true): upserts by `(source, external_id)` / `(owner, person, provider)`.

## 7. Testing (offline; honest about live limits)

- `test_network_crypto` — encrypt→decrypt round-trip; key derivation deterministic; ciphertext ≠ plaintext.
- `test_network_oauth_state` — state JWT sign/verify, expiry, tamper-reject.
- `test_google_connector` / `test_microsoft_connector` — drive `fetch()` through an
  **`httpx.MockTransport`** returning recorded People/Calendar/Graph JSON; assert the produced
  `RawIdentity`/`Touchpoint` set + cursor handling + refresh-on-401. (Test doubles at the HTTP
  boundary — not product mocks.)
- `test_linkedin_csv` — parse a real-format export (incl. the 3-line notes preamble) → identities.
- `test_network_oauth_api` — `/oauth/google/start` returns a Google consent URL + sets state;
  `/callback` with a mocked token exchange connects the account + enqueues sync; unconfigured
  provider → 400.
- Existing `test_network_*` stay green (fixture path unchanged).
- **Not machine-verifiable here:** a real end-to-end live fetch needs the operator's Google/Azure
  OAuth app + a real account. That step is verified by the operator after credentials are set.

## 8. Frontend changes (`frontend/`)

- **Remove** `SAMPLE_IDENTITIES`, the "Import sample" button, and the "Demo network" provider.
- **Connect Google / Microsoft** → call `/network/oauth/{provider}/start`, redirect the browser to
  the returned consent URL. On return (`/network?connected={provider}`), toast + refetch sources
  (first sync already queued). A provider that returns `400 provider_not_configured` shows a clear
  "ask your admin to configure {provider}" message (no fake connect).
- **LinkedIn** → an upload card: file input for `Connections.csv` + a one-line "Settings → Get a copy
  of your data on LinkedIn" hint; posts to `/import-linkedin`.
- Source rows show **real** status, last-synced time, a **Sync** button, and **Reconnect** when
  `status=error`. Pooling toggle unchanged. No demo affordances anywhere.

## 9. What the operator must provide to go live (asked when needed)

- A **Google Cloud** OAuth client (Web app): client id/secret, consent screen (Contacts + Calendar
  scopes), and the registered redirect URI `…/api/network/oauth/google/callback`.
- An **Azure AD** app registration (optional, for Microsoft): client id/secret + redirect URI.
- Env: `NEXUS_NETWORK_GOOGLE_CLIENT_ID/SECRET`, `NEXUS_NETWORK_MICROSOFT_CLIENT_ID/SECRET`,
  `NEXUS_NETWORK_OAUTH_REDIRECT_BASE`, and (optional) `NEXUS_NETWORK_TOKEN_ENC_KEY`.
- Until set, Google/Microsoft connect is inert (clear 400); LinkedIn CSV import works with no creds.

## 10. Non-breaking & migration

- No schema change required — tokens reuse `network_source_accounts.oauth` (JSON, already write-only)
  and `sync_cursor`. (If a per-source `scopes`/`token_expires_at` column proves useful it would be a
  small additive migration 0019; default plan stores those inside the encrypted `oauth` blob, so
  **no migration**.)
- Additive new modules/routes/settings; the only edits to existing files are the router additions,
  the registry mapping, the frontend source changes, and the config settings. Existing endpoints and
  the offline test path are unchanged.
