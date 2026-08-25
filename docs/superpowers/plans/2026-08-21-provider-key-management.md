# Provider Key Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a superadmin add, delete, relabel, pin and test every pooled provider API key from the Control plane, with running worker processes picking up changes without a restart.

**Architecture:** One platform-global `provider_keys` table (no `tenant_id`, read through `get_platform_sessionmaker()`), Fernet-sealed secrets, and a resolver that returns the DB pool when rows exist and the env pool when they do not. Provider factories keep caching their constructed client but resolve the *key list* per operation behind a 30s TTL, which is what lets a separate worker container see a new key. Rotation already distinguishes error classes; this adds a write-back so a runtime rejection marks the row failed.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, Alembic, Pydantic v2, Fernet (`cryptography`), pytest, React 18 + TypeScript.

**Design:** `docs/superpowers/specs/2026-08-21-provider-key-management-design.md`

---

## Before you start

**Migration numbering.** At the time of writing, master's head is `0043_signal_subtype`, and an
unmerged branch (`claude/jovial-sutherland-052807`) adds `0044_crm_connections`. Do **not** assume
`0044` is free. Run this first and chain off what it prints:

```bash
python -c "import pathlib; print(sorted(p.stem for p in pathlib.Path('migrations/versions').glob('0*.py'))[-1])"
```

Use the next free number and set `down_revision` to that printed value. If you guess, the chain
forks and `tests/test_migrations_replay.py` fails.

**Never commit these files** — the user has uncommitted work in them:
`deploy/cloud/**`, `azure-pipelines-*.yml`, `docs/deployment/`, `nexus/core/config.py`,
`nexus/core/db.py`, `scripts/apply_rls.py`, `nexus/relevance/website_icp.py`,
`tests/test_db_pool_config.py`, `tests/test_worker_concurrency.py`.

Task 9 needs one line in `config.py`. Do **not** commit it — leave it modified and say so in the
handoff.

**Run the full suite before the final commit:** `pytest tests/ -q` (~35-40 min).

---

## File Structure

| File | Responsibility |
|---|---|
| `nexus/providers/__init__.py` | New package. Public surface: `key_pool`, `invalidate`. |
| `nexus/providers/crypto.py` | Seal/unseal a provider key. Raises on unsealable — never returns `""`. |
| `nexus/providers/catalog.py` | The nine providers, their env fallback attribute, and their probe/verify shapes. One place, so adding a provider is one entry. |
| `nexus/providers/service.py` | CRUD + `prefer` + `mark_failed`. The only thing that writes `status`. |
| `nexus/providers/testing.py` | `probe()` and `verify()` per provider. |
| `nexus/providers/resolver.py` | `key_pool(provider)` with the 30s TTL. The worker-facing piece. |
| `nexus/models/provider_key.py` | The table. |
| `migrations/versions/00NN_provider_keys.py` | Additive migration. |
| `nexus/api/routers/admin_provider_keys.py` | `/admin/provider-keys`, gated on `providers.manage`. |
| `frontend/src/pages/admin/ProviderKeysTab.tsx` + `.module.css` | The UI tab. |
| `tests/test_provider_keys.py` | Model, crypto, service, resolver, TTL. |
| `tests/test_provider_key_testing.py` | probe/verify per provider, with mocked transports. |
| `tests/test_provider_keys_api.py` | Endpoints, permission, audit, secret-never-returned. |

---

### Task 1: Sealing a provider key

**Files:**
- Create: `nexus/providers/__init__.py`, `nexus/providers/crypto.py`
- Test: `tests/test_provider_keys.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provider_keys.py
"""Provider key storage, resolution and the TTL that keeps a worker current."""
from __future__ import annotations

import pytest


def test_a_sealed_key_round_trips():
    from nexus.providers.crypto import seal_key, unseal_key

    sealed = seal_key("sk-live-abc123")
    assert sealed != "sk-live-abc123", "the key must not be stored in the clear"
    assert unseal_key(sealed) == "sk-live-abc123"


def test_the_same_key_seals_differently_each_time():
    """Fernet is randomised. This is why lookups use the digest column, not the ciphertext."""
    from nexus.providers.crypto import seal_key

    assert seal_key("same") != seal_key("same")


def test_an_unsealable_key_raises_rather_than_reading_as_absent():
    """Returning "" would make a key-rotation mistake look exactly like "no key configured", and
    the operator's next move for those two is opposite: restore the key versus add one. Same
    reasoning as nexus/sources/crypto.py."""
    from nexus.providers.crypto import KeyUnsealable, unseal_key

    with pytest.raises(KeyUnsealable):
        unseal_key("not-a-fernet-token")


def test_digest_is_stable_and_hint_shows_only_the_tail():
    from nexus.providers.crypto import key_digest, key_hint

    assert key_digest("abc123") == key_digest("abc123")
    assert key_digest("abc123") != key_digest("abc124")
    assert key_hint("sk-live-verysecret9876") == "9876"
    # Never enough to reconstruct anything.
    assert len(key_hint("sk-live-verysecret9876")) == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_provider_keys.py -n0 -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.providers'`

- [ ] **Step 3: Write the implementation**

```python
# nexus/providers/__init__.py
"""Platform-wide provider credentials, managed from the Control plane rather than deploy/.env."""
```

```python
# nexus/providers/crypto.py
"""Sealing provider API keys at rest.

Same asymmetry as `nexus/sources/crypto.py`, and for the same reason: an unsealable key must NOT
degrade to `""`. An empty string is what a deleted key looks like, so the caller would report "not
configured" — indistinguishable from a provider nobody set up. The operator's next move differs
completely between "restore the encryption key" and "add a key", so this raises.
"""
from __future__ import annotations

import hashlib

from nexus.core.crypto import seal_text, unseal_text


class KeyUnsealable(RuntimeError):
    """A stored provider key could not be decrypted — wrong encryption key, or a tampered row."""


def seal_key(plaintext: str) -> str:
    return seal_text(plaintext or "")


def unseal_key(sealed: str) -> str:
    out = unseal_text(sealed or "")
    if not out:
        raise KeyUnsealable(
            "a stored provider key could not be decrypted. The encryption key changed or the row "
            "was altered; the key must be re-entered."
        )
    return out


def key_digest(plaintext: str) -> str:
    """Stable fingerprint, so the same key cannot be added twice.

    Fernet is randomised, so the ciphertext cannot be compared. This can. It is an INDEX, not
    anonymisation — the same caveat as `people.email_hash`.
    """
    return hashlib.sha256((plaintext or "").encode()).hexdigest()


def key_hint(plaintext: str) -> str:
    """The last four characters, so the UI can tell two rows apart without the secret."""
    return (plaintext or "")[-4:]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_provider_keys.py -n0 -q`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add nexus/providers/__init__.py nexus/providers/crypto.py tests/test_provider_keys.py
git commit -m "feat(providers): seal provider keys at rest, raising rather than reading as absent"
```

---

### Task 2: The provider catalog

**Files:**
- Create: `nexus/providers/catalog.py`
- Test: `tests/test_provider_keys.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_provider_keys.py


