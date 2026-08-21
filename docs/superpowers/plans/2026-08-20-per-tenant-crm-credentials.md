# Per-Tenant CRM Credentials & Connection UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or
> superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let each tenant connect its own CRM with an encrypted, admin-managed credential, and
resolve the CRM connector per tenant instead of from a process-wide singleton.

**Base:** `master` at `9dc868f`. **Not `main`** — `main` is 143 commits stale and an earlier draft
of this plan was wrong because of it (see spec §0.1).

**Spec:** `docs/superpowers/specs/2026-08-20-per-tenant-crm-credentials-design.md`

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, Pydantic v2, Alembic, `cryptography.fernet` (via
`python-jose[cryptography]`), React 18 + TypeScript + Vite, CSS Modules.

---

## Ground rules

**Never modify or commit:** `deploy/cloud/**`, `azure-pipelines-*.yml`, `docs/deployment/`,
`nexus/core/config.py`, `nexus/core/db.py`, `scripts/apply_rls.py`,
`nexus/relevance/website_icp.py`, `tests/test_db_pool_config.py`. Run `git status --porcelain`
before every commit.

**Do NOT create `nexus/core/crypto.py`** — it exists on master and backs MFA seeds, network OAuth
tokens, and source-DB DSNs. Use its `seal_text` / `unseal_text` / `fernet_for`.

**Never** put a token, or any part of one, into a response model, log line, or exception message.

**Tests:** `python -m pytest tests/... -q` from repo root. `asyncio_mode=auto`.

---

## File structure

| File | Responsibility |
|---|---|
| `nexus/ingestion/crm_crypto.py` *(create)* | CRM secret sealing over `core.crypto` |
| `nexus/core/audit.py` *(create)* | `audit()` — one structured line |
| `nexus/models/integration.py` *(create)* | `CrmConnection` |
| `nexus/models/__init__.py` *(modify)* | Register + export |
| `migrations/versions/0044_crm_connections.py` *(create)* | The table |
| `nexus/ingestion/crm.py` *(modify)* | `CRMTestResult` + `test_connection()`; split globals |
| `nexus/ingestion/crm_credentials.py` *(create)* | Store/load/clear + resolve + cache |
| `nexus/api/schemas.py` *(modify)* | `CRMConnectionIn/Out/TestOut` |
| `nexus/api/routers/integrations.py` *(modify)* | 4 endpoints + 3 call sites + sync-status |
| `nexus/plays/engine.py` *(modify)* | Per-tenant resolution |
| `nexus/workers/tasks.py` *(modify)* | Per-tenant resolution — **the leak fix** |
| `frontend/src/lib/types.ts`, `api.ts` *(modify)* | Types + 4 client methods |
| `frontend/src/pages/IntegrationsPage.tsx` + `.module.css` *(modify)* | Connection form |
| `tests/test_crm_connection.py` *(create)* | The whole feature |

---

## Task 1: CRM secret sealing

**Files:** Create `nexus/ingestion/crm_crypto.py`; Test `tests/test_crm_connection.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Per-tenant CRM credentials: sealing, storage, resolution, endpoints, and the worker fix.

Offline throughout. The recurring theme: a stored token must never leave the server — several
tests assert against the *raw response text* rather than a parsed model, because a parsed model
can only prove the fields we thought to check.
"""
from __future__ import annotations

import pytest

from nexus.ingestion.crm_crypto import seal_crm_secret, unseal_crm_secret
from tests.conftest import auth, make_tenant, principal_from_token, signup, tenant_session


def test_seal_unseal_round_trip():
    blob = seal_crm_secret({"access_token": "pat-secret-123"})
    assert set(blob) == {"enc"}
    assert "pat-secret-123" not in blob["enc"]
    assert unseal_crm_secret(blob) == {"access_token": "pat-secret-123"}


def test_unseal_is_tolerant_of_garbage():
    """A corrupt or key-rotated blob means 'reconnect', never a 500."""
    for bad in (None, {}, {"enc": ""}, {"enc": "not-a-fernet-token"}, {"nope": "x"}):
        assert unseal_crm_secret(bad) == {}


def test_crm_envelope_is_independent_of_the_network_one():
    """Separate subsystems, separate keys later — neither should decrypt the other's blob by
    accident once a dedicated crm_token_enc_key exists."""
    from nexus.network.crypto import unseal_tokens

    blob = seal_crm_secret({"access_token": "pat-x"})
    # Today both derive from secret_key, so this documents the shape, not key isolation.
    assert "enc" in blob and isinstance(unseal_tokens(blob), dict)
```

- [ ] **Step 2: Run to verify it fails** — `python -m pytest tests/test_crm_connection.py -q`
      Expected: `ModuleNotFoundError: No module named 'nexus.ingestion.crm_crypto'`.

- [ ] **Step 3: Create `nexus/ingestion/crm_crypto.py`**

```python
# nexus/ingestion/crm_crypto.py
"""Sealing of per-tenant CRM credentials at rest.

A CRM access token is a live credential to the customer's system of record. It never sits in a
column in the clear and is never serialised back to a client, not even to the admin who typed it.

Mirrors ``nexus/sources/crypto.py``: a thin subsystem module over ``nexus/core/crypto.py`` so the
key derivation is not duplicated. It should have its own key for the reason stated there —
rotating the key protecting CRM tokens must not orphan MFA seeds or network OAuth tokens — but
adding a Settings field is out of scope for this change, so ``_key`` returns "" and the key
derives from ``secret_key``. That is still "always encrypted, no silent plaintext fallback";
``_key`` is the single place to change when ``crm_token_enc_key`` is added.

Unlike ``sources/crypto.py``, an unsealable value is **tolerated** and reads as ``{}``. This
matches ``network/crypto.py``: an unusable CRM token degrades to "reconnect your CRM", a real
state the admin can fix. A DSN cannot degrade that way because "" is what a *deleted* secret looks
like; a CRM connection row still exists and is reported as needing reconnection.
"""
from __future__ import annotations

import json

from nexus.core.crypto import seal_text, unseal_text


def _key() -> str:
    """The CRM sealing key. Empty derives one from ``secret_key`` (see module docstring)."""
    return ""


def seal_crm_secret(bundle: dict) -> dict:
    """Encrypt a credential bundle. Returns the JSON-column value ``{"enc": "..."}``.

    A dict rather than a bare string so an OAuth token set (access + refresh + expiry) can be
    stored later without a migration.
    """
    return {"enc": seal_text(json.dumps(bundle), key=_key())}


def unseal_crm_secret(blob: dict | None) -> dict:
    """Decrypt a stored value back to the bundle. ``{}`` for empty/missing/tampered input."""
    if not blob:
        return {}
    plain = unseal_text(blob.get("enc") or "", key=_key())
    if not plain:
        return {}
    try:
        loaded = json.loads(plain)
    except (ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_crm_connection.py -q`
- [ ] **Step 5: Commit** — `git add nexus/ingestion/crm_crypto.py tests/test_crm_connection.py && git commit -m "feat(crm): seal per-tenant CRM credentials at rest"`

---

## Task 2: Audit logging

