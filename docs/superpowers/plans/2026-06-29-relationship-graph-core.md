# Relationship Graph — Core (Phases 1–2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the offline core of the Happenstance-style relationship graph — connect a network source, fold its contacts + touchpoints into a deduped per-tenant graph with materialized connection strength, then answer NL "who do we know" search (A1) and warm-intro mapping (A4) with private-by-default pooling.

**Architecture:** A new, fully additive `nexus/network/` package + `nexus/models/network.py`. Relational source-of-truth (Approach A): `NetworkSourceAccount`, `NetworkPerson`, `NetworkIdentity`, `NetworkEdge`. Strength is materialized on the edge at ingest (deterministic, no LLM — mirrors `score_icp_fit`). Reads go through a single `visible_edges` predicate (`owner == me OR pooling_enabled`). Connectors sit behind a `NetworkConnector` Protocol with an offline `FixtureConnector`; real Google/Microsoft adapters are later seams. Search/intro run on indexed SQL (the documented Approach-C path); the Redis projection cache is a later perf task.

**Tech Stack:** Python 3.11, FastAPI, async SQLAlchemy 2.0, Pydantic v2, Alembic, pytest (`asyncio_mode=auto`), offline SQLite + in-memory queue + stub LLM.

**Scope note:** This plan delivers Phases 1–2 of [the design spec](../specs/2026-06-29-relationship-graph-design.md). Phase 3 (AI profiling/A5), Phase 4 (real Google/Microsoft OAuth adapters), Phase 5 (frontend Network screen), and Phase 6 (team stats + RLS policies + Redis projection cache) each get their own follow-on plan.

**Spec refinement (deliberate):** Identity resolution is **deterministic** (exact normalized email → exact normalized name+company → create-new), not fuzzy. `lookalike/similarity.py` scores *role similarity* and is correct for ranking similar people in search, but using it for identity dedup would merge distinct people who share a role+company. Conservative resolution (never bad-merge) is the spec's stated tie-break anyway.

**Non-breaking guarantee:** New package, new models file, new router, one additive-only migration, one new worker handler. Zero edits to existing model/endpoint behavior. The only edits to existing files are *append-only registrations*: add the model imports to `nexus/models/__init__.py`, the router to `nexus/api/routers/__init__.py`, and the handler/enqueuer to `nexus/workers/tasks.py`.

---

## File structure

**Create:**
- `nexus/models/network.py` — the 4 ORM tables (Task 1)
- `nexus/network/__init__.py` — empty package marker (Task 3)
- `nexus/network/connectors/__init__.py` — empty package marker (Task 3)
- `nexus/network/connectors/base.py` — `NetworkConnector` Protocol + DTOs (Task 3)
- `nexus/network/connectors/fixture.py` — offline adapter (Task 4)
- `nexus/network/connectors/registry.py` — `get_network_connector` + test override (Task 5)
- `nexus/network/resolution.py` — deterministic identity resolution (Task 6)
- `nexus/network/strength.py` — deterministic edge strength (Task 7)
- `nexus/network/service.py` — `ingest_batch`, `set_pooling`, `visible_edges_where` (Task 8)
- `nexus/network/search.py` — NL parse + ranked search (Task 10)
- `nexus/network/intro.py` — warm-intro mapping (Task 11)
- `nexus/network/schemas.py` — Pydantic request/response models (Task 12)
- `nexus/api/routers/network.py` — the `/network` router (Task 12)
- `migrations/versions/0018_relationship_graph.py` — additive migration (Task 14)
- `tests/test_network_strength.py`, `tests/test_network_resolution.py`, `tests/test_network_ingest.py`, `tests/test_network_search.py`, `tests/test_network_intro.py`, `tests/test_network_privacy.py`, `tests/test_network_api.py`

**Modify (append-only):**
- `nexus/models/__init__.py` — import + export the 4 new models (Task 2)
- `nexus/workers/tasks.py` — add `handle_sync_network_account` + `enqueue_sync_network_account` + registry entry (Task 9)
- `nexus/api/routers/__init__.py` — add `network` to imports + `all_routers` (Task 12)

---

## Task 1: Network ORM models

**Files:**
- Create: `nexus/models/network.py`
- Test: `tests/test_network_resolution.py` (model round-trip lives here; resolution added in Task 6)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_resolution.py
from __future__ import annotations

import uuid

import pytest

from tests.conftest import make_tenant, tenant_session


async def seed_member(ts):
    """Create a global User + tenant-scoped Membership; return the Membership."""
    from nexus.models.identity import Membership, User

    u = User(email=f"u{uuid.uuid4().hex}@x.com", full_name="U", password_hash="x")
    ts.session.add(u)
    await ts.session.flush()
    m = Membership(user_id=u.id, role="rep")
    ts.add(m)
    await ts.session.flush()
    return m