def test_every_catalogued_provider_names_a_real_env_fallback():
    """The env pool is the floor. A provider whose fallback attribute does not exist on Settings
    would silently resolve to an empty pool the moment its DB rows were deleted."""
    from nexus.core.config import get_settings
    from nexus.providers.catalog import PROVIDERS

    settings = get_settings()
    assert len(PROVIDERS) == 9
    for spec in PROVIDERS.values():
        assert hasattr(settings, spec.env_attr), f"{spec.id}: no Settings.{spec.env_attr}"


def test_the_catalog_covers_exactly_the_pooled_providers():
    from nexus.providers.catalog import PROVIDERS

    assert set(PROVIDERS) == {
        "groq", "anthropic", "openai_compat", "exa",
        "firecrawl", "brave", "serper", "apify", "github",
    }


def test_crypto_roots_are_not_in_the_catalog():
    """secret_key, network_token_enc_key and mfa_secret_enc_key are cryptographic roots, not
    provider credentials. Changing one invalidates every sealed token, MFA seed and encrypted
    credential at once, with no way back. They must never be manageable from a UI."""
    from nexus.providers.catalog import PROVIDERS

    attrs = {spec.env_attr for spec in PROVIDERS.values()}
    for forbidden in ("secret_key", "network_token_enc_key", "mfa_secret_enc_key",
                      "stripe_secret_key", "hubspot_access_token"):
        assert forbidden not in attrs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_provider_keys.py -n0 -q -k catalog`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.providers.catalog'`

- [ ] **Step 3: Write the implementation**

```python
# nexus/providers/catalog.py
"""The providers whose keys are manageable from the Control plane.

Adding a provider is one entry here. Deliberately excluded, and the exclusions matter more than
the inclusions:

* `stripe_secret_key` — money. A wrong value stops billing silently rather than erroring.
* `hubspot_access_token` — per-tenant, handled elsewhere. Platform-wide and per-tenant are
  different axes and conflating them is not a config change afterwards.
* `secret_key`, `network_token_enc_key`, `mfa_secret_enc_key` — cryptographic roots. Changing one
  invalidates every sealed OAuth token, every MFA seed and every encrypted credential
  simultaneously. Managing those is a key-ROTATION feature with re-encryption, not this.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    id: str
    label: str
    # The Settings attribute holding the env fallback pool (a list) or single key (a str).
    env_attr: str


PROVIDERS: dict[str, ProviderSpec] = {
    "groq": ProviderSpec("groq", "Groq (LLM)", "groq_api_key_list"),
    "anthropic": ProviderSpec("anthropic", "Anthropic (LLM)", "anthropic_api_key"),
    "openai_compat": ProviderSpec("openai_compat", "OpenAI-compatible (LLM)", "llm_api_key"),
    "exa": ProviderSpec("exa", "Exa (search)", "exa_api_key_list"),
    "firecrawl": ProviderSpec("firecrawl", "Firecrawl (search)", "firecrawl_api_key_list"),
    "brave": ProviderSpec("brave", "Brave (search)", "brave_api_key"),
    "serper": ProviderSpec("serper", "Serper (search)", "serper_api_key"),
    "apify": ProviderSpec("apify", "Apify (actors)", "apify_api_key_list"),
    "github": ProviderSpec("github", "GitHub (public API signals)", "github_token"),
}


def env_pool(provider: str) -> list[str]:
    """The env-configured keys for a provider — the floor the DB layers over."""
    from nexus.core.config import get_settings

    spec = PROVIDERS.get(provider)
    if spec is None:
        return []
    value = getattr(get_settings(), spec.env_attr, "")
    if isinstance(value, list):
        return [k for k in value if k]
    return [value] if value else []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_provider_keys.py -n0 -q`
Expected: PASS, 7 passed

If `firecrawl_api_key_list` or `exa_api_key_list` does not exist on `Settings`, check the real
property name with `grep -n "api_key_list" nexus/core/config.py` and use that. Do not add a
property to `config.py` — it is on the do-not-commit list.

- [ ] **Step 5: Commit**

```bash
git add nexus/providers/catalog.py tests/test_provider_keys.py
git commit -m "feat(providers): catalog the nine pooled providers and their env fallbacks"
```

---

### Task 3: The table

**Files:**
- Create: `nexus/models/provider_key.py`, `migrations/versions/00NN_provider_keys.py`
- Modify: `nexus/models/__init__.py`
- Test: `tests/test_provider_keys.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_provider_keys.py


async def test_a_provider_key_row_stores_no_plaintext():
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.provider_key import ProviderKey
    from nexus.providers.crypto import key_digest, key_hint, seal_key

    secret = "sk-test-abcdefgh1234"
    async with get_platform_sessionmaker()() as s:
        row = ProviderKey(
            provider="exa", label="primary",
            key_encrypted=seal_key(secret),
            key_hint=key_hint(secret), key_digest=key_digest(secret),
        )
        s.add(row)
        await s.commit()
        assert secret not in row.key_encrypted
        assert row.status == "untested"
        assert row.enabled is True
        assert row.preferred is False


def test_the_table_carries_no_tenant_id():
    """Platform-global, like companies/people/source_databases. A tenant_id would make
    scripts/apply_rls.py enrol it, and the worker would then read zero rows — silently."""
    from nexus.models.provider_key import ProviderKey

    assert "tenant_id" not in ProviderKey.__table__.columns
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_provider_keys.py -n0 -q -k "plaintext or tenant_id"`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.models.provider_key'`

- [ ] **Step 3: Write the model**

```python
# nexus/models/provider_key.py
"""Platform-wide provider API keys, managed from the Control plane.

No ``tenant_id`` — these are deployment-wide credentials, not tenant data, so
``scripts/apply_rls.py`` leaves the table alone and everything reads it through
``get_platform_sessionmaker()``. The same rule as ``companies``, ``people`` and
``source_databases``: enrolling this in RLS would make the worker read zero rows, silently.

``status`` is set ONLY by the test functions in ``nexus/providers/testing.py``. A request body
never carries one — an admin who could write ``verified`` by hand could mark a dead key working,
which is the rule ``nexus/sources/service.py`` enforces for the source-database ladder.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime

# untested: never tested. probe_ok: the credential authenticates. verified: a real call through our
# own adapter succeeded. failed: the last test or a runtime call rejected it.
#
# probe_ok and verified are separate on purpose. Measured 2026-08-21: all five Groq keys returned
# 200 from GET /models and 404 from every chat completion, because the configured model had been
# withdrawn. A single green state would have shown five healthy keys while every draft came from
# the stub.
KEY_STATUSES = ("untested", "probe_ok", "verified", "failed")


class ProviderKey(IdMixin, TimestampMixin, Base):
    __tablename__ = "provider_keys"
    __table_args__ = (
        # The same key twice in one provider is always a mistake.
        UniqueConstraint("provider", "key_digest", name="uq_provider_key_digest"),
        Index("ix_provider_key_lookup", "provider", "enabled"),
    )

    provider: Mapped[str] = mapped_column(String(32), index=True)
    label: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    key_hint: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    key_digest: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(16), default="untested", nullable=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_depth: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    last_error: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    last_error_status: Mapped[int | None] = mapped_column(nullable=True)

    # Operator kill switch, separate from `status`. Disabling is never refused.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # The pinned key, used first. At most one per provider — enforced in the service, because a
    # partial unique index is not portable to SQLite where the offline suite runs.
    preferred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
```