**Files:** Create `nexus/core/audit.py`; Test `tests/test_crm_connection.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
def test_audit_emits_one_structured_line(caplog):
    from nexus.core.audit import audit

    with caplog.at_level("INFO", logger="nexus.audit"):
        audit("crm.connection.set", tenant_id="t-1", actor="u-9",
              provider="hubspot", token_set=True)

    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    for fragment in ("action=crm.connection.set", "tenant=t-1", "actor=u-9",
                     "provider=hubspot", "token_set=true"):
        assert fragment in msg


def test_audit_omits_empty_actor_and_quotes_spaces():
    from nexus.core.audit import _format  # noqa: PLC2701 - unit-testing the formatter

    line = _format("crm.connection.test", "t-1", None, {"detail": "two words", "ok": False})
    assert "actor=" not in line
    assert 'detail="two words"' in line
    assert "ok=false" in line
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError: nexus.core.audit`
- [ ] **Step 3: Create `nexus/core/audit.py`**

```python
# nexus/core/audit.py
"""Audit trail for privileged, security-relevant actions.

Deliberately a *log*, not a table: the events worth auditing today (a workspace admin changing an
integration credential) are low-volume and belong in the same stream as the rest of the platform's
operational logging, where a deployment's log shipper already retains them. One stable
``key=value`` line per event keeps it greppable and parseable.

The contract that matters: **this function never records a secret.** Callers pass booleans like
``token_set=True``, never the token, a prefix of it, its length, or a hash.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.audit")


def _render(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = "" if value is None else str(value)
    return f'"{text}"' if (" " in text or not text) else text


def _format(action: str, tenant_id: str, actor: str | None, fields: dict) -> str:
    parts = [f"action={action}", f"tenant={tenant_id}"]
    if actor:
        parts.append(f"actor={actor}")
    parts.extend(f"{k}={_render(v)}" for k, v in fields.items())
    return " ".join(parts)


def audit(action: str, *, tenant_id: str, actor: str | None = None, **fields) -> None:
    """Record one audited action, e.g.

    ``action=crm.connection.set tenant=abc actor=u1 provider=hubspot token_set=true``

    Never pass a secret in ``fields`` — pass a boolean saying whether one was supplied.
    """
    logger.info(_format(action, tenant_id, actor, fields))
```

- [ ] **Step 4: Run to verify pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(audit): structured audit logger for privileged actions"`

---

## Task 3: `CrmConnection` model + migration 0044

**Files:** Create `nexus/models/integration.py`, `migrations/versions/0044_crm_connections.py`;
Modify `nexus/models/__init__.py`

- [ ] **Step 1: Write the failing test** — append:

```python
async def test_crm_connection_is_tenant_scoped_and_stores_ciphertext():
    from nexus.models.integration import CrmConnection

    tid_a = await make_tenant(slug="ta", name="A")
    tid_b = await make_tenant(slug="tb", name="B")

    async with tenant_session(tid_a) as ts:
        ts.add(CrmConnection(tenant_id=tid_a, provider="hubspot",
                             secret=seal_crm_secret({"access_token": "pat-A"})))
        await ts.flush()

    async with tenant_session(tid_a) as ts:
        row = await ts.first(CrmConnection)
        assert row is not None
        assert row.provider == "hubspot"
        assert row.status == "unverified"
        assert row.api_base == ""
        assert unseal_crm_secret(row.secret) == {"access_token": "pat-A"}
        assert "pat-A" not in str(row.secret)

    async with tenant_session(tid_b) as ts:
        assert await ts.first(CrmConnection) is None
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError: nexus.models.integration`
- [ ] **Step 3: Create `nexus/models/integration.py`**

```python
# nexus/models/integration.py
"""Per-tenant integration credentials.

One row per tenant per integration. ``secret`` is a write-only seam: it holds a Fernet envelope
(:mod:`nexus.ingestion.crm_crypto`) and is never serialized into a response model. Being
``TenantScoped`` means ``scripts/apply_rls.py`` picks the table up automatically on deploy — it
walks ``Base.metadata.sorted_tables`` — so no manual RLS policy work is needed.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped


class CrmConnection(IdMixin, TimestampMixin, TenantScoped, Base):
    """A tenant's own CRM credentials. Overrides the deployment-wide env configuration."""

    __tablename__ = "crm_connections"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_crm_connection_tenant"),)

    provider: Mapped[str] = mapped_column(String(16))          # hubspot | salesforce
    secret: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # {"enc": "..."}
    api_base: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # unverified: saved, not yet tested. connected: last test passed. error: last test failed.
    status: Mapped[str] = mapped_column(String(16), default="unverified", nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
```

- [ ] **Step 4: Register in `nexus/models/__init__.py`** — add
      `from nexus.models.integration import CrmConnection` after the identity import, and
      `"CrmConnection",` to `__all__`.

- [ ] **Step 5: Run to verify pass**

- [ ] **Step 6: Create `migrations/versions/0044_crm_connections.py`**

```python
"""Per-tenant CRM credentials: crm_connections.

Moves CRM connectivity off deployment-global env vars (NEXUS_CRM_PROVIDER /
NEXUS_HUBSPOT_ACCESS_TOKEN) and onto a per-tenant, encrypted credential. Additive only: with no
rows, every tenant falls back to the env configuration exactly as before.

``secret`` holds a Fernet envelope, never plaintext. The table is tenant-scoped, so
``scripts/apply_rls.py`` applies an RLS policy to it on the next deploy with no manual work.

Revision ID: 0044_crm_connections
Revises: 0043_signal_subtype
Create Date: 2026-08-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_crm_connections"
down_revision = "0043_signal_subtype"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crm_connections",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("secret", sa.JSON(), nullable=False),
        sa.Column("api_base", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="unverified"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("updated_by_user_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", name="uq_crm_connection_tenant"),
    )
    op.create_index("ix_crm_connections_tenant_id", "crm_connections", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_crm_connections_tenant_id", table_name="crm_connections")
    op.drop_table("crm_connections")
```

- [ ] **Step 7: Verify single head** — `python -m alembic heads`
      Expected: exactly `0044_crm_connections (head)`.

> **Do not verify the migration by running `alembic upgrade head` against your dev database.**
> If that file already has tables (the app creates them on startup), the chain fails at
> `0001_initial` with `table chat_sessions already exists`. That is local DB state, not a defect
> in the chain — `tests/test_migrations_replay.py` builds a database from nothing but
> `alembic upgrade head` and diffs the result against `Base.metadata`, and it passes. Verify with:
>
> ```bash
> python -m pytest tests/test_migrations_replay.py -v
> ```
>
> `test_chain_has_exactly_one_head` catches a duplicate revision number;
> `test_alembic_chain_rebuilds_the_current_schema` catches a migration that has drifted from the
> model. Both must pass before this task is done.

- [ ] **Step 8: Commit** — `git commit -m "feat(models): tenant-scoped crm_connections table"`

---

## Task 4: `test_connection()` + splitting the connector globals

