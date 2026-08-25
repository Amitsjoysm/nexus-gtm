# Per-Tenant CRM Credentials & Connection UI — Design

> **Status:** Approved design (brainstorming output). Next step: implementation plan.
> **Date:** 2026-08-20
> **Base:** `master` (verified at `9dc868f`). See §0.1 — an earlier draft of this spec was written
> against `main`, which is 143 commits stale, and several of its decisions were wrong as a result.
> **Scope:** Turn CRM connectivity from a deployment-global env setting into a per-tenant,
> customer-managed, encrypted connection with a real UI. HubSpot is the one live provider.

## 0. Problem

Verified against `master` at `9dc868f`:

- CRM credentials are **deployment-global env only** — `NEXUS_CRM_PROVIDER` +
  `NEXUS_HUBSPOT_ACCESS_TOKEN`, read in
  `nexus/ingestion/crm.py::build_crm_connector_from_settings()` and memoized in a module-level
  singleton by `get_crm_connector()`. `nexus/ingestion/crm.py` is **byte-identical** between
  `main` and `master`, so this defect is untouched by the 143 commits in between.
- `frontend/src/pages/IntegrationsPage.tsx::CrmCard` is a **manual account-entry grid**, not a
  connection form — also byte-identical between the two branches. There are **no credential
  fields anywhere in the product**.

Consequences:

1. Every tenant shares one HubSpot token. A customer cannot connect their own CRM.
2. `nexus/workers/tasks.py::handle_sync_crm_due_accounts` resolves **one** connector at line 260
   and then loops every tenant with it — so tenant A's accounts are pushed into whatever portal
   the deployment's env token points at. This is a cross-tenant data-egress bug, not just a
   missing feature, and it is the most important thing this work fixes.
3. `GET /integrations/crm/sync-status` reports the *global* `settings.crm_provider`, wrong for any
   tenant that connects its own CRM.
4. `POST /crm/sync` with no rows posted pulls from the *deployment's* connector
   (`integrations.py:100`) and 400s with a message about `NEXUS_CRM_PROVIDER` — which will be the
   wrong explanation once a tenant has its own connection.

### 0.1 Base correction (why this spec was rewritten)

The first draft was written against `main`. `main` is **143 commits behind `master`** and has zero
commits `master` lacks; `master` is the trunk. Three decisions in that draft were wrong:

| Draft decision | Reality on master |
|---|---|
| "Create `nexus/core/crypto.py` with `seal`/`unseal`" | **It already exists**, with a better API: `fernet_for(key)` / `seal_text` / `unseal_text`, each caller passing its own key so keys rotate independently. Creating it would have overwritten the module backing MFA seeds, network OAuth tokens, and source-DB DSNs. |
| "Refactor `nexus/network/crypto.py` to delegate" | **Already delegates** to `core.crypto.fernet_for`. |
| "Migration `0021_crm_connections`" | Migrations reach **`0043_signal_subtype`**. Renumbered to **`0044`**. |

Master also establishes the convention this design now follows: a **per-subsystem crypto module
with its own dedicated key setting** — `mfa_secret_enc_key`, `network_token_enc_key`,
`people_data_enc_key`, `source_db_dsn_enc_key`, each blank-deriving from `secret_key`.
`nexus/sources/crypto.py` is the reference implementation.

## 1. Decisions