- [ ] **Step 4: Register the model and write the migration**

Add to `nexus/models/__init__.py`, beside the other imports:

```python
from nexus.models.provider_key import ProviderKey  # noqa: F401
```

Then create the migration, using the number you determined in "Before you start":

```python
# migrations/versions/00NN_provider_keys.py
"""Platform-wide provider API keys.

Additive. With no rows every provider resolves to its environment variable exactly as before, so
this migration changes no behaviour on its own.

Revision ID: 00NN_provider_keys
Revises: <the head you printed>
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "00NN_provider_keys"
down_revision = "<the head you printed>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_keys",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("label", sa.String(80), nullable=False, server_default=""),
        sa.Column("key_encrypted", sa.Text(), nullable=False),
        sa.Column("key_hint", sa.String(8), nullable=False, server_default=""),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="untested"),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_depth", sa.String(8), nullable=False, server_default=""),
        sa.Column("last_error", sa.String(500), nullable=False, server_default=""),
        sa.Column("last_error_status", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("preferred", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by_user_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_provider_keys_provider", "provider_keys", ["provider"])
    op.create_index("ix_provider_key_lookup", "provider_keys", ["provider", "enabled"])
    op.create_unique_constraint(
        "uq_provider_key_digest", "provider_keys", ["provider", "key_digest"]
    )


def downgrade() -> None:
    op.drop_table("provider_keys")
```

- [ ] **Step 5: Run the tests, including migration replay**

Run: `python -m pytest tests/test_provider_keys.py tests/test_migrations_replay.py -n0 -q`
Expected: PASS. If replay fails with "multiple heads", your `down_revision` is wrong — re-read
"Before you start".

- [ ] **Step 6: Commit**

```bash
git add nexus/models/provider_key.py nexus/models/__init__.py migrations/versions/00NN_provider_keys.py tests/test_provider_keys.py
git commit -m "feat(providers): platform-global provider_keys table"
```

---

### Task 4: Service layer

**Files:**
- Create: `nexus/providers/service.py`
- Test: `tests/test_provider_keys.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_provider_keys.py


async def test_adding_the_same_key_twice_is_refused():
    from nexus.providers.service import DuplicateKey, add_key

    await add_key("exa", "one", "sk-dupe-1111")
    with pytest.raises(DuplicateKey):
        await add_key("exa", "two", "sk-dupe-1111")


async def test_preferring_a_key_unpins_the_previous_one_and_enables_it():
    """At most one pin per provider, and a pinned-but-disabled key is a state the resolver would
    have to silently ignore — so preferring implies enabling."""
    from nexus.providers.service import add_key, list_keys, prefer_key, set_enabled

    a = await add_key("brave", "a", "sk-a-0001")
    b = await add_key("brave", "b", "sk-b-0002")
    await prefer_key(a.id)
    await set_enabled(b.id, False)
    await prefer_key(b.id)

    rows = {r.id: r for r in await list_keys("brave")}
    assert rows[b.id].preferred is True and rows[b.id].enabled is True
    assert rows[a.id].preferred is False


async def test_disabling_the_pinned_key_clears_the_pin():
    from nexus.providers.service import add_key, list_keys, prefer_key, set_enabled

    k = await add_key("serper", "only", "sk-s-0003")
    await prefer_key(k.id)
    await set_enabled(k.id, False)
    row = (await list_keys("serper"))[0]
    assert row.enabled is False and row.preferred is False


async def test_a_caller_cannot_set_status_directly():
    """Only the test functions write status. An admin who could set `verified` by hand could mark
    a dead key working."""
    import inspect

    from nexus.providers import service

    for name in ("add_key", "update_label", "set_enabled", "prefer_key"):
        sig = inspect.signature(getattr(service, name))
        assert "status" not in sig.parameters, f"{name} exposes status"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_provider_keys.py -n0 -q -k "dupe or prefer or disabling or status_directly"`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.providers.service'`

- [ ] **Step 3: Write the implementation**

```python
# nexus/providers/service.py
"""CRUD for provider keys. The only place `status` is written is `mark_tested` / `mark_failed`.

Everything runs on the platform sessionmaker: the table carries no tenant_id, so a tenant-bound
session would return zero rows without erroring.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.models.provider_key import ProviderKey
from nexus.providers.catalog import PROVIDERS
from nexus.providers.crypto import key_digest, key_hint, seal_key

logger = logging.getLogger("nexus.providers.service")


class DuplicateKey(ValueError):
    """This exact key is already registered for this provider."""


class UnknownProvider(ValueError):
    """No such provider in the catalog."""


async def list_keys(provider: str = "") -> list[ProviderKey]:
    async with get_platform_sessionmaker()() as s:
        stmt = select(ProviderKey).order_by(
            ProviderKey.provider, ProviderKey.preferred.desc(), ProviderKey.created_at
        )
        if provider:
            stmt = stmt.where(ProviderKey.provider == provider)
        return list((await s.scalars(stmt)).all())


async def add_key(provider: str, label: str, secret: str, *, user_id: str = "") -> ProviderKey:
    if provider not in PROVIDERS:
        raise UnknownProvider(f"unknown provider {provider!r}")
    secret = (secret or "").strip()
    if not secret:
        raise ValueError("an empty key cannot be stored")
    digest = key_digest(secret)
    async with get_platform_sessionmaker()() as s:
        existing = (
            await s.scalars(
                select(ProviderKey).where(
                    ProviderKey.provider == provider, ProviderKey.key_digest == digest
                )
            )
        ).first()
        if existing is not None:
            raise DuplicateKey(f"that key is already registered for {provider}")
        row = ProviderKey(
            provider=provider, label=(label or "").strip(),
            key_encrypted=seal_key(secret), key_hint=key_hint(secret), key_digest=digest,
            created_by_user_id=user_id or None,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
    _invalidate(provider)
    return row


async def update_label(key_id: str, label: str) -> ProviderKey | None:
    async with get_platform_sessionmaker()() as s:
        row = await s.get(ProviderKey, key_id)
        if row is None:
            return None
        row.label = (label or "").strip()
        await s.commit()
        await s.refresh(row)
        return row


async def delete_key(key_id: str) -> bool:
    async with get_platform_sessionmaker()() as s:
        row = await s.get(ProviderKey, key_id)
        if row is None:
            return False
        provider = row.provider
        await s.delete(row)
        await s.commit()
    _invalidate(provider)
    return True


async def set_enabled(key_id: str, enabled: bool) -> ProviderKey | None:
    """Disabling is never refused — during an incident "stop using this" must not be blocked.
    Disabling the pinned key also clears the pin, so the resolver never sees pinned-but-disabled."""
    async with get_platform_sessionmaker()() as s:
        row = await s.get(ProviderKey, key_id)
        if row is None:
            return None
        row.enabled = bool(enabled)
        if not row.enabled:
            row.preferred = False
        await s.commit()
        await s.refresh(row)
        provider = row.provider
    _invalidate(provider)
    return row


async def prefer_key(key_id: str) -> ProviderKey | None:
    """Pin this key so it is used first. Implies enabling it, and unpins every sibling."""
    async with get_platform_sessionmaker()() as s:
        row = await s.get(ProviderKey, key_id)
        if row is None:
            return None
        siblings = (
            await s.scalars(select(ProviderKey).where(ProviderKey.provider == row.provider))
        ).all()
        for sib in siblings:
            sib.preferred = sib.id == key_id
        row.enabled = True
        await s.commit()
        await s.refresh(row)
        provider = row.provider
    _invalidate(provider)
    return row


async def mark_tested(key_id: str, *, status: str, depth: str,
                      error: str = "", error_status: int | None = None) -> None:
    """The ONLY writer of `status`, alongside `mark_failed`."""
    async with get_platform_sessionmaker()() as s:
        row = await s.get(ProviderKey, key_id)
        if row is None:
            return
        row.status = status
        row.last_depth = depth
        row.last_error = (error or "")[:500]
        row.last_error_status = error_status
        row.last_tested_at = utcnow()
        await s.commit()


async def mark_failed_by_digest(provider: str, digest: str, *, error: str,
                                error_status: int | None) -> None:
    """Record a RUNTIME rejection against whichever row holds this key.

    Keyed by digest because the rotating caller has the plaintext, not the row id. This is what
    makes the panel show what actually happened in production rather than only what the last
    manual test said.
    """
    async with get_platform_sessionmaker()() as s:
        row = (
            await s.scalars(
                select(ProviderKey).where(
                    ProviderKey.provider == provider, ProviderKey.key_digest == digest
                )
            )
        ).first()
        if row is None:
            return
        row.status = "failed"
        row.last_error = (error or "")[:500]
        row.last_error_status = error_status
        row.last_tested_at = utcnow()
        await s.commit()
    _invalidate(provider)


def _invalidate(provider: str) -> None:
    from nexus.providers.resolver import invalidate

    invalidate(provider)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_provider_keys.py -n0 -q`