**Files:** Modify `nexus/ingestion/crm.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
def _fixed_response(status: int, body: dict):
    """A stand-in for HubSpotConnector._request that always answers the same way."""

    async def _req(method: str, path: str, request_body: dict | None = None):
        return status, body

    return _req


async def test_stub_connector_test_connection_ok():
    from nexus.ingestion.crm import StubCRMConnector

    res = await StubCRMConnector().test_connection()
    assert res.ok is True
    assert res.label == "stub"


async def test_salesforce_test_connection_is_honest_about_not_being_live():
    from nexus.ingestion.crm import SalesforceConnector

    res = await SalesforceConnector().test_connection()
    assert res.ok is False
    assert "not available yet" in res.detail


async def test_hubspot_test_connection_maps_statuses():
    from nexus.ingestion.crm import HubSpotConnector

    conn = HubSpotConnector(access_token="pat-x")

    conn._request = _fixed_response(200, {"portalId": 12345678})  # type: ignore[method-assign]
    ok = await conn.test_connection()
    assert ok.ok is True and "12345678" in ok.label

    conn._request = _fixed_response(401, {})  # type: ignore[method-assign]
    assert "Invalid or expired" in (await conn.test_connection()).detail

    conn._request = _fixed_response(429, {})  # type: ignore[method-assign]
    assert "rate limit" in (await conn.test_connection()).detail

    conn._request = _fixed_response(500, {})  # type: ignore[method-assign]
    assert "HTTP 500" in (await conn.test_connection()).detail


async def test_hubspot_test_connection_without_a_token():
    from nexus.ingestion.crm import HubSpotConnector

    res = await HubSpotConnector(access_token="").test_connection()
    assert res.ok is False and "No access token" in res.detail


async def test_hubspot_test_connection_never_raises():
    """A flaky CRM is a failed result, never an exception across the boundary."""
    from nexus.ingestion.crm import HubSpotConnector

    conn = HubSpotConnector(access_token="pat-x")

    async def boom(method, path, body=None):
        raise RuntimeError("socket exploded")

    conn._request = boom  # type: ignore[method-assign]
    res = await conn.test_connection()
    assert res.ok is False
    assert "socket exploded" not in res.detail  # internals never surface


async def test_hubspot_falls_back_when_account_info_is_forbidden():
    """Private apps often lack the `oauth` scope account-info needs; the fallback uses the
    companies scope we already require for syncing."""
    from nexus.ingestion.crm import HubSpotConnector

    conn = HubSpotConnector(access_token="pat-x")
    calls: list[str] = []

    async def _req(method, path, body=None):
        calls.append(path)
        return (403, {}) if path.startswith("/account-info") else (200, {"results": []})

    conn._request = _req  # type: ignore[method-assign]
    res = await conn.test_connection()
    assert res.ok is True
    assert any(p.startswith("/crm/v3/objects/companies") for p in calls)


def test_override_is_distinguishable_from_the_memoized_env_connector():
    """The bug this guards: `_connector` used to hold both the test override and the memoized env
    instance, so 'is an override installed?' was unanswerable — and per-tenant resolution would
    skip tenant credentials on any env-configured deployment."""
    from nexus.ingestion.crm import (
        StubCRMConnector, get_crm_connector, get_crm_connector_override, set_crm_connector,
    )

    set_crm_connector(None)
    assert get_crm_connector_override() is None
    memoized = get_crm_connector()
    assert memoized is get_crm_connector()
    assert get_crm_connector_override() is None  # memoized, NOT an override

    installed = StubCRMConnector()
    set_crm_connector(installed)
    assert get_crm_connector_override() is installed
    assert get_crm_connector() is installed

    set_crm_connector(None)
    assert get_crm_connector_override() is None
```

- [ ] **Step 2: Run to verify they fail** — `AttributeError: ... 'test_connection'` and
      `ImportError: cannot import name 'get_crm_connector_override'`.

- [ ] **Step 3: Add `CRMTestResult`** after the `CRMPushResult` dataclass:

```python
@dataclass(slots=True)
class CRMTestResult:
    """The outcome of verifying a credential. ``label`` is a friendly identity for the UI
    ("HubSpot portal 12345678"); ``detail`` is a human-readable reason, safe to show a user."""

    ok: bool
    label: str = ""
    detail: str = ""
```

- [ ] **Step 4: Add `test_connection` to `CRMConnector`**, before the `# -- routing` comment:

```python
    # -- connection health -------------------------------------------------------------
    async def test_connection(self) -> CRMTestResult:
        """Verify the credential works. Like every connector method, never raises across the
        boundary — a failure is a result the caller can render, not an exception."""
        return CRMTestResult(ok=True, label=self.source, detail="Offline stub connector.")
```

- [ ] **Step 5: Add to `SalesforceConnector`:**

```python
    async def test_connection(self) -> CRMTestResult:
        # There is no real Salesforce API client yet — fetch_accounts returns an injected sample.
        # Reporting a green check here would be a lie about a token that does nothing.
        return CRMTestResult(
            ok=False, label="Salesforce",
            detail="Salesforce connections are not available yet.",
        )
```

- [ ] **Step 6: Add the status map before `class HubSpotConnector`, and the method to it:**

```python
_HUBSPOT_TEST_ERRORS = {
    401: "Invalid or expired access token.",
    403: "Token is missing required scopes (crm.objects.companies.read/write).",
    429: "HubSpot rate limit reached — try again shortly.",
}
```

```python
    async def test_connection(self) -> CRMTestResult:
        """Verify the private-app token.

        Prefers ``/account-info/v3/details`` for a friendly portal label, but that endpoint needs
        the ``oauth`` scope, which many private apps do not grant. On 403 we retry against the
        companies scope we actually require for syncing, so a correctly-scoped token still
        reports connected.
        """
        if not self._token:
            return CRMTestResult(ok=False, label="HubSpot", detail="No access token configured.")
        try:
            st, body = await self._request("GET", "/account-info/v3/details")
            if st == 200:
                portal = body.get("portalId")
                return CRMTestResult(
                    ok=True,
                    label=f"HubSpot portal {portal}" if portal else "HubSpot",
                    detail="Connected.",
                )
            if st == 403:
                st_fallback, _ = await self._request("GET", "/crm/v3/objects/companies?limit=1")
                if st_fallback == 200:
                    return CRMTestResult(ok=True, label="HubSpot", detail="Connected.")
                st = st_fallback
            return CRMTestResult(
                ok=False, label="HubSpot",
                detail=_HUBSPOT_TEST_ERRORS.get(st, f"HubSpot returned HTTP {st}."),
            )
        except Exception as exc:
            # Log the cause for operators; show the user a message that cannot leak internals.
            logger.warning("[hubspot] test_connection failed: %r", exc)
            return CRMTestResult(
                ok=False, label="HubSpot", detail="Could not reach HubSpot. Try again shortly."
            )
```

- [ ] **Step 7: Replace the globals block** (from `_connector: CRMConnector | None = None` through
      the end of `set_crm_connector`) with:

```python
# The deployment-wide connector. Two *separate* globals on purpose:
#   _connector — the memoized env-configured instance (what get_crm_connector() returns)
#   _override  — an instance installed deliberately via set_crm_connector()
# They used to be one variable, which made "is an override installed?" unanswerable: after any
# call to get_crm_connector() on an env-configured deployment the single global was non-None.
# Per-tenant resolution needs that distinction — see crm_credentials.resolve_crm_connector.
_connector: CRMConnector | None = None
_override: CRMConnector | None = None
```

```python
def get_crm_connector() -> CRMConnector:
    """The deployment-wide connector: an installed override, else the env-configured one.

    This is the *fallback*. Per-tenant resolution lives in
    :func:`nexus.ingestion.crm_credentials.resolve_crm_connector`; call that from request and
    worker paths so each tenant syncs to its own CRM.
    """
    global _connector
    if _connector is None:
        _connector = build_crm_connector_from_settings()
    return _connector


def set_crm_connector(connector: CRMConnector | None) -> None:
    """Install (or clear) an explicit connector — the test seam for a recording stub.

    Sets both globals so ``get_crm_connector()`` returns it *and* per-tenant resolution knows it
    was installed deliberately. ``None`` clears both, so the next ``get_crm_connector()`` rebuilds
    from settings.
    """
    global _connector, _override
    _connector = connector
    _override = connector


def get_crm_connector_override() -> CRMConnector | None:
    """The deliberately installed connector, or ``None`` when the module has merely memoized the
    env-configured instance — the distinction ``get_crm_connector()`` cannot make."""
    return _override
```

