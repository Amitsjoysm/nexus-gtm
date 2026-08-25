# HubSpot OAuth, Salesforce adapter, per-tenant SEP, audit table — Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax. Base: `claude/jovial-sutherland-052807`
> (which is `master` + the per-tenant CRM credentials work).

**Goal:** Finish the four items the CRM-credentials spec deliberately deferred.

**Order is by dependency, not by size.** The connection store must be generalized before SEP can
reuse it; OAuth must store a refreshable bundle before either OAuth provider can work.

---

## Blocking constraint and how it is resolved

`nexus/core/config.py` is still uncommitted in the user's main tree and must not be edited. OAuth
needs deployment-level app credentials (client id/secret/redirect base) — these are *our* vendor app
registration, not per-tenant data, so they belong in settings.

`os.getenv` is **not** an acceptable substitute: `Settings` uses `env_file=".env"`, so environment
reads would silently miss every value a developer put in `.env`.

**Resolution:** a second `BaseSettings` class, `nexus/integrations/settings.py`, with the same
`env_prefix="NEXUS_"` and `env_file=".env"`. Identical resolution behavior, zero `config.py` churn,
and folding it into `Settings` later is a copy-paste of the field block plus a find-replace of
`get_integration_settings()` → `get_settings()`. Every new field in this work lands there.

---

## Task 1: `IntegrationSettings`

**Files:** Create `nexus/integrations/settings.py`; Test `tests/test_integration_settings.py`

- [ ] **Step 1: Write the failing test**