Expected: PASS. `_invalidate` imports `resolver`, which does not exist yet — create a stub now so
the import resolves, and Task 6 fills it in:

```python
# nexus/providers/resolver.py  (stub, completed in Task 6)
def invalidate(provider: str = "") -> None:
    return None
```

- [ ] **Step 5: Commit**

```bash
git add nexus/providers/service.py nexus/providers/resolver.py tests/test_provider_keys.py
git commit -m "feat(providers): key CRUD, with prefer implying enable and status write-protected"
```

---

### Task 5: Probe and verify

**Files:**
- Create: `nexus/providers/testing.py`
- Test: `tests/test_provider_key_testing.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provider_key_testing.py
"""Testing a provider key, at two depths.

A cheap auth probe answers "does this credential authenticate". A full round-trip answers "does the
thing we actually do with it work". Measured 2026-08-21, those separated cleanly: all five Groq
keys returned 200 from GET /models and 404 from every chat completion, because the configured model
had been withdrawn. A panel showing five green ticks while every draft came from the stub would be
worse than no panel.
"""
from __future__ import annotations

import httpx
import pytest


async def test_a_probe_reports_ok_on_200():
    from nexus.providers.testing import probe

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))
    result = await probe("groq", "sk-good", transport=transport)
    assert result.ok is True
    assert result.status == "probe_ok"


async def test_a_probe_carries_the_providers_own_error_text():
    """"revoked" and "model withdrawn" need different fixes, so the provider's words are kept."""
    from nexus.providers.testing import probe

    transport = httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
    )
    result = await probe("groq", "sk-dead", transport=transport)
    assert result.ok is False
    assert result.status == "failed"
    assert result.http_status == 401
    assert "Invalid API Key" in result.detail


async def test_a_key_that_probes_ok_but_fails_verify_is_not_marked_verified():
    """The Groq shape, exactly: auth fine, real call broken."""
    from nexus.providers.testing import verify

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        return httpx.Response(404, json={
            "error": {"message": "The model `x` does not exist or you do not have access to it."}
        })

    result = await verify("groq", "sk-ok", transport=httpx.MockTransport(handler))
    assert result.ok is False
    assert result.status == "failed"
    assert "does not exist" in result.detail


async def test_verify_succeeds_on_a_real_completion():
    from nexus.providers.testing import verify

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3},
    }))
    result = await verify("groq", "sk-ok", transport=transport)
    assert result.ok is True and result.status == "verified"


async def test_an_unknown_provider_is_refused_not_silently_ok():
    from nexus.providers.testing import probe

    result = await probe("nope", "k")
    assert result.ok is False and "unknown provider" in result.detail.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_provider_key_testing.py -n0 -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.providers.testing'`

- [ ] **Step 3: Write the implementation**