- [ ] **Step 8: Run** — `python -m pytest tests/test_crm_connection.py tests/test_crm_push.py tests/test_crm_auto_sync.py -q`
      The latter two must pass **unedited**.
- [ ] **Step 9: Commit** — `git commit -m "feat(crm): test_connection(); split override from memoized env connector"`

---

## Task 5: Per-tenant credential store and resolution

**Files:** Create `nexus/ingestion/crm_credentials.py`

- [ ] **Step 1: Add the autouse fixture** near the top of the test file, after the imports:

```python
@pytest.fixture(autouse=True)
def no_connector_override():
    """Clear any installed connector before and after every test in this module.

    `tests/test_crm_push.py` tears down with `set_crm_connector(StubCRMConnector())` — a fresh
    stub, not None — so an override can still be installed process-wide when this module runs.
    Under resolution precedence an override wins over every tenant credential, which would make
    the fallback tests below pass vacuously against someone else's leftover stub.
    """
    from nexus.ingestion.crm import set_crm_connector

    set_crm_connector(None)
    yield
    set_crm_connector(None)
```

- [ ] **Step 2: Write the failing tests** — append:

```python
async def _store(tid: str, token: str, provider: str = "hubspot", api_base: str = ""):
    from nexus.ingestion.crm_credentials import store_credentials

    async with tenant_session(tid) as ts:
        await store_credentials(ts, provider=provider, access_token=token, api_base=api_base)


async def test_resolve_falls_back_to_env_when_tenant_has_no_credential():
    """The non-regression core: an env-only deployment behaves exactly as before."""
    from nexus.ingestion.crm import get_crm_connector
    from nexus.ingestion.crm_credentials import resolve_crm_connector

    tid = await make_tenant(slug="tf", name="F")
    async with tenant_session(tid) as ts:
        assert await resolve_crm_connector(ts) is get_crm_connector()


async def test_stored_credential_beats_env():
    from nexus.ingestion.crm import HubSpotConnector
    from nexus.ingestion.crm_credentials import resolve_crm_connector

    tid = await make_tenant(slug="ts1", name="S")
    await _store(tid, "pat-tenant")
    async with tenant_session(tid) as ts:
        conn = await resolve_crm_connector(ts)
    assert isinstance(conn, HubSpotConnector)
    assert conn.source == "hubspot"


async def test_two_tenants_resolve_to_different_connectors():
    from nexus.ingestion.crm_credentials import resolve_crm_connector

    tid_a = await make_tenant(slug="ra", name="RA")
    tid_b = await make_tenant(slug="rb", name="RB")
    await _store(tid_a, "pat-AAA")
    await _store(tid_b, "pat-BBB")

    async with tenant_session(tid_a) as ts:
        ca = await resolve_crm_connector(ts)
    async with tenant_session(tid_b) as ts:
        cb = await resolve_crm_connector(ts)

    assert ca is not cb
    assert ca._token == "pat-AAA"
    assert cb._token == "pat-BBB"


async def test_installed_override_wins_over_a_stored_credential():
    from nexus.ingestion.crm import StubCRMConnector, set_crm_connector
    from nexus.ingestion.crm_credentials import resolve_crm_connector

    tid = await make_tenant(slug="ov", name="OV")
    await _store(tid, "pat-ignored")
    installed = StubCRMConnector()
    set_crm_connector(installed)
    async with tenant_session(tid) as ts:
        assert await resolve_crm_connector(ts) is installed


async def test_resolution_caches_the_instance_but_notices_a_changed_token():
    """The cache keeps recording buffers stable across pushes; the fingerprint stops it serving a
    credential another process has since replaced."""
    from nexus.ingestion.crm_credentials import resolve_crm_connector

    tid = await make_tenant(slug="cc", name="CC")
    await _store(tid, "pat-1")
    async with tenant_session(tid) as ts:
        first = await resolve_crm_connector(ts)
        assert await resolve_crm_connector(ts) is first  # buffers survive

    await _store(tid, "pat-2")
    async with tenant_session(tid) as ts:
        rebuilt = await resolve_crm_connector(ts)
    assert rebuilt is not first
    assert rebuilt._token == "pat-2"


async def test_blank_token_keeps_the_stored_secret():
    from nexus.ingestion.crm_credentials import get_connection, store_credentials

    tid = await make_tenant(slug="bk", name="BK")
    await _store(tid, "pat-keep")
    async with tenant_session(tid) as ts:
        await store_credentials(ts, provider="hubspot", access_token=None,
                                api_base="https://eu1.hubapi.com")
        row = await get_connection(ts)
        assert unseal_crm_secret(row.secret) == {"access_token": "pat-keep"}
        assert row.api_base == "https://eu1.hubapi.com"


async def test_clearing_a_credential_falls_back_to_env():
    from nexus.ingestion.crm import get_crm_connector
    from nexus.ingestion.crm_credentials import clear_credentials, resolve_crm_connector

    tid = await make_tenant(slug="cl", name="CL")
    await _store(tid, "pat-gone")
    async with tenant_session(tid) as ts:
        assert await clear_credentials(ts) is True
        assert await resolve_crm_connector(ts) is get_crm_connector()


async def test_undecryptable_secret_falls_back_instead_of_crashing():
    """A key rotation must degrade to 'reconnect', not a 500."""
    from nexus.ingestion.crm import get_crm_connector
    from nexus.ingestion.crm_credentials import has_credentials, resolve_crm_connector
    from nexus.models.integration import CrmConnection

    tid = await make_tenant(slug="rot", name="ROT")
    async with tenant_session(tid) as ts:
        ts.add(CrmConnection(tenant_id=tid, provider="hubspot",
                             secret={"enc": "garbage-from-an-old-key"}))
        await ts.flush()

    async with tenant_session(tid) as ts:
        row = await ts.first(CrmConnection)
        assert has_credentials(row) is False
        assert await resolve_crm_connector(ts) is get_crm_connector()
```

- [ ] **Step 3: Run to verify they fail** — `ModuleNotFoundError: nexus.ingestion.crm_credentials`
- [ ] **Step 4: Create `nexus/ingestion/crm_credentials.py`**