async def test_source_account_round_trip_is_tenant_scoped():
    from nexus.models.network import NetworkSourceAccount

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        m = await seed_member(ts)
        ts.add(
            NetworkSourceAccount(
                member_id=m.id, user_id=m.user_id, provider="fixture",
                external_account_id="rep@acme.com", display_email="rep@acme.com",
            )
        )
        await ts.flush()
        rows = await ts.list(NetworkSourceAccount)
        assert len(rows) == 1
        assert rows[0].tenant_id == tid
        assert rows[0].pooling_enabled is False  # private by default
        assert rows[0].status == "connected"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_resolution.py::test_source_account_round_trip_is_tenant_scoped -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.models.network'`

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/models/network.py
"""Relationship Graph (A1–A5): a member's personal network, deduped per tenant.

Four tables form an additive subsystem alongside the account-intelligence loop:
  * NetworkSourceAccount — a member's connected provider account (google/microsoft/...).
  * NetworkPerson — a resolved, deduped person (the dedupe anchor).
  * NetworkIdentity — a raw per-source contact record, resolved to a NetworkPerson.
  * NetworkEdge — an owner(member)↔person relationship with MATERIALIZED connection strength.

Privacy: ``pooling_enabled`` defaults False on the source account and is mirrored onto edges so a
single indexed column gates cross-member visibility (owner == me OR pooling_enabled).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped

PROVIDERS = ("google", "microsoft", "linkedin", "fixture")


class NetworkSourceAccount(IdMixin, TimestampMixin, TenantScoped, Base):
    """A member's connected provider account. OAuth is write-only — never serialized out."""

    __tablename__ = "network_source_accounts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "member_id", "provider", "external_account_id",
            name="uq_network_source",
        ),
        Index("ix_network_source_member", "tenant_id", "member_id"),
    )

    member_id: Mapped[str] = mapped_column(ForeignKey("memberships.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    provider: Mapped[str] = mapped_column(String(16))
    external_account_id: Mapped[str] = mapped_column(String(255))
    display_email: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="connected")
    pooling_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    oauth: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # write-only seam
    sync_cursor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class NetworkPerson(IdMixin, TimestampMixin, TenantScoped, Base):
    """A resolved, deduped person in the tenant graph. ``primary_email`` is the dedupe anchor."""

    __tablename__ = "network_persons"
    __table_args__ = (
        Index("ix_network_person_email", "tenant_id", "primary_email"),
        Index("ix_network_person_domain", "tenant_id", "company_domain"),
    )

    primary_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    first_name: Mapped[str] = mapped_column(String(100), default="")
    last_name: Mapped[str] = mapped_column(String(100), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    company: Mapped[str] = mapped_column(String(200), default="")
    company_domain: Mapped[str] = mapped_column(String(200), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    country: Mapped[str] = mapped_column(String(100), default="")
    linkedin_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    twitter_handle: Mapped[str | None] = mapped_column(String(100), nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    search_text: Mapped[str] = mapped_column(String(600), default="", index=True)
    identity_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    edge_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class NetworkIdentity(IdMixin, TimestampMixin, TenantScoped, Base):
    """A raw per-source contact record, resolved to a NetworkPerson. Upserted by (source, ext id)."""

    __tablename__ = "network_identities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_account_id", "external_id", name="uq_network_identity"
        ),
        Index("ix_network_identity_key", "tenant_id", "resolution_key"),
        Index("ix_network_identity_person", "tenant_id", "person_id"),
    )

    source_account_id: Mapped[str] = mapped_column(
        ForeignKey("network_source_accounts.id"), index=True
    )
    person_id: Mapped[str | None] = mapped_column(ForeignKey("network_persons.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    handle: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    resolution_key: Mapped[str] = mapped_column(String(255), default="")


class NetworkEdge(IdMixin, TimestampMixin, TenantScoped, Base):
    """An owner(member)↔person relationship with materialized strength + touchpoint stats."""

    __tablename__ = "network_edges"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "owner_member_id", "person_id", "provider", name="uq_network_edge"
        ),
        # warm-intro hot path: people→visible brokers ranked by strength, one index seek.
        Index(
            "ix_network_edge_person", "tenant_id", "person_id", "pooling_enabled", "strength"
        ),
        Index("ix_network_edge_owner", "tenant_id", "owner_member_id"),
    )

    owner_member_id: Mapped[str] = mapped_column(ForeignKey("memberships.id"), index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    person_id: Mapped[str] = mapped_column(ForeignKey("network_persons.id"), index=True)
    source_account_id: Mapped[str] = mapped_column(ForeignKey("network_source_accounts.id"))
    provider: Mapped[str] = mapped_column(String(16))
    relation: Mapped[str] = mapped_column(String(16), default="contact")
    strength: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    email_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    received_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meeting_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_touch_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_touch_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    mutual_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pooling_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_resolution.py::test_source_account_round_trip_is_tenant_scoped -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/models/network.py tests/test_network_resolution.py
git commit -m "feat(network): relationship-graph ORM models"
```

---

## Task 2: Register the models

**Files:**
- Modify: `nexus/models/__init__.py`
- Test: `tests/test_network_resolution.py` (add the registration assertion)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_resolution.py  (append)
def test_models_are_registered():
    import nexus.models as m

    for name in ("NetworkSourceAccount", "NetworkPerson", "NetworkIdentity", "NetworkEdge"):
        assert hasattr(m, name), f"{name} not exported from nexus.models"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_resolution.py::test_models_are_registered -v`
Expected: FAIL — `AssertionError: NetworkSourceAccount not exported from nexus.models`

- [ ] **Step 3: Write minimal implementation**

In `nexus/models/__init__.py`, add the import after the existing `from nexus.models.calling import ...` line:

```python
from nexus.models.network import (
    NetworkEdge,
    NetworkIdentity,
    NetworkPerson,
    NetworkSourceAccount,
)
```

And add these four names to the `__all__` list (after `"CallActivity",`):

```python
    "NetworkSourceAccount",
    "NetworkPerson",
    "NetworkIdentity",
    "NetworkEdge",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_resolution.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/models/__init__.py tests/test_network_resolution.py
git commit -m "feat(network): register relationship-graph models"
```

---

## Task 3: Connector interface + DTOs

**Files:**
- Create: `nexus/network/__init__.py` (empty), `nexus/network/connectors/__init__.py` (empty), `nexus/network/connectors/base.py`
- Test: `tests/test_network_ingest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_ingest.py
from __future__ import annotations

from datetime import datetime, timezone


def test_sync_batch_dtos_construct():
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, Touchpoint

    batch = NetworkSyncBatch(
        identities=[RawIdentity(external_id="g1", email="A@Acme.com", name="Ann", title="CTO")],
        touchpoints=[
            Touchpoint(person_external_id="g1", kind="email_sent",
                       at=datetime(2026, 6, 1, tzinfo=timezone.utc))
        ],
        next_cursor="cursor-2",
    )
    assert batch.identities[0].relation == "contact"  # default
    assert batch.next_cursor == "cursor-2"
    assert batch.touchpoints[0].kind == "email_sent"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_ingest.py::test_sync_batch_dtos_construct -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.network'`

- [ ] **Step 3: Write minimal implementation**

Create empty `nexus/network/__init__.py` and `nexus/network/connectors/__init__.py` (0 bytes each). Then:

```python
# nexus/network/connectors/base.py
"""The swappable network-connector seam.

A connector turns a member's connected provider account into a ``NetworkSyncBatch`` of raw
identities + touchpoints. ``FixtureConnector`` runs offline; real Google/Microsoft adapters
implement the same surface — only the bodies differ, so the graph never changes shape.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class RawIdentity(BaseModel):
    external_id: str
    email: str | None = None
    name: str | None = None
    title: str | None = None
    company: str | None = None
    handle: str | None = None
    relation: str = "contact"  # contact | email | calendar | linkedin_1st | follower
    raw: dict = Field(default_factory=dict)


class Touchpoint(BaseModel):
    person_external_id: str
    kind: str  # email_sent | email_received | meeting
    at: datetime


class NetworkSyncBatch(BaseModel):
    identities: list[RawIdentity] = Field(default_factory=list)
    touchpoints: list[Touchpoint] = Field(default_factory=list)
    next_cursor: str | None = None


class AuthChallenge(BaseModel):
    oauth_url: str | None = None
    state: str | None = None


class OAuthTokens(BaseModel):
    access_token: str = ""
    refresh_token: str = ""
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list)


class SourceAccountRef(BaseModel):
    id: str
    provider: str
    external_account_id: str
    oauth: dict = Field(default_factory=dict)


@runtime_checkable
class NetworkConnector(Protocol):
    provider: str

    async def begin_auth(self, redirect_uri: str) -> AuthChallenge: ...

    async def complete_auth(self, code: str) -> OAuthTokens: ...

    async def fetch(self, account: SourceAccountRef, since: str | None) -> NetworkSyncBatch: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_ingest.py::test_sync_batch_dtos_construct -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/network/__init__.py nexus/network/connectors tests/test_network_ingest.py
git commit -m "feat(network): connector interface + sync DTOs"
```

---

## Task 4: Fixture connector

**Files:**
- Create: `nexus/network/connectors/fixture.py`
- Test: `tests/test_network_ingest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_ingest.py  (append)
import pytest


async def test_fixture_connector_returns_its_batch():
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, SourceAccountRef
    from nexus.network.connectors.fixture import FixtureConnector

    canned = NetworkSyncBatch(identities=[RawIdentity(external_id="g1", name="Ann")])
    conn = FixtureConnector(canned)
    ref = SourceAccountRef(id="acc1", provider="fixture", external_account_id="rep@acme.com")

    out = await conn.fetch(ref, None)
    assert out.identities[0].external_id == "g1"
    tokens = await conn.complete_auth("ignored")
    assert tokens.access_token == "fixture-token"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_ingest.py::test_fixture_connector_returns_its_batch -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.network.connectors.fixture'`

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/connectors/fixture.py
"""Offline connector: holds a canned batch so the whole graph runs with zero external services."""
from __future__ import annotations

from nexus.network.connectors.base import (
    AuthChallenge,
    NetworkSyncBatch,
    OAuthTokens,
    SourceAccountRef,
)


class FixtureConnector:
    provider = "fixture"

    def __init__(self, batch: NetworkSyncBatch | None = None):
        self._batch = batch or NetworkSyncBatch()

    async def begin_auth(self, redirect_uri: str) -> AuthChallenge:
        return AuthChallenge(oauth_url=None, state="fixture")

    async def complete_auth(self, code: str) -> OAuthTokens:
        return OAuthTokens(access_token="fixture-token")

    async def fetch(self, account: SourceAccountRef, since: str | None) -> NetworkSyncBatch:
        return self._batch
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_ingest.py::test_fixture_connector_returns_its_batch -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/network/connectors/fixture.py tests/test_network_ingest.py
git commit -m "feat(network): offline fixture connector"
```

---

## Task 5: Connector registry (with test override seam)

**Files:**
- Create: `nexus/network/connectors/registry.py`
- Test: `tests/test_network_ingest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_ingest.py  (append)
def test_registry_returns_fixture_and_override():
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity
    from nexus.network.connectors.fixture import FixtureConnector
    from nexus.network.connectors.registry import (
        get_network_connector,
        set_network_connector,
    )

    assert get_network_connector("fixture").provider == "fixture"
    with pytest.raises(ValueError):
        get_network_connector("nope")

    override = FixtureConnector(NetworkSyncBatch(identities=[RawIdentity(external_id="x")]))
    set_network_connector(override)
    try:
        # the override short-circuits the registry for any provider name
        assert get_network_connector("google") is override
        assert get_network_connector("microsoft") is override
    finally:
        set_network_connector(None)
    # cleared → registry behaviour restored
    assert get_network_connector("fixture").provider == "fixture"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_ingest.py::test_registry_returns_fixture_and_override -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.network.connectors.registry'`

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/connectors/registry.py
"""Connector lookup. A process-wide override lets tests inject a canned FixtureConnector for the
sync-job path without touching real provider code."""
from __future__ import annotations

from nexus.network.connectors.base import NetworkConnector
from nexus.network.connectors.fixture import FixtureConnector

_REGISTRY: dict[str, type] = {"fixture": FixtureConnector}
_override: NetworkConnector | None = None


def set_network_connector(connector: NetworkConnector | None) -> None:
    """Test seam: force every lookup to return ``connector`` (or clear with ``None``)."""
    global _override
    _override = connector


def get_network_connector(provider: str) -> NetworkConnector:
    if _override is not None:
        return _override
    cls = _REGISTRY.get(provider)
    if cls is None:
        raise ValueError(f"unknown network provider: {provider}")
    return cls()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_ingest.py::test_registry_returns_fixture_and_override -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/network/connectors/registry.py tests/test_network_ingest.py
git commit -m "feat(network): connector registry with test override"
```

---

## Task 6: Deterministic identity resolution

**Files:**
- Create: `nexus/network/resolution.py`
- Test: `tests/test_network_resolution.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_resolution.py  (append)
async def test_resolution_dedupes_by_email_and_creates_on_miss():
    from nexus.models.network import NetworkPerson
    from nexus.network.resolution import resolution_key, resolve_person

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        # case-insensitive email dedupe → same person
        p1 = await resolve_person(ts, email="Ann@Acme.com", name="Ann Lee", title="CTO",
                                  company="Acme")
        p2 = await resolve_person(ts, email="ann@acme.com", name="A. Lee", title="Chief Tech",
                                  company="Acme")
        assert p1.id == p2.id
        assert p1.primary_email == "ann@acme.com"

        # no email: name+company dedupe
        q1 = await resolve_person(ts, email=None, name="Bob Roy", title="VP", company="Globex")
        q2 = await resolve_person(ts, email=None, name="bob roy", title="VP Sales",
                                  company="globex")
        assert q1.id == q2.id

        # different person, same company → NOT merged (conservative)
        r = await resolve_person(ts, email=None, name="Carol Diaz", title="VP", company="Globex")
        assert r.id != q1.id

        assert (await ts.list(NetworkPerson)).__len__() == 3

    # resolution_key is the normalized email when present, else a name|company hash
    assert resolution_key(email="A@B.com", name="x", company="y") == "a@b.com"
    assert resolution_key(email=None, name="A", company="B").startswith("h:")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_resolution.py::test_resolution_dedupes_by_email_and_creates_on_miss -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.network.resolution'`

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/resolution.py
"""Deterministic identity resolution: fold raw identities into canonical NetworkPersons.

Resolution order (idempotent, conservative — never bad-merge):
  1. exact normalized email,
  2. else exact normalized name + company (for emailless records),
  3. else create a new person.

NOTE: this is identity matching, not role similarity. ``lookalike/similarity.py`` (used by search)
scores *similar roles* and would wrongly merge two different people who share a title+company.
"""
from __future__ import annotations

import hashlib
import re

from nexus.core.tenancy import TenantSession
from nexus.models.network import NetworkPerson


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _domain_of(email: str | None) -> str:
    e = normalize_email(email)
    return e.split("@", 1)[1] if "@" in e else ""


def resolution_key(*, email: str | None, name: str | None, company: str | None) -> str:
    e = normalize_email(email)
    if e:
        return e
    return "h:" + hashlib.sha1(f"{_norm(name)}|{_norm(company)}".encode()).hexdigest()


def search_text_for(name: str, title: str, company: str) -> str:
    return " ".join(p for p in (name.strip(), title.strip(), company.strip()) if p).lower()


async def resolve_person(
    ts: TenantSession,
    *,
    email: str | None,
    name: str | None,
    title: str | None,
    company: str | None,
) -> NetworkPerson:
    """Find-or-create the canonical person for a raw identity. Idempotent."""
    email_n = normalize_email(email)
    name_n, company_n = _norm(name), _norm(company)

    if email_n:
        hit = await ts.first(NetworkPerson, NetworkPerson.primary_email == email_n)
        if hit is not None:
            return hit
    elif name_n:
        for cand in await ts.list(
            NetworkPerson, NetworkPerson.primary_email.is_(None), limit=500
        ):
            if _norm(cand.full_name) == name_n and _norm(cand.company) == company_n:
                return cand

    person = NetworkPerson(
        primary_email=email_n or None,
        full_name=name or "",
        title=title or "",
        company=company or "",
        company_domain=_domain_of(email),
        search_text=search_text_for(name or "", title or "", company or ""),
    )
    ts.add(person)
    await ts.flush()
    return person
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_resolution.py -v`
Expected: PASS (all tests in file)

- [ ] **Step 5: Commit**

```bash
git add nexus/network/resolution.py tests/test_network_resolution.py
git commit -m "feat(network): deterministic identity resolution"
```

---

## Task 7: Deterministic connection-strength scorer

**Files:**
- Create: `nexus/network/strength.py`
- Test: `tests/test_network_strength.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_strength.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_score_edge_blends_tier_recency_frequency_reciprocity():
    from nexus.network.strength import EdgeStats, score_edge

    now = datetime(2026, 6, 29, tzinfo=timezone.utc)

    # cold contact, no touchpoints → tier only (contact=30)
    assert score_edge(EdgeStats(relation="contact"), now=now) == 30

    # recent two-way email thread: email tier 20 + recency(<=30d) 30 + freq(min(25, 2*4)) 8
    #   + reciprocity 15 = 73
    recent = EdgeStats(
        relation="email", email_count=4, sent_count=2, received_count=2,
        last_touch_at=now - timedelta(days=5),
    )
    assert score_edge(recent, now=now) == 73

    # strong linkedin + heavy frequency clamps the frequency boost at 25 and total at 100
    strong = EdgeStats(
        relation="linkedin_1st", email_count=100, sent_count=50, received_count=50,
        meeting_count=20, last_touch_at=now - timedelta(days=1),
    )
    assert score_edge(strong, now=now) == 100

    # stale relationship (>1y) gets no recency boost
    stale = EdgeStats(relation="contact", last_touch_at=now - timedelta(days=800))
    assert score_edge(stale, now=now) == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_strength.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.network.strength'`

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/strength.py
"""Deterministic connection-strength (0–100), materialized on the edge at ingest.

No LLM, pure function — mirrors RelevanceEngine.score_icp_fit. Blend of relationship tier, recency
of the last touch, interaction frequency, and reciprocity (two-way contact).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from nexus.core.db import ensure_aware

_TIER = {"linkedin_1st": 40, "contact": 30, "calendar": 25, "email": 20, "follower": 10}


@dataclass(slots=True)
class EdgeStats:
    relation: str
    email_count: int = 0
    sent_count: int = 0
    received_count: int = 0
    meeting_count: int = 0
    last_touch_at: datetime | None = None


def _age_days(at: datetime | None, *, now: datetime) -> int | None:
    at = ensure_aware(at)
    if at is None:
        return None
    return max(0, (now - at).days)


def score_edge(stats: EdgeStats, *, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    score = _TIER.get(stats.relation, 15)

    days = _age_days(stats.last_touch_at, now=now)
    if days is not None:
        if days <= 30:
            score += 30
        elif days <= 90:
            score += 20
        elif days <= 365:
            score += 10

    score += min(25, 2 * stats.email_count + 5 * stats.meeting_count)  # frequency
    if stats.sent_count > 0 and stats.received_count > 0:
        score += 15  # reciprocity

    return max(0, min(100, score))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_strength.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/network/strength.py tests/test_network_strength.py
git commit -m "feat(network): deterministic connection-strength scorer"
```

---

## Task 8: Ingest service (fold a batch into the graph)

**Files:**
- Create: `nexus/network/service.py`
- Test: `tests/test_network_ingest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_ingest.py  (append)
from datetime import datetime, timezone

from tests.conftest import make_tenant, tenant_session


async def _seed_account(ts, *, pooling=False):
    """Create a User+Membership+NetworkSourceAccount; return the account."""
    import uuid

    from nexus.models.identity import Membership, User
    from nexus.models.network import NetworkSourceAccount

    u = User(email=f"u{uuid.uuid4().hex}@x.com", full_name="U", password_hash="x")
    ts.session.add(u)
    await ts.session.flush()
    m = Membership(user_id=u.id, role="rep")
    ts.add(m)
    await ts.session.flush()
    acc = NetworkSourceAccount(
        member_id=m.id, user_id=u.id, provider="fixture",
        external_account_id="rep@acme.com", pooling_enabled=pooling,
    )
    ts.add(acc)
    await ts.session.flush()
    return acc


async def test_ingest_batch_creates_persons_edges_and_materializes_strength():
    from nexus.models.network import NetworkEdge, NetworkPerson
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, Touchpoint
    from nexus.network.service import ingest_batch

    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed_account(ts, pooling=True)
        batch = NetworkSyncBatch(
            identities=[
                RawIdentity(external_id="g1", email="ann@acme.com", name="Ann", title="CTO"),
                RawIdentity(external_id="g2", email="bob@globex.com", name="Bob", title="VP"),
            ],
            touchpoints=[
                Touchpoint(person_external_id="g1", kind="email_sent", at=now),
                Touchpoint(person_external_id="g1", kind="email_received", at=now),
            ],
            next_cursor="c2",
        )
        res = await ingest_batch(ts, acc, batch, now=now)
        assert res == {"identities": 2, "new_persons": 2, "new_edges": 2}

        people = await ts.list(NetworkPerson)
        assert {p.primary_email for p in people} == {"ann@acme.com", "bob@globex.com"}

        edges = await ts.list(NetworkEdge)
        ann_edge = next(e for e in edges if e.email_count == 2)
        assert ann_edge.relation == "email"
        assert ann_edge.strength == 69  # email 20 + recency 30 + freq 4 + reciprocity 15
        assert ann_edge.pooling_enabled is True  # mirrored from the source account

        # re-ingesting the same batch is idempotent (no duplicate persons/edges)
        res2 = await ingest_batch(ts, acc, batch, now=now)
        assert res2 == {"identities": 2, "new_persons": 0, "new_edges": 0}
        assert len(await ts.list(NetworkPerson)) == 2
        assert len(await ts.list(NetworkEdge)) == 2
        assert acc.sync_cursor == "c2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_ingest.py::test_ingest_batch_creates_persons_edges_and_materializes_strength -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.network.service'`

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/service.py
"""Graph ingest + visibility helpers.

``ingest_batch`` idempotently folds a connector's NetworkSyncBatch into the graph: upsert raw
identities, resolve canonical persons, then materialize exactly one edge per
(owner_member, person, provider) with deterministic strength + aggregated touchpoint stats.
``visible_edges_where`` is the single privacy predicate used by every cross-member read.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import or_, update

from nexus.core.db import ensure_aware, utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.network import (
    NetworkEdge,
    NetworkIdentity,
    NetworkSourceAccount,
)
from nexus.network.connectors.base import NetworkSyncBatch
from nexus.network.resolution import resolution_key, resolve_person
from nexus.network.strength import EdgeStats, score_edge


def visible_edges_where(member_id: str):
    """An edge is visible to a member if they own it OR it is pooled."""
    return or_(
        NetworkEdge.owner_member_id == member_id,
        NetworkEdge.pooling_enabled.is_(True),
    )


def _aggregate_touchpoints(batch: NetworkSyncBatch) -> dict[str, dict]:
    agg: dict[str, dict] = defaultdict(
        lambda: {"sent": 0, "received": 0, "meetings": 0, "first": None, "last": None}
    )
    for tp in batch.touchpoints:
        a = agg[tp.person_external_id]
        if tp.kind == "email_sent":
            a["sent"] += 1
        elif tp.kind == "email_received":
            a["received"] += 1
        elif tp.kind == "meeting":
            a["meetings"] += 1
        at = ensure_aware(tp.at)
        if at is not None:
            a["first"] = at if a["first"] is None else min(a["first"], at)
            a["last"] = at if a["last"] is None else max(a["last"], at)
    return agg


async def ingest_batch(
    ts: TenantSession,
    account: NetworkSourceAccount,
    batch: NetworkSyncBatch,
    *,
    now: datetime | None = None,
) -> dict:
    now = now or utcnow()
    agg = _aggregate_touchpoints(batch)
    new_persons = 0
    new_edges = 0

    for raw in batch.identities:
        person = await resolve_person(
            ts, email=raw.email, name=raw.name, title=raw.title, company=raw.company
        )

        ident = await ts.first(
            NetworkIdentity,
            NetworkIdentity.source_account_id == account.id,
            NetworkIdentity.external_id == raw.external_id,
        )
        if ident is None:
            ts.add(
                NetworkIdentity(
                    source_account_id=account.id, person_id=person.id, provider=account.provider,
                    external_id=raw.external_id, email=raw.email, name=raw.name, title=raw.title,
                    company=raw.company, handle=raw.handle, raw=raw.raw,
                    resolution_key=resolution_key(
                        email=raw.email, name=raw.name, company=raw.company
                    ),
                )
            )
            person.identity_count += 1
            new_persons += 1
        else:
            ident.person_id = person.id

        a = agg.get(raw.external_id, {})
        sent, received, meetings = a.get("sent", 0), a.get("received", 0), a.get("meetings", 0)
        email_count = sent + received
        if email_count:
            relation = "email"
        elif meetings:
            relation = "calendar"
        else:
            relation = raw.relation
        stats = EdgeStats(
            relation=relation, email_count=email_count, sent_count=sent,
            received_count=received, meeting_count=meetings, last_touch_at=a.get("last"),
        )
        strength = score_edge(stats, now=now)

        edge = await ts.first(
            NetworkEdge,
            NetworkEdge.owner_member_id == account.member_id,
            NetworkEdge.person_id == person.id,
            NetworkEdge.provider == account.provider,
        )
        if edge is None:
            ts.add(
                NetworkEdge(
                    owner_member_id=account.member_id, owner_user_id=account.user_id,
                    person_id=person.id, source_account_id=account.id, provider=account.provider,
                    relation=relation, strength=strength, email_count=email_count, sent_count=sent,
                    received_count=received, meeting_count=meetings, first_touch_at=a.get("first"),
                    last_touch_at=a.get("last"), pooling_enabled=account.pooling_enabled,
                )
            )
            person.edge_count += 1
            new_edges += 1
        else:
            edge.relation = relation
            edge.strength = strength
            edge.email_count = email_count
            edge.sent_count = sent
            edge.received_count = received
            edge.meeting_count = meetings
            edge.first_touch_at = a.get("first")
            edge.last_touch_at = a.get("last")
            edge.pooling_enabled = account.pooling_enabled

    account.sync_cursor = batch.next_cursor
    account.last_synced_at = now
    account.status = "connected"
    await ts.flush()
    return {"identities": len(batch.identities), "new_persons": new_persons, "new_edges": new_edges}


async def set_pooling(ts: TenantSession, account: NetworkSourceAccount, enabled: bool) -> None:
    """Toggle pooling on a source account and mirror it onto its edges (drives visibility)."""
    account.pooling_enabled = enabled
    await ts.session.execute(
        update(NetworkEdge)
        .where(
            NetworkEdge.tenant_id == ts.tenant_id,
            NetworkEdge.source_account_id == account.id,
        )
        .values(pooling_enabled=enabled)
    )
    await ts.flush()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_ingest.py -v`
Expected: PASS (all tests in file)

- [ ] **Step 5: Commit**

```bash
git add nexus/network/service.py tests/test_network_ingest.py
git commit -m "feat(network): idempotent graph ingest + pooling toggle"
```

---

## Task 9: Sync worker job

**Files:**
- Modify: `nexus/workers/tasks.py`
- Test: `tests/test_network_ingest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_ingest.py  (append)
async def test_sync_job_pulls_from_connector_and_ingests():
    from nexus.models.network import NetworkPerson
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity
    from nexus.network.connectors.fixture import FixtureConnector
    from nexus.network.connectors.registry import set_network_connector
    from nexus.workers.tasks import handle_sync_network_account

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed_account(ts)
        acc_id = acc.id

    set_network_connector(
        FixtureConnector(NetworkSyncBatch(
            identities=[RawIdentity(external_id="g9", email="zoe@acme.com", name="Zoe")],
            next_cursor="cz",
        ))
    )
    try:
        res = await handle_sync_network_account({"tenant_id": tid, "account_id": acc_id})
    finally:
        set_network_connector(None)

    assert res["new_persons"] == 1
    async with tenant_session(tid) as ts:
        assert len(await ts.list(NetworkPerson)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_ingest.py::test_sync_job_pulls_from_connector_and_ingests -v`
Expected: FAIL — `ImportError: cannot import name 'handle_sync_network_account'`

- [ ] **Step 3: Write minimal implementation**

In `nexus/workers/tasks.py`, add this handler after `handle_discover_icp_accounts` (before the `HANDLERS` dict):

```python
async def handle_sync_network_account(payload: dict) -> dict:
    """Pull a member's network source via its connector and fold the batch into the graph.
    Idempotent: re-running re-upserts identities/edges and advances the sync cursor."""
    from nexus.models.network import NetworkSourceAccount
    from nexus.network.connectors.base import SourceAccountRef
    from nexus.network.connectors.registry import get_network_connector
    from nexus.network.service import ingest_batch

    tid = payload["tenant_id"]
    account_id = payload["account_id"]
    async with tenant_session(tid) as ts:
        acc = await ts.get(NetworkSourceAccount, account_id)
        if acc is None:
            return {"error": "account_not_found", "account_id": account_id}
        connector = get_network_connector(acc.provider)
        ref = SourceAccountRef(
            id=acc.id, provider=acc.provider,
            external_account_id=acc.external_account_id, oauth=acc.oauth,
        )
        batch = await connector.fetch(ref, acc.sync_cursor)
        res = await ingest_batch(ts, acc, batch)
    return {"account_id": account_id, **res}
```

Add to the `HANDLERS` dict (after `"discover_icp_accounts": handle_discover_icp_accounts,`):

```python
    "sync_network_account": handle_sync_network_account,
```

And add this enqueuer after `enqueue_discover_icp_accounts`:

```python
async def enqueue_sync_network_account(
    tenant_id: str, account_id: str, *, queue: TaskQueue | None = None
) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(
        Job(name="sync_network_account",
            payload={"tenant_id": tenant_id, "account_id": account_id})
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_ingest.py::test_sync_job_pulls_from_connector_and_ingests -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/workers/tasks.py tests/test_network_ingest.py
git commit -m "feat(network): sync_network_account worker job"
```

---

## Task 10: NL search (A1)

**Files:**
- Create: `nexus/network/search.py`
- Test: `tests/test_network_search.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_search.py
from __future__ import annotations

from datetime import datetime, timezone

from tests.conftest import make_tenant, tenant_session


async def _seed_account(ts, *, pooling=False):
    import uuid

    from nexus.models.identity import Membership, User
    from nexus.models.network import NetworkSourceAccount

    u = User(email=f"u{uuid.uuid4().hex}@x.com", full_name="U", password_hash="x")
    ts.session.add(u)
    await ts.session.flush()
    m = Membership(user_id=u.id, role="rep")
    ts.add(m)
    await ts.session.flush()
    acc = NetworkSourceAccount(
        member_id=m.id, user_id=u.id, provider="fixture",
        external_account_id=f"{u.email}", pooling_enabled=pooling,
    )
    ts.add(acc)
    await ts.session.flush()
    return acc


def test_parse_query_extracts_titles_and_keywords():
    from nexus.network.search import parse_query

    q = parse_query("Find me a CTO at healthcare startups in New York")
    assert "cto" in q.titles
    assert "healthcare" in q.keywords
    assert "find" not in q.keywords  # stopword


async def test_search_ranks_visible_people_by_match_and_strength():
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, Touchpoint
    from nexus.network.search import search_network
    from nexus.network.service import ingest_batch

    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed_account(ts, pooling=True)
        await ingest_batch(
            ts, acc,
            NetworkSyncBatch(
                identities=[
                    RawIdentity(external_id="g1", email="ann@health.com", name="Ann Lee",
                                title="CTO", company="HealthCo"),
                    RawIdentity(external_id="g2", email="bob@bank.com", name="Bob Roy",
                                title="CFO", company="BankCo"),
                ],
                touchpoints=[Touchpoint(person_external_id="g1", kind="email_sent", at=now)],
            ),
            now=now,
        )
        hits = await search_network(ts, member_id=acc.member_id, query="CTO at HealthCo")
        assert hits[0].person.full_name == "Ann Lee"
        assert hits[0].broker_member_ids == [acc.member_id]
        assert hits[0].best_strength > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_search.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.network.search'`

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/search.py
"""NL "who do we know" search (A1).

Two stages, both swappable:
  1. parse — deterministic NL→structured ``NetworkQuery`` (a real LLM adapter can replace
     ``parse_query`` without changing the contract; the StubLLMProvider philosophy).
  2. rank — fetch tenant people who have >=1 *visible* edge, score by keyword match × best visible
     connection strength, return top-N with their broker member ids.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import or_, select

from nexus.core.tenancy import TenantSession
from nexus.models.network import NetworkEdge, NetworkPerson
from nexus.network.service import visible_edges_where

_TITLE_HINTS = (
    "ceo", "cto", "cfo", "coo", "cmo", "cro", "vp", "head", "director", "founder",
    "manager", "lead", "president", "owner", "engineer", "designer", "recruiter",
    "partner", "investor", "analyst",
)
_STOP = frozenset({
    "at", "in", "the", "a", "an", "of", "who", "find", "people", "person", "someone",
    "know", "me", "we", "our", "is", "are", "and", "or", "to", "for", "with",
})


@dataclass(slots=True)
class NetworkQuery:
    keywords: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchHit:
    person: NetworkPerson
    score: float
    best_strength: int
    broker_member_ids: list[str]


def parse_query(text: str) -> NetworkQuery:
    toks = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP]
    titles = [t for t in toks if t in _TITLE_HINTS]
    keywords = [t for t in toks if len(t) >= 3]
    return NetworkQuery(keywords=keywords, titles=titles)


def _match_score(person: NetworkPerson, q: NetworkQuery) -> float:
    if not q.keywords:
        return 0.5
    hay = (person.search_text or "").lower()
    hits = sum(1 for k in q.keywords if k in hay)
    return hits / len(q.keywords)


async def search_network(
    ts: TenantSession, *, member_id: str, query: str, limit: int = 20
) -> list[SearchHit]:
    q = parse_query(query)
    visible = visible_edges_where(member_id)

    stmt = ts.select(NetworkPerson).where(
        NetworkPerson.id.in_(
            select(NetworkEdge.person_id).where(NetworkEdge.tenant_id == ts.tenant_id, visible)
        )
    )
    if q.keywords:
        stmt = stmt.where(
            or_(*[NetworkPerson.search_text.like(f"%{k}%") for k in q.keywords[:8]])
        )
    people = list((await ts.session.scalars(stmt.limit(500))).all())

    hits: list[SearchHit] = []
    for p in people:
        edges = list(
            (await ts.session.scalars(
                ts.select(NetworkEdge, NetworkEdge.person_id == p.id, visible)
                .order_by(NetworkEdge.strength.desc())
            )).all()
        )
        if not edges:
            continue
        rel = _match_score(p, q)
        if rel <= 0:
            continue
        best = edges[0].strength
        hits.append(
            SearchHit(
                person=p, score=rel * (best / 100.0), best_strength=best,
                broker_member_ids=[e.owner_member_id for e in edges],
            )
        )
    hits.sort(key=lambda h: (h.score, h.best_strength), reverse=True)
    return hits[:limit]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_search.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/network/search.py tests/test_network_search.py
git commit -m "feat(network): NL who-do-we-know search (A1)"
```

---

## Task 11: Warm-intro mapping (A4)

**Files:**
- Create: `nexus/network/intro.py`
- Test: `tests/test_network_intro.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_intro.py
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from tests.conftest import make_tenant, tenant_session


async def _member(ts, *, pooling=False):
    from nexus.models.identity import Membership, User
    from nexus.models.network import NetworkSourceAccount

    u = User(email=f"u{uuid.uuid4().hex}@x.com", full_name="U", password_hash="x")
    ts.session.add(u)
    await ts.session.flush()
    m = Membership(user_id=u.id, role="rep")
    ts.add(m)
    await ts.session.flush()
    acc = NetworkSourceAccount(
        member_id=m.id, user_id=u.id, provider="fixture",
        external_account_id=u.email, pooling_enabled=pooling,
    )
    ts.add(acc)
    await ts.session.flush()
    return m, acc


async def test_intro_paths_rank_visible_brokers_by_strength():
    from nexus.models.network import NetworkPerson
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, Touchpoint
    from nexus.network.intro import intro_paths
    from nexus.network.service import ingest_batch

    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        # Two members both know Target. Alice (pooled) has a recent two-way thread → strong.
        # Bob (pooled) only has a cold contact → weak.
        alice, alice_acc = await _member(ts, pooling=True)
        bob, bob_acc = await _member(ts, pooling=True)
        target = RawIdentity(external_id="t1", email="target@corp.com", name="Tess Target",
                             title="VP Procurement", company="Corp")

        await ingest_batch(ts, alice_acc, NetworkSyncBatch(
            identities=[target],
            touchpoints=[
                Touchpoint(person_external_id="t1", kind="email_sent", at=now),
                Touchpoint(person_external_id="t1", kind="email_received", at=now),
            ],
        ), now=now)
        await ingest_batch(ts, bob_acc, NetworkSyncBatch(identities=[target]), now=now)

        person = (await ts.list(NetworkPerson))[0]
        paths = await intro_paths(ts, person_id=person.id, member_id=alice.id)
        assert [p.broker_member_id for p in paths] == [alice.id, bob.id]  # strong first
        assert paths[0].strength > paths[1].strength
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_intro.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.network.intro'`

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/intro.py
"""Warm-intro mapping (A4): for a target person, who on the team can broker an intro, ranked.

Effectively 1-hop in the team-pool model — the brokers are the *members* who hold a visible edge
to the person. One index seek on (tenant_id, person_id, pooling_enabled, strength).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nexus.core.tenancy import TenantSession
from nexus.models.network import NetworkEdge
from nexus.network.service import visible_edges_where


@dataclass(slots=True)
class IntroPath:
    broker_member_id: str
    broker_user_id: str
    relation: str
    strength: int
    last_touch_at: datetime | None
    provider: str


async def intro_paths(
    ts: TenantSession, *, person_id: str, member_id: str
) -> list[IntroPath]:
    edges = list(
        (await ts.session.scalars(
            ts.select(NetworkEdge, NetworkEdge.person_id == person_id, visible_edges_where(member_id))
            .order_by(NetworkEdge.strength.desc(), NetworkEdge.last_touch_at.desc())
        )).all()
    )
    return [
        IntroPath(
            broker_member_id=e.owner_member_id, broker_user_id=e.owner_user_id,
            relation=e.relation, strength=e.strength, last_touch_at=e.last_touch_at,
            provider=e.provider,
        )
        for e in edges
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_intro.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/network/intro.py tests/test_network_intro.py
git commit -m "feat(network): warm-intro mapping (A4)"
```

---

## Task 12: API router + schemas

**Files:**
- Create: `nexus/network/schemas.py`, `nexus/api/routers/network.py`
- Modify: `nexus/api/routers/__init__.py`
- Test: `tests/test_network_api.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_api.py
from __future__ import annotations

import pytest

from tests.conftest import auth, client, signup


async def test_network_end_to_end_over_http(client):
    token = await signup(client, slug="acme", email="rep@acme.com", company="Acme")
    h = auth(token)

    # connect a fixture source
    r = await client.post("/api/network/accounts",
                          json={"provider": "fixture", "external_account_id": "rep@acme.com"},
                          headers=h)
    assert r.status_code == 201, r.text
    account_id = r.json()["id"]
    assert r.json()["pooling_enabled"] is False

    # import a batch inline
    r = await client.post(
        f"/api/network/accounts/{account_id}/import",
        json={
            "identities": [
                {"external_id": "g1", "email": "ann@health.com", "name": "Ann Lee",
                 "title": "CTO", "company": "HealthCo"},
            ],
            "touchpoints": [
                {"person_external_id": "g1", "kind": "email_sent", "at": "2026-06-29T00:00:00Z"},
            ],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_persons"] == 1

    # search finds Ann
    r = await client.post("/api/network/search", json={"query": "CTO at HealthCo"}, headers=h)
    assert r.status_code == 200, r.text
    hits = r.json()
    assert hits[0]["person"]["full_name"] == "Ann Lee"
    person_id = hits[0]["person"]["id"]

    # intro paths attribute the broker
    r = await client.get(f"/api/network/people/{person_id}/intro-paths", headers=h)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1
    assert r.json()[0]["strength"] > 0

    # list accounts never leaks oauth
    r = await client.get("/api/network/accounts", headers=h)
    assert "oauth" not in r.json()[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_api.py -v`
Expected: FAIL — 404 on `/api/network/accounts` (router not registered)

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/schemas.py
"""Request/response models for the /network router. OAuth is never exposed."""
from __future__ import annotations

from pydantic import BaseModel, Field

from nexus.network.connectors.base import RawIdentity, Touchpoint


class ConnectRequest(BaseModel):
    provider: str
    external_account_id: str
    display_email: str = ""


class PatchAccountRequest(BaseModel):
    pooling_enabled: bool | None = None
    status: str | None = None


class NetworkAccountOut(BaseModel):
    id: str
    provider: str
    external_account_id: str
    display_email: str
    status: str
    pooling_enabled: bool
    last_synced_at: str | None = None


class ImportRequest(BaseModel):
    identities: list[RawIdentity] = Field(default_factory=list)
    touchpoints: list[Touchpoint] = Field(default_factory=list)
    next_cursor: str | None = None


class IngestResultOut(BaseModel):
    identities: int
    new_persons: int
    new_edges: int


class PersonOut(BaseModel):
    id: str
    primary_email: str | None
    full_name: str
    title: str
    company: str
    location: str


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=20, ge=1, le=100)


class SearchHitOut(BaseModel):
    person: PersonOut
    score: float
    best_strength: int
    broker_member_ids: list[str]


class IntroPathOut(BaseModel):
    broker_member_id: str
    broker_user_id: str
    relation: str
    strength: int
    last_touch_at: str | None
    provider: str
```

```python
# nexus/api/routers/network.py
"""Relationship Graph endpoints (A1 search, A4 intro-paths): connect a network source, import or
sync its contacts, then search the deduped graph and map warm intros. Tenant-scoped + RBAC; OAuth
is never serialized out; cross-member reads pass the pooling visibility predicate."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from nexus.api.deps import Principal, get_principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.identity import Membership
from nexus.models.network import NetworkEdge, NetworkPerson, NetworkSourceAccount
from nexus.network.intro import intro_paths
from nexus.network.schemas import (
    ConnectRequest,
    ImportRequest,
    IngestResultOut,
    IntroPathOut,
    NetworkAccountOut,
    PatchAccountRequest,
    PersonOut,
    SearchHitOut,
    SearchRequest,
)
from nexus.network.search import search_network
from nexus.network.service import ingest_batch, set_pooling
from nexus.network.connectors.base import NetworkSyncBatch
from nexus.workers.tasks import enqueue_sync_network_account

router = APIRouter(prefix="/network", tags=["network"])


async def _member(ts: TenantSession, principal: Principal) -> Membership:
    m = await ts.first(Membership, Membership.user_id == principal.user_id)
    if m is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no membership for current user")
    return m


def _account_out(a: NetworkSourceAccount) -> NetworkAccountOut:
    return NetworkAccountOut(
        id=a.id, provider=a.provider, external_account_id=a.external_account_id,
        display_email=a.display_email, status=a.status, pooling_enabled=a.pooling_enabled,
        last_synced_at=a.last_synced_at.isoformat() if a.last_synced_at else None,
    )


def _person_out(p: NetworkPerson) -> PersonOut:
    return PersonOut(
        id=p.id, primary_email=p.primary_email, full_name=p.full_name,
        title=p.title, company=p.company, location=p.location,
    )


async def _owned_account(ts: TenantSession, member: Membership, account_id: str) -> NetworkSourceAccount:
    acc = await ts.get(NetworkSourceAccount, account_id)
    if acc is None or acc.member_id != member.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")
    return acc


@router.post("/accounts", response_model=NetworkAccountOut, status_code=status.HTTP_201_CREATED)
async def connect_account(
    body: ConnectRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> NetworkAccountOut:
    member = await _member(ts, principal)
    acc = NetworkSourceAccount(
        member_id=member.id, user_id=principal.user_id, provider=body.provider,
        external_account_id=body.external_account_id,
        display_email=body.display_email or body.external_account_id,
    )
    ts.add(acc)
    await ts.flush()
    return _account_out(acc)


@router.get("/accounts", response_model=list[NetworkAccountOut])
async def list_accounts(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> list[NetworkAccountOut]:
    member = await _member(ts, principal)
    rows = await ts.list(NetworkSourceAccount, NetworkSourceAccount.member_id == member.id)
    return [_account_out(a) for a in rows]


@router.patch("/accounts/{account_id}", response_model=NetworkAccountOut)
async def patch_account(
    account_id: str,
    body: PatchAccountRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> NetworkAccountOut:
    member = await _member(ts, principal)
    acc = await _owned_account(ts, member, account_id)
    if body.pooling_enabled is not None:
        await set_pooling(ts, acc, body.pooling_enabled)
    if body.status is not None:
        acc.status = body.status
    await ts.flush()
    return _account_out(acc)


@router.post("/accounts/{account_id}/import", response_model=IngestResultOut)
async def import_batch(
    account_id: str,
    body: ImportRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> IngestResultOut:
    member = await _member(ts, principal)
    acc = await _owned_account(ts, member, account_id)
    batch = NetworkSyncBatch(
        identities=body.identities, touchpoints=body.touchpoints, next_cursor=body.next_cursor
    )
    res = await ingest_batch(ts, acc, batch)
    return IngestResultOut(**res)


@router.post("/accounts/{account_id}/sync")
async def sync_account(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> dict:
    member = await _member(ts, principal)
    acc = await _owned_account(ts, member, account_id)
    await enqueue_sync_network_account(ts.tenant_id, acc.id)
    return {"enqueued": True, "account_id": acc.id}


@router.post("/search", response_model=list[SearchHitOut])
async def search(
    body: SearchRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> list[SearchHitOut]:
    member = await _member(ts, principal)
    hits = await search_network(ts, member_id=member.id, query=body.query, limit=body.limit)
    return [
        SearchHitOut(
            person=_person_out(h.person), score=round(h.score, 4),
            best_strength=h.best_strength, broker_member_ids=h.broker_member_ids,
        )
        for h in hits
    ]


@router.get("/people/{person_id}", response_model=PersonOut)
async def get_person(
    person_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> PersonOut:
    member = await _member(ts, principal)
    person = await ts.get(NetworkPerson, person_id)
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")
    # only surface a person the member can actually see (owns or pooled edge)
    paths = await intro_paths(ts, person_id=person_id, member_id=member.id)
    if not paths:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")
    return _person_out(person)


@router.get("/people/{person_id}/intro-paths", response_model=list[IntroPathOut])
async def get_intro_paths(
    person_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> list[IntroPathOut]:
    member = await _member(ts, principal)
    paths = await intro_paths(ts, person_id=person_id, member_id=member.id)
    return [
        IntroPathOut(
            broker_member_id=p.broker_member_id, broker_user_id=p.broker_user_id,
            relation=p.relation, strength=p.strength,
            last_touch_at=p.last_touch_at.isoformat() if p.last_touch_at else None,
            provider=p.provider,
        )
        for p in paths
    ]
```

In `nexus/api/routers/__init__.py`, add `network` to the import block (keep alphabetical order, after `integrations,`):

```python
    network,
```

and add `network.router,` to the `all_routers` list (after `integrations.router,`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/network/schemas.py nexus/api/routers/network.py nexus/api/routers/__init__.py tests/test_network_api.py
git commit -m "feat(network): /network router (connect, import, sync, search, intro-paths)"
```

---

## Task 13: Privacy / visibility test

**Files:**
- Test: `tests/test_network_privacy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_privacy.py
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from tests.conftest import make_tenant, tenant_session


async def _member(ts, *, pooling):
    from nexus.models.identity import Membership, User
    from nexus.models.network import NetworkSourceAccount

    u = User(email=f"u{uuid.uuid4().hex}@x.com", full_name="U", password_hash="x")
    ts.session.add(u)
    await ts.session.flush()
    m = Membership(user_id=u.id, role="rep")
    ts.add(m)
    await ts.session.flush()
    acc = NetworkSourceAccount(
        member_id=m.id, user_id=u.id, provider="fixture",
        external_account_id=u.email, pooling_enabled=pooling,
    )
    ts.add(acc)
    await ts.session.flush()
    return m, acc


async def test_non_pooled_edges_are_invisible_to_other_members():
    from nexus.models.network import NetworkPerson
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity
    from nexus.network.intro import intro_paths
    from nexus.network.search import search_network
    from nexus.network.service import ingest_batch, set_pooling

    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        alice, alice_acc = await _member(ts, pooling=False)  # private
        bob, _ = await _member(ts, pooling=False)
        await ingest_batch(ts, alice_acc, NetworkSyncBatch(
            identities=[RawIdentity(external_id="t1", email="t@corp.com", name="Tess",
                                    title="VP", company="Corp")],
        ), now=now)
        person = (await ts.list(NetworkPerson))[0]

        # Alice (owner) sees it; Bob does not.
        assert len(await intro_paths(ts, person_id=person.id, member_id=alice.id)) == 1
        assert len(await intro_paths(ts, person_id=person.id, member_id=bob.id)) == 0
        assert len(await search_network(ts, member_id=bob.id, query="VP Corp")) == 0

        # Alice opts into pooling → Bob can now see + intro through her.
        await set_pooling(ts, alice_acc, True)
        assert len(await intro_paths(ts, person_id=person.id, member_id=bob.id)) == 1
        hits = await search_network(ts, member_id=bob.id, query="VP Corp")
        assert hits and hits[0].broker_member_ids == [alice.id]


async def test_graph_is_tenant_isolated():
    from nexus.models.network import NetworkPerson
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity
    from nexus.network.service import ingest_batch

    t1 = await make_tenant(slug="t1")
    t2 = await make_tenant(slug="t2")
    async with tenant_session(t1) as ts:
        _, acc = await _member(ts, pooling=True)
        await ingest_batch(ts, acc, NetworkSyncBatch(
            identities=[RawIdentity(external_id="x", email="a@a.com", name="A")]
        ))
    async with tenant_session(t2) as ts:
        assert await ts.list(NetworkPerson) == []  # t2 cannot see t1's graph
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_privacy.py -v`
Expected: PASS immediately (the visibility predicate + tenancy already enforce this from earlier tasks). If either test FAILS, fix `visible_edges_where` / `set_pooling` before continuing — this task is the guard that proves the privacy posture.

- [ ] **Step 3: (no new implementation expected)**

This task is a behavioral guard over Tasks 8/10/11. If it passes, proceed. If not, the failure localizes to `nexus/network/service.py` (visibility predicate or pooling mirror).

- [ ] **Step 4: Run the full network suite**

Run: `python -m pytest tests/ -k network -v`
Expected: PASS (all network tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_network_privacy.py
git commit -m "test(network): private-by-default pooling + tenant isolation"
```

---

## Task 14: Production migration

**Files:**
- Create: `migrations/versions/0018_relationship_graph.py`
- Test: `tests/test_network_migration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_migration.py
from __future__ import annotations


def test_migration_columns_match_models():
    """The migration must create every column the ORM models declare (prod parity with create_all)."""
    import importlib

    import nexus.models  # noqa: F401  (register mappers)
    from nexus.core.db import Base

    mod = importlib.import_module("migrations.versions.0018_relationship_graph")
    assert mod.down_revision == "0017_password_reset"
    assert mod.revision == "0018_relationship_graph"

    # Every network table the ORM knows about must be created by the migration source.
    import inspect

    src = inspect.getsource(mod.upgrade)
    for table in (
        "network_source_accounts", "network_persons", "network_identities", "network_edges",
    ):
        assert table in Base.metadata.tables
        assert f'"{table}"' in src or f"'{table}'" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_migration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'migrations.versions.0018_relationship_graph'`

- [ ] **Step 3: Write minimal implementation**

```python
# migrations/versions/0018_relationship_graph.py
"""Relationship Graph (A1–A5): network source accounts, persons, identities, edges.

Revision ID: 0018_relationship_graph
Revises: 0017_password_reset
Create Date: 2026-06-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_relationship_graph"
down_revision = "0017_password_reset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "network_source_accounts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("member_id", sa.String(length=32), sa.ForeignKey("memberships.id"), index=True),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id")),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("external_account_id", sa.String(length=255), nullable=False),
        sa.Column("display_email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="connected"),
        sa.Column("pooling_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("oauth", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("sync_cursor", sa.String(length=255), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id", "member_id", "provider", "external_account_id", name="uq_network_source"
        ),
    )
    op.create_index("ix_network_source_member", "network_source_accounts", ["tenant_id", "member_id"])

    op.create_table(
        "network_persons",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("primary_email", sa.String(length=255), nullable=True),
        sa.Column("full_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("first_name", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("last_name", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("company", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("company_domain", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("location", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("country", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("linkedin_url", sa.String(length=300), nullable=True),
        sa.Column("twitter_handle", sa.String(length=100), nullable=True),
        sa.Column("photo_url", sa.String(length=500), nullable=True),
        sa.Column("profile", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("search_text", sa.String(length=600), nullable=False, server_default="", index=True),
        sa.Column("identity_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("edge_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_network_person_email", "network_persons", ["tenant_id", "primary_email"])
    op.create_index("ix_network_person_domain", "network_persons", ["tenant_id", "company_domain"])

    op.create_table(
        "network_identities",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("source_account_id", sa.String(length=32),
                  sa.ForeignKey("network_source_accounts.id"), index=True),
        sa.Column("person_id", sa.String(length=32), sa.ForeignKey("network_persons.id"), nullable=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column("handle", sa.String(length=100), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("resolution_key", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "source_account_id", "external_id", name="uq_network_identity"),
    )
    op.create_index("ix_network_identity_key", "network_identities", ["tenant_id", "resolution_key"])
    op.create_index("ix_network_identity_person", "network_identities", ["tenant_id", "person_id"])

    op.create_table(
        "network_edges",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("owner_member_id", sa.String(length=32), sa.ForeignKey("memberships.id"), index=True),
        sa.Column("owner_user_id", sa.String(length=32), sa.ForeignKey("users.id")),
        sa.Column("person_id", sa.String(length=32), sa.ForeignKey("network_persons.id"), index=True),
        sa.Column("source_account_id", sa.String(length=32),
                  sa.ForeignKey("network_source_accounts.id")),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("relation", sa.String(length=16), nullable=False, server_default="contact"),
        sa.Column("strength", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("email_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("received_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("meeting_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_touch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_touch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mutual_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pooling_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "owner_member_id", "person_id", "provider", name="uq_network_edge"),
    )
    op.create_index(
        "ix_network_edge_person", "network_edges",
        ["tenant_id", "person_id", "pooling_enabled", "strength"],
    )
    op.create_index("ix_network_edge_owner", "network_edges", ["tenant_id", "owner_member_id"])


def downgrade() -> None:
    op.drop_table("network_edges")
    op.drop_table("network_identities")
    op.drop_table("network_persons")
    op.drop_table("network_source_accounts")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/0018_relationship_graph.py tests/test_network_migration.py
git commit -m "feat(network): additive migration 0018 (relationship-graph tables)"
```

---

## Task 15: Full-suite regression check

**Files:** none (verification only)

- [ ] **Step 1: Run the whole suite to prove nothing existing broke**

Run: `python -m pytest -q`
Expected: PASS — the entire pre-existing suite is green and the new `test_network_*` tests pass. If anything in the existing suite fails, the cause is one of the three append-only edits (`models/__init__.py`, `workers/tasks.py`, `api/routers/__init__.py`); revert the offending line and re-run.

- [ ] **Step 2: Commit (only if any fix was needed)**

```bash
git add -A
git commit -m "fix(network): resolve regression from registration edits"
```

---

## Self-review

**Spec coverage (Phases 1–2):**
- Models / migration / TenantSession wiring → Tasks 1, 2, 14.
- FixtureConnector + `/import` inline ingestion → Tasks 4, 12.
- Identity resolution (deterministic refinement) → Task 6.
- Connection strength (materialized) → Tasks 7, 8.
- Sync worker job (incremental via `sync_cursor`) → Task 9.
- NL search A1 (parse + rank + visibility) → Task 10.
- Warm-intro mapping A4 → Task 11.
- `visible_edges` privacy predicate + pooling toggle → Tasks 8, 13.
- API surface (accounts CRUD, import, sync, search, person, intro-paths) → Task 12.
- Non-breaking regression guard → Task 15.
- Deferred to later plans (correctly out of this plan's scope): A5 profiling, Google/Microsoft OAuth adapters, frontend, team stats, RLS policies, Redis projection cache, `GET /network/stats`.

**Placeholder scan:** No `TBD`/`TODO` in implementation code. Every code step ships complete, runnable code. (The connectors' `oauth` field is an intentional, documented write-only seam — not a placeholder.)

**Type consistency check:**
- `ingest_batch(ts, account, batch, *, now=None) -> dict{"identities","new_persons","new_edges"}` — produced in Task 8, consumed identically in Tasks 9, 12 (`IngestResultOut(**res)`) and asserted in tests.
- `visible_edges_where(member_id)` — defined in Task 8, used in Tasks 10, 11.
- `search_network(ts, *, member_id, query, limit=20) -> list[SearchHit]` and `SearchHit{person, score, best_strength, broker_member_ids}` — Task 10, consumed in Task 12.
- `intro_paths(ts, *, person_id, member_id) -> list[IntroPath]` and `IntroPath{broker_member_id, broker_user_id, relation, strength, last_touch_at, provider}` — Task 11, consumed in Task 12.
- `resolve_person(ts, *, email, name, title, company)` / `resolution_key(*, email, name, company)` — Task 6, used in Task 8.
- `score_edge(EdgeStats, *, now=None)` / `EdgeStats(relation, email_count, sent_count, received_count, meeting_count, last_touch_at)` — Task 7, used in Task 8.
- `set_network_connector` / `get_network_connector` — Task 5, used in Tasks 9 (+ its test).
- Strength assertion arithmetic: email tier 20 + recency(≤30d) 30 + freq `min(25, 2·email_count)` + reciprocity 15. Task 8's Ann edge has `email_count=2` → freq `min(25,4)=4` → 20+30+4+15 = **69** (matches the test). Task 7's `recent` has `email_count=4` → freq `min(25,8)=8` → 20+30+8+15 = **73** (matches).