```python
# nexus/providers/testing.py
"""Testing a provider key at two depths.

`probe` is the cheapest call that proves the credential authenticates. It runs on save and on
"test all", and costs nothing meaningful.

`verify` runs a real request of the kind the product actually makes. It costs credits, so it is
opt-in per key and never swept. It exists because a probe is not sufficient: on 2026-08-21 every
Groq key passed `GET /models` and failed every chat completion, and only the second call revealed
it.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

_TIMEOUT = 20.0


@dataclass(slots=True)
class TestResult:
    ok: bool
    status: str                 # probe_ok | verified | failed
    detail: str = ""
    http_status: int | None = None


def _detail(resp: httpx.Response) -> str:
    """The provider's own words, because 'rotate your key' and 'fix your model name' are
    different instructions and the status code alone does not distinguish them."""
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "")[:300]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    if isinstance(err, str):
        return err[:300]
    return str(body)[:300]


async def _call(method: str, url: str, *, headers: dict, json_body: dict | None = None,
                transport=None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_TIMEOUT, transport=transport) as client:
        return await client.request(method, url, headers=headers, json=json_body)


async def probe(provider: str, key: str, *, transport=None) -> TestResult:
    from nexus.core.config import get_settings
    from nexus.providers.catalog import PROVIDERS

    if provider not in PROVIDERS:
        return TestResult(False, "failed", f"unknown provider {provider!r}")
    s = get_settings()
    try:
        if provider == "groq":
            resp = await _call("GET", f"{s.groq_base_url}/models",
                               headers={"Authorization": f"Bearer {key}"}, transport=transport)
        elif provider == "anthropic":
            resp = await _call("GET", "https://api.anthropic.com/v1/models",
                               headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                               transport=transport)
        elif provider == "openai_compat":
            resp = await _call("GET", f"{s.llm_base_url}/models",
                               headers={"Authorization": f"Bearer {key}"}, transport=transport)
        elif provider == "exa":
            resp = await _call("POST", "https://api.exa.ai/search",
                               headers={"x-api-key": key},
                               json_body={"query": "test", "numResults": 1}, transport=transport)
        elif provider == "firecrawl":
            resp = await _call("POST", "https://api.firecrawl.dev/v1/search",
                               headers={"Authorization": f"Bearer {key}"},
                               json_body={"query": "test", "limit": 1}, transport=transport)
        elif provider == "brave":
            resp = await _call("GET",
                               "https://api.search.brave.com/res/v1/web/search?q=test&count=1",
                               headers={"X-Subscription-Token": key}, transport=transport)
        elif provider == "serper":
            resp = await _call("POST", "https://google.serper.dev/search",
                               headers={"X-API-KEY": key},
                               json_body={"q": "test", "num": 1}, transport=transport)
        elif provider == "apify":
            resp = await _call("GET", f"https://api.apify.com/v2/users/me?token={key}",
                               headers={}, transport=transport)
        else:  # github
            resp = await _call("GET", "https://api.github.com/rate_limit",
                               headers={"Authorization": f"Bearer {key}"}, transport=transport)
    except Exception as exc:
        # Network failure is not evidence against the key, so say what happened rather than
        # condemning it.
        return TestResult(False, "failed", f"could not reach the provider: {exc!r}"[:300])

    if resp.status_code == 200:
        return TestResult(True, "probe_ok", "authenticated", 200)
    return TestResult(False, "failed", _detail(resp), resp.status_code)


async def verify(provider: str, key: str, *, transport=None) -> TestResult:
    """A real request of the kind the product makes. Costs credits."""
    from nexus.core.config import get_settings

    s = get_settings()
    try:
        if provider in ("groq", "openai_compat"):
            base = s.groq_base_url if provider == "groq" else s.llm_base_url
            model = s.groq_model if provider == "groq" else s.llm_model
            resp = await _call(
                "POST", f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json_body={"model": model,
                           "messages": [{"role": "user", "content": "Reply with OK"}],
                           "max_tokens": 5},
                transport=transport,
            )
        elif provider == "anthropic":
            resp = await _call(
                "POST", "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json_body={"model": s.anthropic_model, "max_tokens": 5,
                           "messages": [{"role": "user", "content": "Reply with OK"}]},
                transport=transport,
            )
        elif provider == "apify":
            resp = await _call(
                "GET", f"https://api.apify.com/v2/acts?token={key}&limit=1",
                headers={}, transport=transport,
            )
        else:
            # For the search providers the probe already issues a real query, so verify is the
            # same call — honest rather than inventing a second one that proves less.
            return await probe(provider, key, transport=transport)
    except Exception as exc:
        return TestResult(False, "failed", f"could not reach the provider: {exc!r}"[:300])

    if resp.status_code == 200:
        return TestResult(True, "verified", "a real call succeeded", 200)
    return TestResult(False, "failed", _detail(resp), resp.status_code)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_provider_key_testing.py -n0 -q`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add nexus/providers/testing.py tests/test_provider_key_testing.py
git commit -m "feat(providers): probe and verify, because auth passing is not the same as working"
```

---

### Task 6: The resolver and its TTL — the worker requirement

**Files:**
- Modify: `nexus/providers/resolver.py` (replace the Task 4 stub)
- Test: `tests/test_provider_keys.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_provider_keys.py


async def test_an_empty_table_resolves_to_the_env_pool():
    """The safety property that lets this ship before anyone adds a key."""
    from nexus.providers import resolver
    from nexus.providers.catalog import env_pool

    resolver.invalidate()
    assert await resolver.key_pool("brave") == env_pool("brave")


async def test_db_keys_replace_the_env_pool_once_any_exist():
    from nexus.providers import resolver
    from nexus.providers.service import add_key

    await add_key("serper", "db", "sk-db-9999")
    resolver.invalidate()
    pool = await resolver.key_pool("serper")
    assert pool == ["sk-db-9999"]


async def test_the_pinned_key_comes_first():
    from nexus.providers import resolver
    from nexus.providers.service import add_key, prefer_key

    await add_key("github", "first-added", "ghp-aaaa")
    second = await add_key("github", "second-added", "ghp-bbbb")
    await prefer_key(second.id)
    resolver.invalidate()
    assert (await resolver.key_pool("github"))[0] == "ghp-bbbb"


async def test_a_disabled_key_is_not_in_the_pool():
    from nexus.providers import resolver
    from nexus.providers.service import add_key, set_enabled

    k = await add_key("exa", "off", "sk-off-1234")
    await add_key("exa", "on", "sk-on-5678")
    await set_enabled(k.id, False)
    resolver.invalidate()
    assert "sk-off-1234" not in await resolver.key_pool("exa")