```python
# nexus/ingestion/crm_credentials.py
"""Per-tenant CRM credentials: encrypted storage + connector resolution.

A tenant connects its own CRM by storing an access token here. The token is sealed
(:mod:`nexus.ingestion.crm_crypto`) and never leaves the server.

Resolution is layered so a deployment that only sets ``NEXUS_CRM_PROVIDER`` /
``NEXUS_HUBSPOT_ACCESS_TOKEN`` keeps behaving exactly as it did before per-tenant credentials
existed:

  1. a deliberately installed connector (``set_crm_connector`` — the test seam) wins;
  2. else the tenant's stored credential;
  3. else the env-configured connector.

Every request and worker path should call :func:`resolve_crm_connector` rather than
``get_crm_connector()``. Resolving once for a whole process — as the heartbeat sweep used to —
pushes every tenant's accounts into whichever CRM the deployment env points at.
"""
from __future__ import annotations

import logging
from collections import OrderedDict

from nexus.core.tenancy import TenantSession
from nexus.ingestion.crm import (
    CRMConnector,
    HubSpotConnector,
    get_crm_connector,
    get_crm_connector_override,
)
from nexus.ingestion.crm_crypto import seal_crm_secret, unseal_crm_secret
from nexus.models.integration import CrmConnection

logger = logging.getLogger("nexus.integrations.crm")

# Provider names the product recognises, and the subset with a working API client. Salesforce is
# known (so the UI can render it, disabled, and the model can store it later) but not live:
# SalesforceConnector.fetch_accounts returns an injected sample, so accepting a Salesforce token
# would mean storing a secret that does nothing.
KNOWN_CRM_PROVIDERS = ("hubspot", "salesforce")
LIVE_CRM_PROVIDERS = ("hubspot",)

_CACHE_MAX = 128
# tenant_id -> (fingerprint, connector).
#
# What this cache does: keeps the connector *instance* stable, so the per-instance recording
# buffers capped by CRMConnector.MAX_RECORDED_PUSHES survive across pushes instead of resetting
# on every call, and skips a decrypt + construction each time.
#
# What it does NOT do: skip the row read. Resolution reads the row every call on purpose — that
# single indexed lookup is how a worker process notices a credential the API process just
# changed. N+1 query pressure is handled by hoisting resolution out of inner loops, not here.
_TENANT_CONNECTORS: "OrderedDict[str, tuple[str, CRMConnector]]" = OrderedDict()


def invalidate_tenant_connector(tenant_id: str | None = None) -> None:
    """Drop cached connectors. ``None`` clears every tenant."""
    if tenant_id is None:
        _TENANT_CONNECTORS.clear()
    else:
        _TENANT_CONNECTORS.pop(tenant_id, None)


def _fingerprint(row: CrmConnection) -> str:
    stamp = row.updated_at.isoformat() if row.updated_at else ""
    return f"{stamp}|{row.provider}|{row.api_base}"


def has_credentials(row: CrmConnection | None) -> bool:
    """True when the row holds a secret that still decrypts to a usable token."""
    return bool(row and unseal_crm_secret(row.secret).get("access_token"))


def build_tenant_connector(provider: str, token: str, api_base: str = "") -> CRMConnector | None:
    """Build a connector from decrypted credentials, or ``None`` if we cannot honor them."""
    if provider == "hubspot" and token:
        if api_base:
            return HubSpotConnector(access_token=token, api_base=api_base)
        return HubSpotConnector(access_token=token)
    return None


async def get_connection(ts: TenantSession) -> CrmConnection | None:
    """The tenant's stored CRM connection row, if any. One row per tenant."""
    return await ts.first(CrmConnection)


async def resolve_crm_connector(ts: TenantSession) -> CRMConnector:
    """The connector this tenant's syncs must use. See the module docstring for precedence."""
    override = get_crm_connector_override()
    if override is not None:
        return override

    row = await get_connection(ts)
    if row is not None:
        fingerprint = _fingerprint(row)
        cached = _TENANT_CONNECTORS.get(ts.tenant_id)
        if cached is not None and cached[0] == fingerprint:
            _TENANT_CONNECTORS.move_to_end(ts.tenant_id)
            return cached[1]

        token = str(unseal_crm_secret(row.secret).get("access_token") or "")
        connector = build_tenant_connector(row.provider, token, row.api_base or "")
        if connector is not None:
            _TENANT_CONNECTORS[ts.tenant_id] = (fingerprint, connector)
            _TENANT_CONNECTORS.move_to_end(ts.tenant_id)
            while len(_TENANT_CONNECTORS) > _CACHE_MAX:
                _TENANT_CONNECTORS.popitem(last=False)
            return connector

        # A row we cannot honor: an unknown provider, or a secret that no longer decrypts because
        # the key rotated. Fall through to env rather than fail the sync; the connection endpoint
        # reports the row as needing reconnection.
        logger.warning(
            "[crm] tenant %s has an unusable stored credential (provider=%s)",
            ts.tenant_id, row.provider,
        )
        invalidate_tenant_connector(ts.tenant_id)

    return get_crm_connector()


async def store_credentials(
    ts: TenantSession, *, provider: str, access_token: str | None,
    api_base: str = "", actor_user_id: str | None = None,
) -> CrmConnection:
    """Upsert the tenant's CRM connection.

    ``access_token`` is write-only: a blank or omitted value keeps the stored secret, so an admin
    can change the provider or api_base without re-entering the token. Any save resets the row to
    ``unverified`` — a credential is not "connected" until a test says so.
    """
    row = await get_connection(ts)
    if row is None:
        row = CrmConnection(tenant_id=ts.tenant_id, provider=provider, secret={})
        ts.add(row)
    row.provider = provider
    row.api_base = api_base
    if access_token:
        row.secret = seal_crm_secret({"access_token": access_token})
    row.status = "unverified"
    row.verified_at = None
    row.last_error = None
    row.updated_by_user_id = actor_user_id
    await ts.flush()
    invalidate_tenant_connector(ts.tenant_id)
    return row


async def clear_credentials(ts: TenantSession) -> bool:
    """Delete the tenant's connection so it falls back to env. True when a row was removed."""
    row = await get_connection(ts)
    if row is None:
        return False
    await ts.delete(row)
    await ts.flush()
    invalidate_tenant_connector(ts.tenant_id)
    return True
```

- [ ] **Step 5: Run to verify pass**
- [ ] **Step 6: Commit** — `git commit -m "feat(crm): per-tenant credential store and layered connector resolution"`

---

## Task 6: API schemas and the four endpoints

**Files:** Modify `nexus/api/schemas.py` (after `CRMSyncStatusOut`),
`nexus/api/routers/integrations.py`

> **Written against master's `integrations.py`**, which already has `_PostedRows`, `CRM_SOURCES`,
> and the two-path `/crm/sync`. Do not reintroduce the `_CRM_CONNECTORS` dict — it was the
> HubSpot-500 bug fixed in `3ae2c1a`.

- [ ] **Step 1: Write the failing tests** — append (see the full list in spec §4):