```python
def test_integration_settings_read_the_nexus_prefix(monkeypatch):
    from nexus.integrations.settings import get_integration_settings

    monkeypatch.setenv("NEXUS_HUBSPOT_CLIENT_ID", "cid-123")
    get_integration_settings.cache_clear()
    try:
        assert get_integration_settings().hubspot_client_id == "cid-123"
    finally:
        get_integration_settings.cache_clear()


def test_integration_settings_default_to_inert():
    """Unset credentials must read as 'not configured', never as a fake default."""
    from nexus.integrations.settings import get_integration_settings

    s = get_integration_settings()
    assert s.hubspot_client_id == ""
    assert s.salesforce_client_id == ""
    assert s.oauth_redirect_base == ""
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Create the module**

```python
# nexus/integrations/settings.py
"""Deployment-level integration credentials (OAuth app registrations).

These are *our* app registrations with HubSpot / Salesforce / Outreach — one per deployment, not
per tenant. Per-tenant secrets live encrypted in ``integration_connections``; this file holds only
the client id/secret pair that identifies this product to the vendor.

**Why this is not in ``nexus/core/config.py``:** that file has uncommitted work in it and is out of
scope for this change. This class deliberately mirrors ``Settings`` exactly — same ``NEXUS_``
prefix, same ``.env`` file, same ``extra="ignore"`` — so a value resolves identically either way,
and merging it back is a paste of the field block plus a rename of the accessor. Reading these via
``os.getenv`` instead would have missed every value set in ``.env``.

Everything defaults to empty, which means **inert**: the OAuth endpoints return a clear
"not configured" rather than half-building an authorize URL that the vendor would reject.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class IntegrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEXUS_", env_file=".env", extra="ignore")

    # Public base URL of this deployment, e.g. https://app.example.com. OAuth callbacks are
    # built from it; a provider rejects a redirect_uri that is not registered, so this must
    # match the vendor app config exactly.
    oauth_redirect_base: str = ""

    hubspot_client_id: str = ""
    hubspot_client_secret: str = ""

    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    # login.salesforce.com for production orgs, test.salesforce.com for sandboxes.
    salesforce_login_base: str = "https://login.salesforce.com"

    outreach_client_id: str = ""
    outreach_client_secret: str = ""


@lru_cache
def get_integration_settings() -> IntegrationSettings:
    return IntegrationSettings()
```

- [ ] **Step 4: Run to verify pass. Commit.**

---

## Task 2: Audit table

The `nexus.audit` logger stays (operators grep it); the table is what a workspace admin can read.

**Files:** Create `nexus/models/audit.py`, `migrations/versions/0048_audit_log.py`; modify
`nexus/core/audit.py`, `nexus/models/__init__.py`, `nexus/api/routers/workspace.py`,
`nexus/api/schemas.py`

- [ ] **Step 1: Write the failing tests**

```python
async def test_audit_row_is_written_and_scoped_to_the_tenant():
    from nexus.core.audit import record_audit
    from nexus.models.audit import AuditLog

    tid = await make_tenant(slug="aud", name="AUD")
    async with tenant_session(tid) as ts:
        await record_audit(ts, "crm.connection.set", actor_user_id="u-1",
                           target_type="crm_connection", target_id="c-1",
                           meta={"provider": "hubspot", "token_set": True})
        await ts.flush()

    async with tenant_session(tid) as ts:
        rows = await ts.list(AuditLog)
        assert len(rows) == 1
        assert rows[0].action == "crm.connection.set"
        assert rows[0].meta["provider"] == "hubspot"

    other = await make_tenant(slug="aud2", name="AUD2")
    async with tenant_session(other) as ts:
        assert await ts.list(AuditLog) == []


async def test_record_audit_also_emits_the_log_line(caplog):
    """The table is for admins; the line is for operators. Both, not either."""
    from nexus.core.audit import record_audit

    tid = await make_tenant(slug="aud3", name="AUD3")
    with caplog.at_level("INFO", logger="nexus.audit"):
        async with tenant_session(tid) as ts:
            await record_audit(ts, "crm.connection.clear", actor_user_id="u-2")
            await ts.flush()
    assert any("action=crm.connection.clear" in r.getMessage() for r in caplog.records)


async def test_audit_write_never_breaks_the_action_it_records():
    """An audit failure must not roll back the thing being audited — losing the credential
    change to save the audit row is exactly backwards."""
    from nexus.core.audit import record_audit

    tid = await make_tenant(slug="aud4", name="AUD4")
    async with tenant_session(tid) as ts:
        # meta carries something json cannot serialize; the call must still return.
        await record_audit(ts, "x.y", actor_user_id="u", meta={"bad": object()})


async def test_audit_endpoint_is_admin_only_and_newest_first(client):
    h = auth(await signup(client))
    await client.put("/api/integrations/crm/connection", headers=h,
                     json={"provider": "hubspot", "access_token": "pat-abc"})
    r = await client.get("/api/workspace/audit", headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert rows[0]["action"] == "crm.connection.set"
    assert "pat-abc" not in r.text
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Create `nexus/models/audit.py`**

```python
# nexus/models/audit.py
"""Workspace audit trail.

Tenant-scoped on purpose, unlike ``billing_audit_log``, which is platform-global and names its
column ``subject_tenant_id`` precisely so RLS does *not* hide it from operators. This table is the
opposite case: the reader is the workspace admin who made the change, so it carries ``tenant_id``
and RLS enrolment is what we want.