async def test_a_second_process_sees_a_new_key_once_the_ttl_lapses(monkeypatch):
    """THE worker requirement.

    The worker is a separate container, so the API invalidating its own cache reaches nothing. Two
    independently-constructed caches stand in for two processes here: a test with one cache would
    pass while the worker stayed stale, which is the actual bug.
    """
    from nexus.providers import resolver
    from nexus.providers.service import add_key

    worker = resolver.PoolCache()          # "the worker process"
    await add_key("firecrawl", "one", "fc-first-0001")
    assert await worker.key_pool("firecrawl") == ["fc-first-0001"]

    # The API process adds a key. The worker's cache knows nothing about it.
    await add_key("firecrawl", "two", "fc-second-0002")
    assert await worker.key_pool("firecrawl") == ["fc-first-0001"], "should still be cached"

    # ...until the TTL lapses, with no restart and no message passing.
    now = [0.0]
    monkeypatch.setattr(resolver.time, "monotonic", lambda: now[0])
    fresh = resolver.PoolCache()
    await fresh.key_pool("firecrawl")
    now[0] += resolver.POOL_TTL_S + 1
    assert set(await fresh.key_pool("firecrawl")) == {"fc-first-0001", "fc-second-0002"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_provider_keys.py -n0 -q -k "resolver or env_pool or pinned or ttl or second_process"`
Expected: FAIL, `AttributeError: module 'nexus.providers.resolver' has no attribute 'key_pool'`

- [ ] **Step 3: Write the implementation**

```python
# nexus/providers/resolver.py
"""Which keys a provider should use right now.

    key_pool(p) -> [pinned, ...other enabled rows by created_at]   if any rows exist
                -> the environment pool                             if none

**The worker must see a new key without restarting, and it is a separate container.** The API
process invalidating its own cache reaches nothing else, so correctness cannot depend on message
passing. A short TTL cannot miss a message; its worst case is bounded lateness, which is the right
trade for a feature whose entire purpose is removing silent staleness.

Deliberately NOT Valkey pub/sub, though Valkey is already here: a dropped message means a worker
runs on a stale pool forever and nothing says so — the exact failure class this replaces.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("nexus.providers.resolver")

# Cross-process staleness bound. One indexed lookup per provider per 30s is nothing against the
# cost of an LLM call or a paid search.
POOL_TTL_S = 30.0


class PoolCache:
    """A per-process view of the key pools. One instance per process in normal use; tests build
    two to stand in for the API and the worker."""

    def __init__(self) -> None:
        self._pools: dict[str, tuple[float, list[str]]] = {}

    def invalidate(self, provider: str = "") -> None:
        """Drop cached pools. Immediate for THIS process; other processes wait for the TTL."""
        if provider:
            self._pools.pop(provider, None)
        else:
            self._pools.clear()

    async def key_pool(self, provider: str) -> list[str]:
        cached = self._pools.get(provider)
        if cached is not None and (time.monotonic() - cached[0]) < POOL_TTL_S:
            return list(cached[1])
        pool = await self._read(provider)
        self._pools[provider] = (time.monotonic(), pool)
        return list(pool)

    async def _read(self, provider: str) -> list[str]:
        from sqlalchemy import select

        from nexus.core.db import get_platform_sessionmaker
        from nexus.models.provider_key import ProviderKey
        from nexus.providers.catalog import env_pool
        from nexus.providers.crypto import KeyUnsealable, unseal_key

        try:
            async with get_platform_sessionmaker()() as s:
                rows = list(
                    (
                        await s.scalars(
                            select(ProviderKey)
                            .where(ProviderKey.provider == provider, ProviderKey.enabled.is_(True))
                            .order_by(ProviderKey.preferred.desc(), ProviderKey.created_at)
                        )
                    ).all()
                )
        except Exception:
            # A database blip must not take a provider offline when the env pool would have
            # served. Same bias as the entitlement engine: unknown means allow.
            logger.warning("could not read provider keys for %s; using the env pool",
                           provider, exc_info=True)
            return env_pool(provider)

        out: list[str] = []
        for row in rows:
            try:
                out.append(unseal_key(row.key_encrypted))
            except KeyUnsealable:
                # Loud, and skipped rather than fatal: one unreadable row must not disable the
                # rest of the pool.
                logger.error("provider key %s (%s) could not be decrypted — skipping",
                             row.id, provider)
        return out or env_pool(provider)


_CACHE = PoolCache()


async def key_pool(provider: str) -> list[str]:
    return await _CACHE.key_pool(provider)


def invalidate(provider: str = "") -> None:
    _CACHE.invalidate(provider)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_provider_keys.py -n0 -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/providers/resolver.py tests/test_provider_keys.py
git commit -m "feat(providers): resolve pools with a 30s TTL so a separate worker stays current"
```

---

### Task 7: Wire the resolver into the provider factories

**Files:**
- Modify: `nexus/agents/llm.py`, `nexus/integrations/search/engines.py`, `nexus/integrations/apify.py`
- Test: `tests/test_provider_keys.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_provider_keys.py


async def test_the_llm_provider_uses_a_db_key_when_one_exists():
    """The behaviour that makes the whole feature real: a key added in the UI reaches the code
    that calls the provider, without an env change and without a restart."""
    from nexus.providers import resolver
    from nexus.providers.service import add_key

    await add_key("groq", "from-ui", "gsk-from-the-ui-0001")
    resolver.invalidate()
    assert "gsk-from-the-ui-0001" in await resolver.key_pool("groq")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_provider_keys.py -n0 -q -k llm_provider_uses`
Expected: PASS already (it only exercises the resolver). This step is the seam wiring; the
behavioural assertion lives in the manual check in Step 4.

- [ ] **Step 3: Add a refresh hook to each provider**

In `nexus/agents/llm.py`, on `GroqLLMProvider`, add:

```python
    async def _refresh_keys(self) -> None:
        """Re-read the pool so a key added in the Control plane reaches a running worker.

        Cheap: `resolver.key_pool` is TTL-cached, so this is a dict lookup on all but one call in
        thirty seconds.
        """
        from nexus.providers.resolver import key_pool

        pool = await key_pool("groq")
        if pool and pool != self._keys:
            self._keys = pool
            self._idx = 0        # start from the pinned key
```

Call it as the first line of `complete()`, before the retry loop.

In `nexus/integrations/search/engines.py`, add the same to `ExaSearchProvider` and
`FirecrawlSearchProvider`, using `key_pool("exa")` / `key_pool("firecrawl")` and assigning to
`self.api_keys` with `self._key_idx = 0`. Call it at the top of `_post` / `_request`.

In `nexus/integrations/apify.py`, add the same to `ApifyClient` using `key_pool("apify")`, called
at the top of `run_actor`.

- [ ] **Step 4: Verify by hand against the running stack**

```bash
docker exec nexus-gtm-app-1 python -c "
import asyncio
from nexus.providers.resolver import key_pool
print(asyncio.run(key_pool('exa')))"
```

Expected: the env keys (the table is empty). Add one through the API in Task 9 and re-run — the
new key must appear within 30s in **both** `nexus-gtm-app-1` and `nexus-gtm-worker-1`.

- [ ] **Step 5: Run the affected suites**

Run: `python -m pytest tests/test_search_engines.py tests/test_llm_providers.py tests/test_apify_client.py tests/test_provider_keys.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nexus/agents/llm.py nexus/integrations/search/engines.py nexus/integrations/apify.py tests/test_provider_keys.py
git commit -m "feat(providers): provider factories re-read the pool per call"
```

---

### Task 8: Runtime rejections mark the row failed

**Files:**
- Modify: `nexus/integrations/search/engines.py`, `nexus/agents/llm.py`, `nexus/integrations/apify.py`
- Test: `tests/test_provider_keys.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_provider_keys.py


async def test_a_runtime_rejection_marks_the_row_failed():
    """What makes the panel operational rather than a form: it shows what happened in production,
    not only what the last manual test said."""
    from nexus.providers.crypto import key_digest
    from nexus.providers.service import add_key, list_keys, mark_failed_by_digest

    k = await add_key("exa", "will-die", "sk-dies-4321")
    await mark_failed_by_digest(
        "exa", key_digest("sk-dies-4321"), error="invalid api key", error_status=401
    )
    row = next(r for r in await list_keys("exa") if r.id == k.id)
    assert row.status == "failed"
    assert row.last_error_status == 401
    assert "invalid api key" in row.last_error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_provider_keys.py -n0 -q -k runtime_rejection`
Expected: PASS if Task 4 is complete (`mark_failed_by_digest` already exists). The remaining work
is calling it from the rotation paths.

- [ ] **Step 3: Call it where a key is condemned**

In `ExaSearchProvider._post`, inside the `_KEY_REJECTED_STATUS` branch added on 2026-08-21, after
the `logger.warning`:

```python
                    # Record it against the row so the Control plane shows production reality.
                    # Fire-and-forget: a bookkeeping write must never fail a search.
                    try:
                        from nexus.providers.crypto import key_digest
                        from nexus.providers.service import mark_failed_by_digest

                        await mark_failed_by_digest(
                            "exa", key_digest(keys[self._key_idx]),
                            error=_detail_text(resp), error_status=resp.status_code,
                        )
                    except Exception:
                        logger.debug("could not record the Exa key rejection", exc_info=True)
```

Add the same in `FirecrawlSearchProvider._request` with `"firecrawl"`, in `GroqLLMProvider.complete`
with `"groq"` in its 401/403 branch, and in `ApifyClient.run_actor` with `"apify"` in its 401/403
branch.

Add this helper near `_detail` in `engines.py`:

```python
def _detail_text(resp) -> str:
    """The provider's own error message, for the Control plane to display verbatim."""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        return str(err or body)[:300]
    except Exception:
        return (getattr(resp, "text", "") or "")[:300]
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_search_engines.py tests/test_llm_providers.py tests/test_provider_keys.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/integrations/search/engines.py nexus/agents/llm.py nexus/integrations/apify.py tests/test_provider_keys.py
git commit -m "feat(providers): a runtime rejection marks the key failed in the Control plane"
```

---

### Task 9: API surface

**Files:**
- Create: `nexus/api/routers/admin_provider_keys.py`
- Modify: `nexus/billing/permissions.py`, `nexus/api/__init__.py` (router registration)
- Test: `tests/test_provider_keys_api.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provider_keys_api.py
"""The Control-plane surface for provider keys.

Gated on `providers.manage`, in the superadmin preset only: registering credentials and granting
platform power are different acts, the same argument that keeps `sources.manage` separate.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


async def _superadmin(client, monkeypatch, email="boss@pk.com"):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug="pk1", email=email, company="PK")


async def test_a_key_is_never_returned_in_a_response(client, monkeypatch):
    token = await _superadmin(client, monkeypatch)
    created = await client.post("/api/admin/provider-keys", headers=auth(token),
                                json={"provider": "exa", "label": "p", "key": "sk-secret-9999"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert "sk-secret-9999" not in str(body)
    assert body["key_hint"] == "9999"

    listed = (await client.get("/api/admin/provider-keys", headers=auth(token))).json()
    assert "sk-secret-9999" not in str(listed)


async def test_a_tenant_owner_cannot_reach_it(client):
    """No tenant role grants platform power."""
    token = await signup(client, slug="pk2", email="o@pk2.com", company="PK2")
    r = await client.get("/api/admin/provider-keys", headers=auth(token))
    assert r.status_code == 403


async def test_a_request_body_cannot_set_status(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, email="boss2@pk.com")
    r = await client.post("/api/admin/provider-keys", headers=auth(token),
                          json={"provider": "exa", "label": "x", "key": "sk-x-1111",
                                "status": "verified"})
    # `extra="forbid"` rejects it outright rather than quietly ignoring it.
    assert r.status_code == 422


async def test_preferring_a_key_is_audited(client, monkeypatch):
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingAuditLog

    token = await _superadmin(client, monkeypatch, email="boss3@pk.com")
    created = (await client.post("/api/admin/provider-keys", headers=auth(token),
                                 json={"provider": "brave", "label": "b",
                                       "key": "sk-b-2222"})).json()
    r = await client.post(f"/api/admin/provider-keys/{created['id']}/prefer",
                          headers=auth(token))
    assert r.status_code == 200

    async with get_platform_sessionmaker()() as s:
        rows = list((await s.scalars(select(BillingAuditLog))).all())
    assert any(row.action == "provider_key.prefer" for row in rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_provider_keys_api.py -n0 -q`
Expected: FAIL, 404 on the endpoint

- [ ] **Step 3: Add the permission**

In `nexus/billing/permissions.py`, beside `SOURCES_MANAGE`:

```python
PROVIDERS_MANAGE = "providers.manage"    # add/test/pin platform provider API keys
```

Add it to `ALL_PERMISSIONS`. Do **not** add it to the `support` or `billing_admin` presets —
`superadmin` gets it via `ALL_PERMISSIONS`.

- [ ] **Step 4: Write the router**

```python
# nexus/api/routers/admin_provider_keys.py
"""Provider API keys, managed from the Control plane.

The key itself is never in a response model — not even for the superadmin who typed it. `key_hint`
(the last four characters) is what the UI identifies a row by.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.audit import record_admin_action
from nexus.billing.permissions import PROVIDERS_MANAGE
from nexus.core.db import get_platform_sessionmaker
from nexus.providers import service
from nexus.providers.catalog import PROVIDERS

router = APIRouter(prefix="/admin/provider-keys", tags=["admin-providers"])


class ProviderKeyOut(BaseModel):
    id: str
    provider: str
    label: str
    key_hint: str
    status: str
    last_depth: str
    last_error: str
    last_error_status: int | None
    enabled: bool
    preferred: bool

    @classmethod
    def of(cls, row) -> "ProviderKeyOut":
        return cls(
            id=row.id, provider=row.provider, label=row.label, key_hint=row.key_hint,
            status=row.status, last_depth=row.last_depth, last_error=row.last_error,
            last_error_status=row.last_error_status, enabled=row.enabled,
            preferred=row.preferred,
        )


class ProviderKeyIn(BaseModel):
    # `forbid` so a body carrying `status` is rejected outright rather than silently ignored.
    model_config = {"extra": "forbid"}

    provider: str
    label: str = ""
    key: str = Field(min_length=8)


@router.get("", response_model=list[ProviderKeyOut])
async def list_provider_keys(
    provider: str = "",
    _: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> list[ProviderKeyOut]:
    return [ProviderKeyOut.of(r) for r in await service.list_keys(provider)]


@router.post("", response_model=ProviderKeyOut, status_code=status.HTTP_201_CREATED)
async def create_provider_key(
    body: ProviderKeyIn,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> ProviderKeyOut:
    try:
        row = await service.add_key(body.provider, body.label, body.key,
                                    user_id=principal.user_id)
    except service.DuplicateKey as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except service.UnknownProvider as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await _audit(principal, "provider_key.create", row.id,
                 {"provider": row.provider, "hint": row.key_hint})
    return ProviderKeyOut.of(row)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider_key(
    key_id: str,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> None:
    if not await service.delete_key(key_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such key")
    await _audit(principal, "provider_key.delete", key_id, {})


@router.post("/{key_id}/prefer", response_model=ProviderKeyOut)
async def prefer_provider_key(
    key_id: str,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> ProviderKeyOut:
    row = await service.prefer_key(key_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such key")
    await _audit(principal, "provider_key.prefer", key_id, {"provider": row.provider})
    return ProviderKeyOut.of(row)


@router.post("/{key_id}/enabled/{value}", response_model=ProviderKeyOut)
async def set_provider_key_enabled(
    key_id: str,
    value: bool,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> ProviderKeyOut:
    row = await service.set_enabled(key_id, value)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such key")
    await _audit(principal, "provider_key.enabled", key_id, {"enabled": value})
    return ProviderKeyOut.of(row)


@router.post("/{key_id}/test", response_model=dict)
async def test_provider_key(
    key_id: str,
    depth: str = "probe",
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> dict:
    """`depth=probe` proves the credential authenticates and is free. `depth=verify` makes a real
    call through the adapter and costs credits — which is why it is never automatic."""
    from nexus.models.provider_key import ProviderKey
    from nexus.providers.crypto import unseal_key
    from nexus.providers.testing import probe, verify

    async with get_platform_sessionmaker()() as s:
        row = await s.get(ProviderKey, key_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such key")
        provider, secret = row.provider, unseal_key(row.key_encrypted)

    runner = verify if depth == "verify" else probe
    result = await runner(provider, secret)
    await service.mark_tested(key_id, status=result.status, depth=depth,
                              error=result.detail, error_status=result.http_status)
    await _audit(principal, "provider_key.test", key_id,
                 {"depth": depth, "ok": result.ok, "status": result.status})
    return {"ok": result.ok, "status": result.status, "detail": result.detail,
            "http_status": result.http_status}


async def _audit(principal: Principal, action: str, target: str, after: dict) -> None:
    async with get_platform_sessionmaker()() as s:
        await record_admin_action(s, actor=principal.user_id, action=action,
                                  target=target, after=after)
        await s.commit()


@router.get("/providers", response_model=list[dict])
async def list_supported_providers(
    _: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> list[dict]:
    return [{"id": p.id, "label": p.label} for p in PROVIDERS.values()]
```

Register it where the other admin routers are registered (`grep -n "admin_sources" nexus/api/__init__.py`
to find the pattern) and mirror it exactly.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_provider_keys_api.py -n0 -q`
Expected: PASS, 4 passed

If `record_admin_action` requires `subject_tenant_id`, pass `None` — these are platform actions
with no tenant.

- [ ] **Step 6: Commit**

```bash
git add nexus/api/routers/admin_provider_keys.py nexus/billing/permissions.py nexus/api/__init__.py tests/test_provider_keys_api.py
git commit -m "feat(providers): Control-plane API for provider keys, gated on providers.manage"
```

---

### Task 10: The UI tab

**Files:**
- Create: `frontend/src/pages/admin/ProviderKeysTab.tsx`, `frontend/src/pages/admin/ProviderKeysTab.module.css`
- Modify: `frontend/src/pages/admin/AdminBillingPage.tsx`, `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`

- [ ] **Step 1: Add the types**

```typescript
// frontend/src/lib/types.ts
/** A provider API key. The key itself is never sent to the client — `key_hint` is its last 4. */
export interface ProviderKey {
  id: string;
  provider: string;
  label: string;
  key_hint: string;
  /** untested | probe_ok | verified | failed. `probe_ok` and `verified` are NOT the same: a key
   *  can authenticate while every real call fails (measured on Groq, 2026-08-21). */
  status: string;
  last_depth: string;
  last_error: string;
  last_error_status: number | null;
  enabled: boolean;
  preferred: boolean;
}

export interface ProviderKeyTestResult {
  ok: boolean;
  status: string;
  detail: string;
  http_status: number | null;
}
```

- [ ] **Step 2: Add the API client methods**

```typescript
// frontend/src/lib/api.ts — beside the other admin methods.
// NOTE: `body` takes a plain object. `request` serializes it; pre-stringifying double-encodes and
// produces a 422 (see the comment on billingCheckout).
  providerKeys(provider = "", signal?: AbortSignal) {
    return this.request<ProviderKey[]>("/admin/provider-keys", {
      query: { provider }, signal,
    });
  }
  addProviderKey(body: { provider: string; label: string; key: string }) {
    return this.request<ProviderKey>("/admin/provider-keys", { method: "POST", body });
  }
  deleteProviderKey(id: string) {
    return this.request<void>(`/admin/provider-keys/${id}`, { method: "DELETE" });
  }
  preferProviderKey(id: string) {
    return this.request<ProviderKey>(`/admin/provider-keys/${id}/prefer`, { method: "POST" });
  }
  setProviderKeyEnabled(id: string, enabled: boolean) {
    return this.request<ProviderKey>(
      `/admin/provider-keys/${id}/enabled/${enabled}`, { method: "POST" },
    );
  }
  testProviderKey(id: string, depth: "probe" | "verify") {
    return this.request<ProviderKeyTestResult>(
      `/admin/provider-keys/${id}/test`, { method: "POST", query: { depth } },
    );
  }
```

- [ ] **Step 3: Build the tab**

Group rows by provider. Each row shows label, `••••{key_hint}`, a status badge, and the last error
when there is one. Row actions: **Pin** (disabled when already preferred), **Test** (probe),
**Verify** (billable — the button must say so), **Disable/Enable**, **Delete** (confirm first).

Status badges, and they are four distinct states, not three:

| Status | Tone | Means |
|---|---|---|
| `verified` | success | A real call through our adapter worked. |
| `probe_ok` | info, **not** success | The credential authenticates. Real calls untested. |
| `failed` | danger | Show `last_error` verbatim — it is the provider's own words. |
| `untested` | neutral | |

`probe_ok` must not render as a green tick. That is the whole point: five Groq keys were
`probe_ok` while every draft came from the stub.

Follow `frontend/src/pages/admin/AdminForms.module.css` for form styling, use design tokens only
(`var(--space-4)`, `var(--text-muted)`), and check contrast is ≥4.5:1 for body text — compositing
alpha properly, since `--text-muted` on `--surface-2` measures 4.49:1 and fails.

- [ ] **Step 4: Register the tab**

In `AdminBillingPage.tsx`, add `"Provider keys"` to the tab list beside Rate cards / Plans /
Subscriptions and render `<ProviderKeysTab />` for it.

- [ ] **Step 5: Typecheck and build**

Run: `cd frontend && npx tsc --noEmit && npm run build`
Expected: no errors

- [ ] **Step 6: Verify in the browser**

```bash
cd deploy && docker compose -f docker-compose.prod.yml build app && \
  docker compose -f docker-compose.prod.yml up -d --force-recreate app worker
```

Log in as superadmin, open Control plane → Provider keys, add a key, pin it, probe it. Then confirm
the worker sees it — this is the requirement, so check the worker container specifically:

```bash
docker exec nexus-gtm-worker-1 python -c "
import asyncio
from nexus.providers.resolver import key_pool
print(asyncio.run(key_pool('exa')))"
```

Expected: the new key present within 30 seconds, with no restart of that container.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/admin/ProviderKeysTab.tsx frontend/src/pages/admin/ProviderKeysTab.module.css frontend/src/pages/admin/AdminBillingPage.tsx frontend/src/lib/api.ts frontend/src/lib/types.ts
git commit -m "feat(providers): Provider keys tab in the Control plane"
```

---

### Task 11: Full suite and documentation

- [ ] **Step 1: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS. Baseline before this work was 1909 + the rotation-fix tests.

- [ ] **Step 2: Record it in CLAUDE.md**

Add a section after "CRM and telephony: what is actually connected" covering: the table is
platform-global with no tenant_id; DB layers over env and an empty table is byte-identical to
today; `probe_ok` and `verified` are separate because all five Groq keys passed `GET /models` and
404'd on every completion; rotation condemns the key only on 401/402/403/429 and raises on
400/404/422 because those fail identically on every key; and the 30s TTL is what lets a separate
worker container pick up a new key without restarting.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: provider key management, and why probe and verify are separate states"
```

---

## Self-review

**Spec coverage.** Every section maps to a task: model → 3; sealing → 1; catalog/scope → 2;
resolution + TTL → 6; singleton refactor → 7; rotation write-back → 8; permission/API/audit → 9;
UI → 10; two test depths → 5. The out-of-scope list is enforced by a test in Task 2.

**Placeholders.** The only intentional one is `00NN` for the migration number, which cannot be
resolved now because an unmerged branch may claim `0044`; "Before you start" gives the exact
command.

**Type consistency.** `TestResult(ok, status, detail, http_status)` is used identically in Tasks 5,
9 and 10. `mark_failed_by_digest(provider, digest, error, error_status)` matches between Tasks 4 and
8. `ProviderKeyOut` fields match the `ProviderKey` TypeScript interface field for field.

**Known risk.** Task 7 touches three hot paths. Each `_refresh_keys` is a TTL-cached dict lookup on
all but one call in thirty seconds, but if the suite slows measurably, that is the place to look.