| Decision | Choice | Why |
|---|---|---|
| Credential storage | **New tenant-scoped `crm_connections` table** (one row per tenant) | Gets Postgres RLS automatically; carries real `status`/`verified_at`/`last_error` columns a JSON blob cannot. Rejected a generic `integration_credentials` table (YAGNI) and a `Tenant.crm_settings` JSON column (no per-row status). |
| Provider scope | **HubSpot real; Salesforce known but rejected on write** | `HubSpotConnector` is a real API client and can be genuinely tested. `SalesforceConnector.fetch_accounts()` returns an injected sample — accepting a Salesforce token would store a secret that does nothing. Storage stays provider-agnostic so Salesforce drops in later. |
| Manual grid | **Demoted, kept** | Moves into a collapsed "Import accounts manually" disclosure. `POST /crm/sync` keeps both its paths. |
| Sealing | **`nexus/ingestion/crm_crypto.py`** delegating to `core.crypto.seal_text` | Follows `nexus/sources/crypto.py` exactly. Uses its own key so a CRM key rotation cannot orphan MFA seeds. |
| Encryption key | **Derived from `secret_key`** (`key=""`) for now | The convention wants a dedicated `crm_token_enc_key`, but `nexus/core/config.py` is off-limits (uncommitted work in the main tree). `crm_crypto` routes its key through **one function**, so adding the setting later is a one-line change in one place. |
| Unseal failure | **Tolerant → `{}`** (the `network/crypto.py` posture, not `sources/crypto.py`'s raise) | An unsealable CRM token degrades to a real user-fixable state — "reconnect your CRM" — exactly like an unsealable OAuth bundle. It is not like a DSN, where `""` would be indistinguishable from "never configured". |
| Test-injection seam | **`set_crm_connector()` survives and still wins** | Keeps `tests/test_crm_push.py` and `tests/test_crm_auto_sync.py` green unedited, and stays the documented way to inject a recording stub. |

### Hard constraints

- **The env path must keep working byte-for-byte.** A deployment with only `NEXUS_CRM_PROVIDER` +
  `NEXUS_HUBSPOT_ACCESS_TOKEN` set, and no tenant rows, must behave exactly as today. Tenant
  credentials are an *override*, never a replacement.
- **Do not modify** `deploy/cloud/**`, `azure-pipelines-*.yml`, `docs/deployment/`,
  `nexus/core/config.py`, `nexus/core/db.py`, `scripts/apply_rls.py`,
  `nexus/relevance/website_icp.py`, `tests/test_db_pool_config.py` — uncommitted work in all of
  these (re-confirmed on master).
- Credentials must **never** appear in any response model, log line, or error message.

## 2. Architecture

```
nexus/ingestion/crm_crypto.py      (new)  seal_crm_secret / unseal_crm_secret
nexus/core/audit.py                (new)  audit(action, *, tenant_id, actor, **fields)
nexus/models/integration.py        (new)  CrmConnection
nexus/models/__init__.py           (edit) register + export
nexus/ingestion/crm.py             (edit) + CRMTestResult / test_connection(); split globals
nexus/ingestion/crm_credentials.py (new)  store/load/clear + resolve_crm_connector + cache
nexus/api/schemas.py               (edit) CRMConnectionIn / Out / TestOut
nexus/api/routers/integrations.py  (edit) 4 endpoints + 3 call sites + sync-status fix
nexus/plays/engine.py              (edit) per-tenant resolution
nexus/workers/tasks.py             (edit) per-tenant resolution — the leak fix
migrations/versions/0047_crm_connections.py (new)
```

### 2.1 Sealing — `nexus/ingestion/crm_crypto.py`

Mirrors `nexus/sources/crypto.py`: a thin subsystem module over `core.crypto`.

```python
def _key() -> str:
    # The one place the CRM sealing key is chosen. Adding `crm_token_enc_key` to Settings later
    # is a one-line change here and nowhere else.
    return ""

def seal_crm_secret(bundle: dict) -> dict:   # -> {"enc": "<fernet>"}
def unseal_crm_secret(blob: dict | None) -> dict   # tolerant: {} on empty/tampered
```

The JSON-envelope shape `{"enc": ...}` matches `network/crypto.py` so the column can hold a
multi-field bundle (an OAuth token set later) without a migration.

### 2.2 Model — `CrmConnection`

`nexus/models/integration.py`, exported from `nexus/models/__init__.py`. One row per tenant
(unique on `tenant_id`).

| column | purpose |
|---|---|
| `provider` | `hubspot` / `salesforce` |
| `secret` | JSON `{"enc": "<fernet>"}` — write-only seam |
| `api_base` | region/proxy override |
| `status` | `unverified` / `connected` / `error` |
| `verified_at`, `last_error` | last test outcome |
| `updated_by_user_id` | who changed it |

RLS needs no manual work: `scripts/apply_rls.py::_tenant_tables()` walks
`Base.metadata.sorted_tables` and covers every table carrying `tenant_id`.

Migration `0047_crm_connections`, `down_revision = "0043_signal_subtype"`.

### 2.3 Resolution — the core refactor

`nexus/ingestion/crm_credentials.py`:

```python
async def resolve_crm_connector(ts: TenantSession) -> CRMConnector
```

Precedence: **(1)** an explicitly installed connector (`set_crm_connector` — the test seam);
**(2)** the tenant's stored credential; **(3)** `get_crm_connector()` — today's env behavior.
Step 3 is why an env-only deployment is unaffected.

**Step 1 requires splitting a global in `crm.py`, and it is easy to get wrong.** Today
`_connector` serves two roles: the test override *and* the memoized env instance. After any
`get_crm_connector()` call on an env-configured deployment it is non-`None` — so a naive "if set,
it's an override" check would skip tenant credentials entirely and silently re-create the
shared-token bug. Split into `_connector` (memoized) and `_override` (installed), add
`get_crm_connector_override()`; `get_crm_connector()` / `set_crm_connector()` keep their exact
observable behavior.

**Caching.** Bounded LRU (128), `tenant_id → (fingerprint, connector)`, fingerprint =
`updated_at|provider|api_base`. Be precise about what it buys: resolution **still reads the row
every call** — that lookup is how a worker notices a credential the API just changed, so it cannot
be skipped. The cache avoids the decrypt + construction and keeps the connector *instance* stable
so `MAX_RECORDED_PUSHES` buffers survive across pushes. N+1 pressure is solved by hoisting
resolution out of inner loops, not by the cache.

**Call sites converted (six on master):**

| File:line | Change |
|---|---|
| `integrations.py:100` | `/crm/sync` no-rows pull — per-tenant, and its 400 must name the tenant's connection, not just `NEXUS_CRM_PROVIDER` |
| `integrations.py:133` | `/crm/push/{id}` |
| `plays/engine.py:115,120` | resolve once per action, reuse for push_account + push_activity |
| `tasks.py:298` | `handle_sync_crm_account` |
| `tasks.py:260` | **`handle_sync_crm_due_accounts` — hoist resolution into the per-tenant loop.** The leak. |

### 2.4 Test connection

Additive `CRMTestResult(ok, label, detail)` + `CRMConnector.test_connection()`.

`HubSpotConnector` tries `GET /account-info/v3/details` for a friendly portal label; on 403 (the
`oauth` scope many private apps lack) falls back to `GET /crm/v3/objects/companies?limit=1`, which
needs only the scope `fetch_accounts` already requires. Mapping: `401 → "Invalid or expired access
token."`, `403 → "Token is missing required scopes (crm.objects.companies.read/write)."`,
`429 → rate limit`, other → `HTTP {n}`. Never raises; an exception is logged and returned as a
generic failure so internals cannot leak.

`SalesforceConnector` returns `ok=False`, "Salesforce connections are not available yet."
The base/stub returns ok with an offline note.

### 2.5 Endpoints — all `Permission.manage_workspace`, all audited

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/integrations/crm/connection` | `CRMConnectionOut`. **Never the secret.** |
| `PUT` | `/integrations/crm/connection` | Blank `access_token` keeps the stored one. Rejects unknown providers (400) and known-but-not-live ones (400). Resets to `unverified`. |
| `POST` | `/integrations/crm/connection/test` | Runs `test_connection()` on the *resolved* connector; stamps `verified_at` or `last_error`. |
| `DELETE` | `/integrations/crm/connection` | Deletes the row → falls back to env. `204`. |

`CRMConnectionOut` is the exhaustive list of what leaves the server: `provider`,
`source` (`tenant|env|none`), `has_credentials`, `status`, `api_base`, `verified_at`,
`last_error`, `updated_at`. A row whose secret no longer decrypts reports
`source="tenant", has_credentials=false, status="error"` with a "reconnect" message — an admin
must see that, not a silent "not connected".

**`GET /crm/sync-status`** reports the tenant's effective provider instead of `settings.crm_provider`.

### 2.6 Audit

No audit table exists and adding one is out of scope. `nexus/core/audit.py` (~20 lines) emits one
stable line on the `nexus.audit` logger:
`action=crm.connection.set tenant=<id> actor=<uid> provider=hubspot token_set=true`.
It logs `token_set`, **never any part of a token** — not a prefix, length, or hash.

## 3. Frontend

`CrmConnectionCard` becomes the primary CRM card (skeleton / error / loaded states): status badge
(`Connected` / `Not verified` / `Connection error` / `Using deployment default` / `Not connected`),
provider select with Salesforce disabled, `type="password"` token field showing `••••• (saved)`
when `has_credentials`, an Advanced disclosure for `api_base`, and Save / Test connection /
Disconnect (confirmed in a `Modal`). `last_error` renders inline, not only in a toast, so it
survives a page revisit. The manual grid moves into a collapsed "Import accounts manually"
disclosure with its behavior unchanged.

`impeccable` is invoked before writing this code, per CLAUDE.md; `DESIGN.md` governs tokens.

## 4. Testing

`tests/test_crm_connection.py`. **Crypto:** round-trip, tampered blob → `{}`, and a guard that the
CRM envelope is independent of the network one. **Secret never escapes:** raw DB column has no
plaintext; `GET`, `PUT`, and `POST /test` raw response bodies contain no token substring.
**RBAC:** a rep gets 403 on all four verbs. **Tenancy:** A's credential invisible to B;
`resolve_crm_connector` returns *different* connectors per tenant. **Fallback (the
non-regression core):** no credential → env connector; stored beats env; DELETE → env; blank token
preserves the secret; `set_crm_connector` override beats both; an undecryptable secret falls back
rather than 500s. **Behavior:** test stamps `verified_at` / `last_error`; cache notices a changed
token. **The leak fix:** two tenants driven through `handle_sync_crm_due_accounts`, each landing on
its own connector — this test fails against current `master`.

`tests/test_crm_push.py` and `tests/test_crm_auto_sync.py` must pass **unedited**. A fixture
hygiene note that matters: `test_crm_push.py` tears down with `set_crm_connector(StubCRMConnector())`
— a *fresh stub*, not `None` — so an override can still be installed process-wide when a later
module runs, which would make the fallback tests pass vacuously. `test_crm_connection.py` gets an
autouse `set_crm_connector(None)` fixture.

## 5. Out of scope

- HubSpot OAuth (this is private-app-token auth). The sealed bundle is JSON, so a token set drops
  in later without a migration.
- A real Salesforce adapter.
- Per-tenant SEP credentials — same shape, deliberately not bundled.
- An audit *table*.
- A dedicated `crm_token_enc_key` Settings field (blocked on `config.py`); routed through one
  function so it is a one-line addition later.