```python
_SECRET = "pat-super-secret-value"


async def _connect(client, headers, token=_SECRET, provider="hubspot"):
    return await client.put("/api/integrations/crm/connection", headers=headers,
                            json={"provider": provider, "access_token": token})


async def test_put_then_get_never_returns_the_token(client):
    h = auth(await signup(client))
    r = await _connect(client, h)
    assert r.status_code == 200, r.text
    assert _SECRET not in r.text

    g = await client.get("/api/integrations/crm/connection", headers=h)
    assert g.status_code == 200
    assert _SECRET not in g.text          # raw body, not just the fields we thought to check
    body = g.json()
    assert body["source"] == "tenant"
    assert body["provider"] == "hubspot"
    assert body["has_credentials"] is True
    assert body["status"] == "unverified"


async def test_stored_token_is_ciphertext_in_the_database(client):
    from nexus.models.integration import CrmConnection

    token = await signup(client)
    await _connect(client, auth(token))
    async with tenant_session(principal_from_token(token).tenant_id) as ts:
        row = await ts.first(CrmConnection)
        assert _SECRET not in str(row.secret)


async def test_get_reports_env_source_when_no_tenant_credential(client):
    h = auth(await signup(client))
    g = (await client.get("/api/integrations/crm/connection", headers=h)).json()
    assert g["source"] in ("env", "none")
    assert g["has_credentials"] is False


async def test_put_rejects_unknown_and_not_live_providers(client):
    h = auth(await signup(client))
    bad = await client.put("/api/integrations/crm/connection", headers=h,
                           json={"provider": "pipedrive", "access_token": "x"})
    assert bad.status_code == 400
    assert "Unknown CRM provider" in bad.json()["detail"]

    sf = await client.put("/api/integrations/crm/connection", headers=h,
                          json={"provider": "salesforce", "access_token": "x"})
    assert sf.status_code == 400
    assert "not available yet" in sf.json()["detail"]


async def test_put_requires_a_token_on_first_connect(client):
    h = auth(await signup(client))
    r = await client.put("/api/integrations/crm/connection", headers=h,
                         json={"provider": "hubspot"})
    assert r.status_code == 400
    assert "access token is required" in r.json()["detail"].lower()


async def test_put_with_blank_token_keeps_the_stored_secret(client):
    h = auth(await signup(client))
    await _connect(client, h)
    r = await client.put("/api/integrations/crm/connection", headers=h,
                         json={"provider": "hubspot", "api_base": "https://eu1.hubapi.com"})
    assert r.status_code == 200, r.text
    assert r.json()["has_credentials"] is True
    assert r.json()["api_base"] == "https://eu1.hubapi.com"


async def test_test_endpoint_records_success(client, monkeypatch):
    from nexus.ingestion.crm import CRMTestResult, HubSpotConnector

    async def ok(self):
        return CRMTestResult(ok=True, label="HubSpot portal 42", detail="Connected.")

    monkeypatch.setattr(HubSpotConnector, "test_connection", ok)

    h = auth(await signup(client))
    await _connect(client, h)
    r = await client.post("/api/integrations/crm/connection/test", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "label": "HubSpot portal 42", "detail": "Connected."}
    assert _SECRET not in r.text

    g = (await client.get("/api/integrations/crm/connection", headers=h)).json()
    assert g["status"] == "connected"
    assert g["verified_at"] is not None
    assert g["last_error"] is None


async def test_test_endpoint_records_failure_without_raising(client, monkeypatch):
    from nexus.ingestion.crm import CRMTestResult, HubSpotConnector

    async def bad(self):
        return CRMTestResult(ok=False, label="HubSpot", detail="Invalid or expired access token.")

    monkeypatch.setattr(HubSpotConnector, "test_connection", bad)

    h = auth(await signup(client))
    await _connect(client, h)
    assert (await client.post("/api/integrations/crm/connection/test",
                              headers=h)).json()["ok"] is False

    g = (await client.get("/api/integrations/crm/connection", headers=h)).json()
    assert g["status"] == "error"
    assert g["last_error"] == "Invalid or expired access token."


async def test_delete_clears_the_connection(client):
    h = auth(await signup(client))
    await _connect(client, h)
    assert (await client.delete("/api/integrations/crm/connection", headers=h)).status_code == 204
    g = (await client.get("/api/integrations/crm/connection", headers=h)).json()
    assert g["has_credentials"] is False
    assert g["source"] in ("env", "none")


async def test_one_tenant_cannot_see_anothers_connection(client):
    h_a = auth(await signup(client, slug="ia", email="a@ia.com", company="IA"))
    h_b = auth(await signup(client, slug="ib", email="b@ib.com", company="IB"))
    await _connect(client, h_a)
    g = (await client.get("/api/integrations/crm/connection", headers=h_b)).json()
    assert g["has_credentials"] is False


async def test_sync_status_reports_the_tenants_own_provider(client):
    h = auth(await signup(client))
    assert (await client.get("/api/integrations/crm/sync-status",
                             headers=h)).json()["provider"] == "stub"
    await _connect(client, h)
    assert (await client.get("/api/integrations/crm/sync-status",
                             headers=h)).json()["provider"] == "hubspot"
```

Plus an RBAC test asserting a `rep` gets 403 on GET / PUT / POST test / DELETE. **Read
`nexus/api/routers/workspace.py`'s member-create endpoint on master and match its actual request
schema** — do not assume a `password` field exists.