``meta`` never holds a secret. Callers pass ``token_set=True``, not the token.
"""
from __future__ import annotations

from sqlalchemy import JSON, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped


class AuditLog(IdMixin, TimestampMixin, TenantScoped, Base):
    """One privileged, security-relevant action."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_tenant_created", "tenant_id", "created_at"),)

    action: Mapped[str] = mapped_column(String(64), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_type: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
```

Register in `nexus/models/__init__.py` (import + `__all__`).

- [ ] **Step 4: Extend `nexus/core/audit.py`** — keep `audit()` unchanged, add:

```python
async def record_audit(
    ts, action: str, *, actor_user_id: str | None = None,
    target_type: str = "", target_id: str = "", meta: dict | None = None,
) -> None:
    """Write an audit row **and** emit the log line.

    Never raises. An audit failure must not roll back the action it records — losing a credential
    change to save its audit row is exactly backwards — so a bad ``meta`` or a write error is
    logged at ERROR (with the payload, so the evidence survives) and swallowed.
    """
```

Implementation notes: `json.dumps(meta, default=str)` round-trip to guarantee the JSON column
accepts it; wrap the whole body in `try/except Exception` logging at ERROR.

- [ ] **Step 5: Migration `0048_audit_log.py`**, `down_revision = "0047_crm_connections"`.

- [ ] **Step 6: `GET /workspace/audit`** — `manage_workspace`, newest-first, `limit` (default 100,
  max 500), optional `action` filter. Schema `AuditEntryOut`.

- [ ] **Step 7: Swap the four CRM call sites** in `integrations.py` from `audit(...)` to
  `await record_audit(ts, ...)`.

- [ ] **Step 8: Run, verify replay test, commit.**

```bash
python -m pytest tests/test_crm_connection.py tests/test_audit.py tests/test_migrations_replay.py -q
```

---

## Task 3: Generalize the connection store

The second consumer (SEP) has arrived, which is the moment to generalize — the earlier YAGNI call
against a generic table was correct *then* and is wrong *now*. `crm_connections` becomes
`integration_connections` with a `kind` discriminator. Safe to rename because `0044` has not
shipped outside this branch.

**Files:** modify `nexus/models/integration.py`, `nexus/ingestion/crm_credentials.py`;
create `migrations/versions/0049_integration_connections.py`

- [ ] **Step 1: Test that both kinds coexist for one tenant**

```python
async def test_one_tenant_can_hold_a_crm_and_a_sep_connection():
    from nexus.models.integration import IntegrationConnection

    tid = await make_tenant(slug="two", name="TWO")
    async with tenant_session(tid) as ts:
        ts.add(IntegrationConnection(tenant_id=tid, kind="crm", provider="hubspot",
                                     secret=seal_crm_secret({"access_token": "a"})))
        ts.add(IntegrationConnection(tenant_id=tid, kind="sep", provider="salesloft",
                                     secret=seal_crm_secret({"api_key": "b"})))
        await ts.flush()
        assert len(await ts.list(IntegrationConnection)) == 2
```

- [ ] **Step 2: Rename model + table.** `CrmConnection` → `IntegrationConnection`, add
  `kind: Mapped[str] = mapped_column(String(16), default="crm")`, unique becomes
  `("tenant_id", "kind")`. Keep `CrmConnection = IntegrationConnection` as a module alias **only if
  something outside this branch imports it** — it does not, so do a clean rename.

- [ ] **Step 3: Migration 0046** — `op.rename_table`, add `kind` NOT NULL server_default `'crm'`,
  drop the old unique constraint, add `uq_integration_connection_tenant_kind`. Use
  `batch_alter_table` so SQLite works.

- [ ] **Step 4: Parameterize `crm_credentials`** — every query gains `kind="crm"`. Extract the
  shared body into `nexus/integrations/connections.py` with `kind` as an argument so SEP reuses it
  rather than copying it.

- [ ] **Step 5: Full CRM suite must still pass unchanged. Commit.**

---

## Task 4: Per-tenant SEP credentials + real adapters

Both current SEP "connectors" are recording stubs, so this is also the first real SEP integration.

**Files:** modify `nexus/integrations/sep.py`; create `nexus/integrations/sep_credentials.py`;
modify `nexus/api/routers/integrations.py`, `nexus/plays/engine.py`

- [ ] **Step 1: Tests** — mirror the CRM set: secret never returned, rep 403, tenant isolation,
  env/stub fallback, `set_sep_connector` override still wins, and a `test_connection`.

- [ ] **Step 2: `SalesloftConnector`** — real. `Authorization: Bearer <api_key>`,
  `GET /v2/me.json` for `test_connection`, `POST /v2/people.json` +
  `POST /v2/cadence_memberships.json` for `push_contact`. Same never-raise posture as
  `HubSpotConnector`: a failure is a `SEPPushResult(ok=False)`, never an exception.

- [ ] **Step 3: `OutreachConnector`** — OAuth2 only; `GET /api/v2/` for test,
  `POST /api/v2/prospects` + `POST /api/v2/sequenceStates` for push. Uses the shared refreshable
  bundle from Task 5.

- [ ] **Step 4: `StubSEPConnector`** — the existing recording behavior, kept as the offline default
  so the whole suite and every play stay zero-network. **This is the piece that must not regress:**
  `get_sep_connector()` currently returns `OutreachConnector()` (a stub) by default, so making
  Outreach real silently turns every default deployment's SEP pushes into failing network calls.
  The default becomes the explicit stub.

- [ ] **Step 5: `resolve_sep_connector(ts)`** — same three-layer precedence as CRM.

- [ ] **Step 6: Endpoints** `/integrations/sep/connection` (GET/PUT/test/DELETE) + convert the
  `sep_push` endpoint and the plays `sep_push` action to per-tenant resolution.

- [ ] **Step 7: Run, commit.**

---

## Task 5: Refreshable OAuth bundles + HubSpot OAuth

- [ ] **Step 1:** `nexus/integrations/oauth.py` — PKCE + signed state JWT, lifted from
  `nexus/network/oauth.py` conventions (`typ` claim, short TTL, tenant + kind + provider bound).
  HubSpot does not support PKCE, so the verifier is optional; the state JWT is what prevents CSRF.

- [ ] **Step 2:** The sealed bundle grows to
  `{access_token, refresh_token, expires_at, instance_url?}`. `has_credentials` must accept a bundle
  with *either* a static token or an OAuth pair — a workspace that connected via OAuth has no
  "access_token the admin typed" and must not read as unconfigured.

- [ ] **Step 3: Token refresh is the real work.** `HubSpotConnector` takes a static string today.
  It gains an optional async `token_provider` callback; `_request` refreshes once on 401 and
  retries. The refreshed bundle must be persisted, so the callback owns a `TenantSession`.
  **Refresh must be single-flight per tenant** or a burst of pushes each mint a new token and
  HubSpot invalidates the earlier ones.

- [ ] **Step 4: Endpoints** `GET /integrations/crm/oauth/{provider}/start` (returns the authorize
  URL; 400 when the deployment is not configured) and
  `GET /integrations/crm/oauth/{provider}/callback` (exchanges, seals, redirects to
  `/integrations?connected=…`).

- [ ] **Step 5:** UI — "Connect HubSpot" button alongside the manual token field, shown only when
  `oauth_available` is true on `CRMConnectionOut`.

- [ ] **Step 6: Run, commit.**

---

## Task 6: Real Salesforce adapter

- [ ] **Step 1:** OAuth2 web-server flow against `salesforce_login_base`; the token response
  carries `instance_url`, which every subsequent REST call needs — store it in the bundle.
- [ ] **Step 2:** `fetch_accounts` — SOQL
  `SELECT Id,Name,Website,Industry,NumberOfEmployees,BillingCountry FROM Account LIMIT 200` via
  `GET /services/data/v59.0/query`. Map `Website` → domain (host only).
- [ ] **Step 3:** `push_account` — `PATCH /sobjects/Account/{id}` when `crm_id` is known, else
  `GET /query` by domain, else `POST /sobjects/Account`. Contacts by email.
- [ ] **Step 4:** `push_activity` — `POST /sobjects/Task` with `WhatId` = the Account id.
- [ ] **Step 5:** `test_connection` — `GET /services/data/v59.0/limits`; map 401 → expired, 403 →
  scopes.
- [ ] **Step 6:** Remove the "not available yet" gate: `LIVE_CRM_PROVIDERS` gains `salesforce`, the
  UI option is enabled, and `test_connection` stops returning the placeholder refusal.
- [ ] **Step 7: Run the full suite. Commit.**

---

## Definition of done

- [ ] `python -m pytest tests/ -q` fully green
- [ ] `python -m pytest tests/test_migrations_replay.py -v` green (chain replays, one head)
- [ ] `cd frontend && npx tsc --noEmit && npm run build` clean
- [ ] `git diff master...HEAD --name-only` touches none of the 8 protected paths
- [ ] No secret in any response model, log line, or audit `meta`