- [ ] **Step 2: Run to verify they fail** (404s — endpoints don't exist)

- [ ] **Step 3: Add schemas** to `nexus/api/schemas.py` after `CRMSyncStatusOut`:

```python
# ---- integrations: per-tenant CRM connection ----
class CRMConnectionIn(BaseModel):
    """A tenant's own CRM credentials.

    ``access_token`` is write-only: omit it or send a blank string to keep the stored secret, so
    an admin can change ``api_base`` without re-entering the token.
    """

    provider: str = Field(default="hubspot", max_length=16)
    access_token: str | None = Field(default=None, max_length=512)
    api_base: str = Field(default="", max_length=255)


class CRMConnectionOut(BaseModel):
    """Everything the server will say about a CRM connection. The secret is not on this list,
    and must never be added to it."""

    provider: str                     # effective provider
    source: str                       # tenant | env | none
    has_credentials: bool = False     # a secret is stored — never the secret itself
    status: str = "none"              # none | unverified | connected | error
    api_base: str = ""
    verified_at: str | None = None
    last_error: str | None = None
    updated_at: str | None = None


class CRMConnectionTestOut(BaseModel):
    ok: bool
    label: str = ""
    detail: str = ""
```

- [ ] **Step 4: Update imports** in `integrations.py`: add the three schemas; add
      `from nexus.core.audit import audit`; add the `crm_credentials` imports
      (`KNOWN_CRM_PROVIDERS`, `LIVE_CRM_PROVIDERS`, `clear_credentials`, `get_connection`,
      `has_credentials`, `resolve_crm_connector`, `store_credentials`); add
      `from nexus.models.integration import CrmConnection`. **Keep** `get_crm_connector` — the
      400-message branch in `/crm/sync` still needs to name the env provider.

- [ ] **Step 5: Add the endpoints** after `crm_sync_status`:

```python
# ---- per-tenant CRM connection -------------------------------------------------------
# A tenant connects its own CRM here. Before this existed every tenant shared one
# deployment-wide token, so a customer could not connect their own CRM and the heartbeat
# sweep pushed every tenant's accounts to whichever portal the env pointed at.


def _connection_out(row: CrmConnection | None, *, env_provider: str) -> CRMConnectionOut:
    """Project a stored row (or the env fallback) into the response model.

    The only place connection state becomes JSON — keeping it in one function is what makes
    "the secret never leaves the server" checkable by reading a single body.
    """
    if row is not None:
        stored = has_credentials(row)
        return CRMConnectionOut(
            provider=row.provider,
            source="tenant",
            has_credentials=stored,
            # A row whose secret no longer decrypts (key rotation) is an error the admin must
            # see and act on — not silently reported as "no connection".
            status=row.status if stored else "error",
            api_base=row.api_base or "",
            verified_at=row.verified_at.isoformat() if (stored and row.verified_at) else None,
            last_error=(
                row.last_error if stored
                else "Stored credential could not be decrypted — reconnect your CRM."
            ),
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
        )
    if env_provider and env_provider != "stub":
        # Configured deployment-wide. Never tested from here, so it is not claimed as verified.
        return CRMConnectionOut(provider=env_provider, source="env", status="unverified")
    return CRMConnectionOut(provider=env_provider or "stub", source="none", status="none")


def _env_provider() -> str:
    return (get_settings().crm_provider or "stub").strip().lower()


@router.get("/crm/connection", response_model=CRMConnectionOut)
async def get_crm_connection(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> CRMConnectionOut:
    """The tenant's CRM connection state. Never includes the credential."""
    return _connection_out(await get_connection(ts), env_provider=_env_provider())


@router.put("/crm/connection", response_model=CRMConnectionOut)
async def set_crm_connection(
    body: CRMConnectionIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> CRMConnectionOut:
    """Store (or update) this tenant's CRM credentials."""
    provider = (body.provider or "").strip().lower()
    if provider not in KNOWN_CRM_PROVIDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown CRM provider '{provider}'")
    if provider not in LIVE_CRM_PROVIDERS:
        # Storing a credential we cannot actually use would be a silent no-op for the customer.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{provider.capitalize()} connections are not available yet.",
        )

    token = (body.access_token or "").strip()
    if not token and not has_credentials(await get_connection(ts)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "An access token is required to connect a CRM."
        )

    row = await store_credentials(
        ts, provider=provider, access_token=token or None,
        api_base=(body.api_base or "").strip(), actor_user_id=principal.user_id,
    )
    audit("crm.connection.set", tenant_id=ts.tenant_id, actor=principal.user_id,
          provider=provider, token_set=bool(token))
    return _connection_out(row, env_provider=_env_provider())


@router.post("/crm/connection/test", response_model=CRMConnectionTestOut)
async def test_crm_connection(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> CRMConnectionTestOut:
    """Verify the resolved connector's credentials and record the outcome on the row."""
    connector = await resolve_crm_connector(ts)
    result = await connector.test_connection()

    row = await get_connection(ts)
    if row is not None:
        if result.ok:
            row.status = "connected"
            row.verified_at = utcnow()
            row.last_error = None
        else:
            row.status = "error"
            row.last_error = (result.detail or "Connection test failed.")[:500]
        await ts.flush()

    audit("crm.connection.test", tenant_id=ts.tenant_id, actor=principal.user_id,
          provider=connector.source, ok=result.ok)
    return CRMConnectionTestOut(ok=result.ok, label=result.label, detail=result.detail)


@router.delete("/crm/connection", status_code=status.HTTP_204_NO_CONTENT)
async def clear_crm_connection(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> None:
    """Disconnect the tenant's CRM. Resolution falls back to the deployment env configuration."""
    removed = await clear_credentials(ts)
    audit("crm.connection.clear", tenant_id=ts.tenant_id, actor=principal.user_id,
          removed=removed)
```

- [ ] **Step 6: Fix `crm_sync_status`** — after the `enabled = ...` line insert:

```python
    # The tenant's own CRM when it has one — reporting the deployment-wide provider here told a
    # connected tenant the wrong thing.
    connection = await get_connection(ts)
    tenant_provider = connection.provider if has_credentials(connection) else _env_provider()
```

and change `provider=(settings.crm_provider or "stub"),` → `provider=tenant_provider,`.

- [ ] **Step 7: Convert `/crm/push`** — `connector=get_crm_connector()` →
      `connector=await resolve_crm_connector(ts)`.

- [ ] **Step 8: Convert the `/crm/sync` no-rows pull** — replace the `else` branch with:

```python
    else:
        # Pull from the CRM this *tenant* is connected to, falling back to the deployment's.
        connector = await resolve_crm_connector(ts)
        if connector.source != body.source:
            configured = _env_provider()
            connection = await get_connection(ts)
            where = (
                f"this workspace is connected to '{connection.provider}'"
                if has_credentials(connection)
                else f"NEXUS_CRM_PROVIDER is '{configured}'"
            )
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Not connected to {body.source} ({where}), so there is nothing to pull. "
                f"Post accounts to import them manually, or connect {body.source} "
                f"in Integrations.",
            )
```

- [ ] **Step 9: Run** — `python -m pytest tests/test_crm_connection.py tests/test_crm_push.py tests/test_integrations*.py -q`
- [ ] **Step 10: Commit** — `git commit -m "feat(api): per-tenant CRM connection endpoints, audited"`

---

## Task 7: The cross-tenant leak fix

**Files:** Modify `nexus/workers/tasks.py` (`:260`, `:298` and their local imports),
`nexus/plays/engine.py` (`:115,120` and its import)

- [ ] **Step 1: Write the failing test** — append:

```python
async def test_heartbeat_sweep_uses_each_tenants_own_connector(monkeypatch):
    """The bug this fixes: handle_sync_crm_due_accounts resolved ONE connector and looped every
    tenant with it, so tenant A's accounts were pushed into whichever CRM the deployment env
    pointed at. Fails against pre-change master."""
    from nexus.core.db import utcnow
    from nexus.ingestion import crm_credentials
    from nexus.ingestion.crm import StubCRMConnector
    from nexus.models.account import Account
    from nexus.models.identity import Tenant
    from nexus.workers.tasks import handle_sync_crm_due_accounts

    monkeypatch.setattr("nexus.core.config.get_settings",
                        _settings_with(crm_sync_enabled=True))

    tid_a = await make_tenant(slug="swa", name="SWA")
    tid_b = await make_tenant(slug="swb", name="SWB")
    for tid, name in ((tid_a, "Acct A"), (tid_b, "Acct B")):
        async with tenant_session(tid) as ts:
            (await ts.session.get(Tenant, tid)).automation_enabled = True
            ts.add(Account(tenant_id=tid, name=name, domain=f"{name[-1].lower()}.com"))
            await ts.flush()

    conn_a, conn_b = StubCRMConnector(), StubCRMConnector()
    by_tenant = {tid_a: conn_a, tid_b: conn_b}

    async def fake_resolve(ts):
        return by_tenant[ts.tenant_id]

    monkeypatch.setattr(crm_credentials, "resolve_crm_connector", fake_resolve)

    await handle_sync_crm_due_accounts({"now_iso": utcnow().isoformat()})

    assert [r["name"] for r in conn_a.pushed_accounts] == ["Acct A"]
    assert [r["name"] for r in conn_b.pushed_accounts] == ["Acct B"]
```

with this helper above it:

```python
def _settings_with(**overrides):
    """A get_settings() replacement that keeps the real settings but overrides a few fields."""
    from nexus.core.config import get_settings as real_get_settings

    base = real_get_settings()

    def _patched():
        return base.model_copy(update=overrides)

    return _patched
```

> `handle_sync_crm_due_accounts` imports `get_settings` and `resolve_crm_connector` *inside* the
> function, so patching the module attributes works. If the sweep returns
> `{"skipped": "crm_sync_disabled"}`, the settings patch missed — use `monkeypatch.setenv`
> plus `get_settings.cache_clear()` instead.

- [ ] **Step 2: Run to verify it fails** (both accounts on one connector, or A empty)

- [ ] **Step 3: Fix `handle_sync_crm_due_accounts`** — swap the local import to
      `from nexus.ingestion.crm_credentials import resolve_crm_connector`, delete
      `connector = get_crm_connector()` above the loop, and make the loop:

```python
    synced = 0
    for tid, account_ids in by_tenant.items():
        async with tenant_session(tid) as ts:
            # Resolve ONCE per tenant, inside that tenant's session: each tenant syncs to its own
            # CRM. Resolving once for the whole sweep — as this did before per-tenant credentials
            # — pushed every tenant's accounts into whichever portal the deployment env named.
            # Hoisted out of the account loop so a tenant with N due accounts still costs one
            # credential lookup, not N.
            connector = await resolve_crm_connector(ts)
            for aid in account_ids:
                account = await ts.get(Account, aid)
                if account is None:
                    continue
                await sync_account_to_crm(ts, account, connector=connector, now=now)
                synced += 1
```

- [ ] **Step 4: Fix `handle_sync_crm_account`** — swap the local import the same way and use
      `connector=await resolve_crm_connector(ts)`.

- [ ] **Step 5: Fix the `crm_push` play action** in `plays/engine.py` — swap the module import to
      `from nexus.ingestion.crm_credentials import resolve_crm_connector`, then:

```python
            elif atype == "crm_push":
                contacts = await ts.list(Contact, Contact.account_id == account.id)
                # Resolve once and reuse for both calls — this tenant's CRM, not the process's.
                connector = await resolve_crm_connector(ts)
                res = await connector.push_account(account, contacts=contacts)
                outcomes.append({"type": "crm_push", "ok": res.ok, "source": res.source})
                if action.get("log_activity", True):
                    await connector.push_activity(
                        account_id=account.crm_id or account.id,
                        kind="signal",
                        detail={"signal": signal.title, "play": play.name},
                    )
```

- [ ] **Step 6: Verify no production call site uses the singleton** —
      `grep -rn "get_crm_connector()" --include="*.py" nexus/`
      Expected: only inside `resolve_crm_connector` (the env fallback) and the `/crm/sync`
      400-message branch if it still reads the env provider by name.

- [ ] **Step 7: Run** — `python -m pytest tests/test_crm_connection.py tests/test_crm_push.py tests/test_crm_auto_sync.py -q`
- [ ] **Step 8: Commit** — `git commit -m "fix(crm): resolve the connector per tenant in sweep, event path, and plays"`

---

## Task 8: Frontend types and API client

**Files:** Modify `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts`

- [ ] **Step 1: Add types** after `CRMSyncStatus`:

```ts
/** A tenant's own CRM connection. The access token is never returned by the API. */
export interface CRMConnection {
  provider: string;
  /** Where the effective config comes from: the tenant's own row, deployment env, or nothing. */
  source: "tenant" | "env" | "none";
  has_credentials: boolean;
  status: "none" | "unverified" | "connected" | "error";
  api_base: string;
  verified_at: string | null;
  last_error: string | null;
  updated_at: string | null;
}

/** `access_token` is write-only: omit it to keep the stored secret. */
export interface CRMConnectionInput {
  provider: string;
  access_token?: string | null;
  api_base?: string;
}

export interface CRMConnectionTest {
  ok: boolean;
  label: string;
  detail: string;
}
```

- [ ] **Step 2: Add client methods** in the `// ---- integrations ----` block, and the three type
      imports:

```ts
  crmConnection(signal?: AbortSignal) {
    return this.request<CRMConnection>("/integrations/crm/connection", { signal });
  }
  setCrmConnection(body: CRMConnectionInput, signal?: AbortSignal) {
    return this.request<CRMConnection>("/integrations/crm/connection", {
      method: "PUT", body, signal,
    });
  }
  testCrmConnection(signal?: AbortSignal) {
    return this.request<CRMConnectionTest>("/integrations/crm/connection/test", {
      method: "POST", signal,
    });
  }
  clearCrmConnection(signal?: AbortSignal) {
    return this.request<void>("/integrations/crm/connection", { method: "DELETE", signal });
  }
```

`request` already maps an empty 204 body to `null`, so `clearCrmConnection` needs no special case.

- [ ] **Step 3: Verify** — `cd frontend && npx tsc --noEmit` (exit 0)
- [ ] **Step 4: Commit** — `git commit -m "feat(frontend): CRM connection types and API client"`

---

## Task 9: The connection form

**Files:** Modify `frontend/src/pages/IntegrationsPage.tsx`, `IntegrationsPage.module.css`

- [ ] **Step 1: Invoke `impeccable` (required by CLAUDE.md) and read `DESIGN.md`.** Use
      `ui-ux-pro-max` for palette/state decisions. Non-negotiable: semantic HTML, labelled
      controls, keyboard support, visible focus, explicit loading/empty/error states, tokens only,
      reduced-motion fallbacks.

- [ ] **Step 2: Convert `CrmCard` → `ManualImportForm` in place**
  1. Rename `function CrmCard()` → `function ManualImportForm()`.
  2. Delete its `<Card padding="lg" className={styles.card}>` wrapper and the whole
     `<div className={styles.cardHead}>…</div>` block (icon, `<h2>CRM import</h2>`, description) —
     the parent card supplies the heading now. The returned element becomes the
     `<form className={styles.form} onSubmit={onSync} noValidate>` that currently sits inside them.
  3. Keep everything else byte-for-byte: `source` state and `Select`, row state and handlers,
     `validRows`, the whole `onSync` body including `api.crmSync(...)`, the buttons, the result
     banner. The manual path must behave exactly as today — only its chrome moved.
  4. Leave `CRM_SOURCES`, `AccountRow`, `EMPTY_ROW` at module scope.

- [ ] **Step 3: Render `CrmConnectionCard` in place of `CrmCard`** in `IntegrationsPage`.

- [ ] **Step 4: Add `CrmConnectionCard`** — status badge from `source`/`status`, provider `Select`
      (Salesforce disabled), `type="password"` token `Input` with `autoComplete="off"` and a
      `••••••••  (saved)` placeholder when `has_credentials`, an `Advanced` `<details>` for
      `api_base`, inline `last_error`, and Save / Test connection / Disconnect with a `Modal`
      confirm on Disconnect. Wire with `useApi` + `refetch` after every mutation.

> **Verify component props before writing:** read `Select.tsx` (does `SelectOption` support
> `disabled`?), `Skeleton.tsx` (`height`?), `ErrorState.tsx` (`description`/`onRetry`?),
> `Badge.tsx` (is there a `warn` tone?), `Button.tsx` (is there a `danger` variant?). Adjust to
> what they actually expose. **Do not invent props.**

- [ ] **Step 5: Add CSS** for `.cardHeadText`, `.advanced`, `.summary` (with `:focus-visible`),
      `.errorNote`, `.okNote`. **Verify every token exists in `frontend/src/styles/tokens.css`
      before using it.**

- [ ] **Step 6: Verify** — `cd frontend && npx tsc --noEmit && npm run build`
- [ ] **Step 7: Render check** — start the dev server via the preview tooling (never Bash), load
      Integrations, confirm skeleton → loaded, badge reads "Not connected" on a fresh workspace,
      token field is `type="password"`, Salesforce is disabled, manual import still syncs. Check
      the console. Screenshot for the final report.
- [ ] **Step 8: Commit**

---

## Task 10: Docs and full-suite verification

- [ ] **Step 1:** Update the Migrations paragraph in `CLAUDE.md` to name `0044_crm_connections` on
      top of `0043_signal_subtype`, and add a line to the repository-layout section pointing at
      `nexus/ingestion/crm_credentials.py` as the per-tenant CRM seam.
- [ ] **Step 2: Full suite** — `python -m pytest tests/ -q` (~35 min). Read the summary line. Do
      not report success from a partial run.
- [ ] **Step 3:** `git status --porcelain` — confirm no forbidden path is staged.
- [ ] **Step 4:** `git diff master...HEAD --stat` — confirm the file list matches the table above.
- [ ] **Step 5: Commit.**

---

## Self-review notes

**Spec coverage:** §2.1 → Task 1. §2.2 → Task 3. §2.3 → Tasks 4, 5, 7. §2.4 → Task 4. §2.5 →
Task 6. §2.6 → Task 2. §3 → Tasks 8, 9. §4 → tests throughout + Task 10.

**Deliberate ordering:** the autouse `no_connector_override` fixture lands in Task 5 Step 1, before
any test depending on resolution precedence — adding it later would let the fallback tests pass
against a leftover stub from `test_crm_push.py`.

**Names used consistently throughout:** `seal_crm_secret`/`unseal_crm_secret`,
`resolve_crm_connector`, `get_connection`, `has_credentials`, `store_credentials`,
`clear_credentials`, `invalidate_tenant_connector`, `build_tenant_connector`,
`KNOWN_CRM_PROVIDERS`, `LIVE_CRM_PROVIDERS`, `get_crm_connector_override`, `CRMTestResult`,
`CRMConnectionIn/Out/TestOut`, `CrmConnection`.
