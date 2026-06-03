# Conversational Orchestrator — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a chat-first orchestrator to NEXUS GTM: the user states an ICP in natural language, a deterministic brain asks clarifying questions only when a required slot is missing, then launches a read-only **discovery** run that surfaces matching companies/contacts (own data ranked by ICP fit, web to fill gaps). Conversation is token-frugal, per-workspace, resumable, and links to the run console. Adds CSV proprietary-data ingest and cross-workspace switching.

**Architecture:** A new chat layer sits *beside* the existing run engine and reuses it unchanged. `IntakeController` (the brain) runs a deterministic control loop over a token-budgeted `ContextEnvelope`: a pure-Python `Extractor` fills ICP slots, `missing_required()` gates, an LLM `Phraser` asks one question, an LLM `Summarizer` keeps a rolling ~150-token summary. When the ICP is complete it calls `engine.create_run("discover", …)`. `DiscoveryAgent` (account=None) ranks the tenant's accounts with `RelevanceEngine.score_icp_fit`, then web-fills net-new `Account(source="discovery")` rows. New tables: `ChatSession`, `ChatMessage`, `CustomFieldDef`; `custom_fields` JSON added to `Account`/`Contact`; `chat_session_id` FK added to `OrchestrationRun`. Tenant-switch endpoints re-issue the JWT after re-verifying membership.

**Tech Stack:** Python 3.10, FastAPI, async SQLAlchemy 2.0 (SQLite offline / Postgres prod), Pydantic v2, pytest (`asyncio_mode=auto`), stub LLM + stubbed browser for deterministic offline tests. All tables `TenantScoped` (+ Postgres RLS).

**Scope:** This is the **backend** plan (spec §11 phases 1–5). The frontend (ChatPage, mini-chat, results panel, CSV modal, workspace switcher, client wiring — spec §8) is a **separate plan** written after this one lands. Spec: `docs/superpowers/specs/2026-06-02-conversational-orchestrator-design.md`.

**Conventions to follow (read before starting):**
- Every new model uses `IdMixin, TimestampMixin, TenantScoped, Base` (see `nexus/models/account.py`).
- `utcnow()` / `ensure_aware()` live in `nexus/core/db.py`. Never use naive datetimes in arithmetic.
- Tenant scoping is automatic via `TenantSession` (`ts.add`, `ts.get`, `ts.select`, `ts.first`, `ts.list`). Never touch the raw session for tenant data.
- JSON columns mutated in place need `flag_modified(obj, "col")` to persist (see `engine.py`).
- Tests bind a tenant with `async with tenant_session(tid) as ts:` (from `tests/conftest.py` / `nexus.workers.tasks`).
- Run the suite with `python -m pytest -q` from the repo root. Baseline is **62 passing**; every task keeps it green.

---

## File structure (what each new file owns)

| File | Responsibility |
|---|---|
| `nexus/models/chat.py` | `ChatSession`, `ChatMessage`, `CustomFieldDef` ORM models |
| `nexus/orchestration/intake.py` | Slot schema, `missing_required`, `Extractor`, `Phraser`, `Summarizer`, `ContextEnvelope`, `IntakeController` |
| `nexus/orchestration/chat_service.py` | `ChatService`: create/append/list sessions, run the control loop, persist messages with monotonic `seq` |
| `nexus/orchestration/chat_schemas.py` | Pydantic wire models for chat + discovery results + custom fields |
| `nexus/agents/discovery.py` | `DiscoveryAgent` (own-data rank + web gap-fill) |
| `nexus/api/routers/chat.py` | Chat REST + SSE router (`/orchestration/chat/...`) |
| `nexus/api/routers/custom_fields.py` | Custom-field CRUD + CSV import (`/custom-fields/...`) |
| `nexus/custom_fields/service.py` | `CustomFieldService`: CRUD + CSV upsert (stdlib `csv`) |

Modified: `nexus/models/account.py`, `nexus/models/orchestration.py`, `nexus/models/__init__.py`, `nexus/core/config.py`, `nexus/agents/llm.py`, `nexus/agents/runtime.py`, `nexus/orchestration/tools.py`, `nexus/orchestration/planner.py`, `nexus/api/routers/auth.py`, `nexus/api/routers/orchestration.py`, `nexus/api/routers/__init__.py`, `nexus/api/schemas.py`.

---

## Phase 1 — Data model + settings

Adds the three new tables, the two column changes, the FK, model registration, and the four new settings. Because tests recreate tables from `Base.metadata` (`conftest.fresh_db`), no migration is needed for the suite to pass; the Postgres Alembic migration is a final-phase task.

### Task 1.1: ChatSession / ChatMessage / CustomFieldDef models

**Files:**
- Create: `nexus/models/chat.py`
- Test: `tests/test_chat_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_models.py
"""Chat + custom-field models persist under tenant scoping with a monotonic seq."""
from __future__ import annotations

import pytest

from nexus.models.chat import ChatMessage, ChatSession, CustomFieldDef
from tests.conftest import make_tenant, tenant_session


async def test_chat_session_and_messages_persist():
    tid = await make_tenant("t-chat")
    async with tenant_session(tid) as ts:
        session = ChatSession(tenant_id=tid, title="Find fintech", target="companies",
                              icp_state={"industries": ["fintech"]})
        ts.add(session)
        await ts.flush()
        ts.add(ChatMessage(tenant_id=tid, session_id=session.id, seq=1, role="user",
                           kind="text", content="find fintech in the US"))
        ts.add(ChatMessage(tenant_id=tid, session_id=session.id, seq=2, role="assistant",
                           kind="clarifying_question", content="What size?",
                           data={"slot": "company_size"}))
        await ts.flush()
        rows = await ts.list(ChatMessage, ChatMessage.session_id == session.id)
        assert sorted(m.seq for m in rows) == [1, 2]
        assert session.status == "active"
        assert session.icp_state == {"industries": ["fintech"]}


async def test_chat_message_seq_unique_per_session():
    tid = await make_tenant("t-seq")
    async with tenant_session(tid) as ts:
        session = ChatSession(tenant_id=tid, title="s")
        ts.add(session)
        await ts.flush()
        ts.add(ChatMessage(tenant_id=tid, session_id=session.id, seq=1, role="user", content="a"))
        await ts.flush()
        # Contain the deliberate IntegrityError in a savepoint so the outer transaction
        # (which tenant_session commits on exit) stays usable.
        with pytest.raises(Exception):
            async with ts.session.begin_nested():
                ts.add(ChatMessage(tenant_id=tid, session_id=session.id, seq=1, role="user", content="b"))
                await ts.session.flush()


async def test_custom_field_def_unique_key():
    tid = await make_tenant("t-cf")
    async with tenant_session(tid) as ts:
        ts.add(CustomFieldDef(tenant_id=tid, entity="account", key="arr", label="ARR", kind="number"))
        await ts.flush()
        with pytest.raises(Exception):
            async with ts.session.begin_nested():
                ts.add(CustomFieldDef(tenant_id=tid, entity="account", key="arr", label="ARR2", kind="number"))
                await ts.session.flush()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_chat_models.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.models.chat'`.

- [ ] **Step 3: Create the models**

```python
# nexus/models/chat.py
"""Conversational orchestrator: chat sessions, append-only messages, custom-field registry.

A ChatSession is a token-frugal conversation that builds an ICP and launches discovery runs.
Messages are append-only with a monotonic ``seq`` per session (powers SSE replay, mirrors
RunEvent). CustomFieldDef is the per-tenant registry that gives proprietary data on
Account/Contact (stored as JSON ``custom_fields``) its column metadata and a CSV mapping target.

All tables are tenant-scoped — a conversation never reads or writes across tenant boundaries.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped

# Session lifecycle.
CHAT_ACTIVE = "active"
CHAT_ARCHIVED = "archived"

# Message roles / kinds.
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_SYSTEM = "system"
KIND_TEXT = "text"
KIND_CLARIFYING = "clarifying_question"
KIND_RUN_LAUNCHED = "run_launched"
KIND_NOTICE = "notice"

# Custom-field entities / kinds.
ENTITY_ACCOUNT = "account"
ENTITY_CONTACT = "contact"
CF_KINDS = frozenset({"text", "number", "date", "bool", "url"})


class ChatSession(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ix_chat_session_tenant_account", "tenant_id", "account_id"),
        Index("ix_chat_session_tenant_status", "tenant_id", "status"),
    )

    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # The account/"client" the conversation centers on; null for pure ICP discovery.
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, index=True
    )
    # Branch/continue a prior conversation (inherits its summary + icp_state).
    parent_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_sessions.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(160), default="New conversation")
    status: Mapped[str] = mapped_column(String(16), default=CHAT_ACTIVE, index=True)
    target: Mapped[str | None] = mapped_column(String(16), nullable=True)  # companies | contacts
    icp_state: Mapped[dict] = mapped_column(JSON, default=dict)
    missing_slots: Mapped[list] = mapped_column(JSON, default=list)
    context_summary: Mapped[str] = mapped_column(Text, default="")


class ChatMessage(IdMixin, TimestampMixin, TenantScoped, Base):
    """Append-only. ``seq`` is monotonic within a session so an SSE client can resume from
    its ``Last-Event-ID`` without gaps or duplicates (mirrors RunEvent)."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_chat_msg_seq"),
        Index("ix_chat_msg_session_seq", "session_id", "seq"),
    )

    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(12))
    kind: Mapped[str] = mapped_column(String(24), default=KIND_TEXT)
    content: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class CustomFieldDef(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "custom_field_defs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "entity", "key", name="uq_custom_field_key"),
    )

    entity: Mapped[str] = mapped_column(String(12))  # account | contact
    key: Mapped[str] = mapped_column(String(60))     # machine key
    label: Mapped[str] = mapped_column(String(120))  # display
    kind: Mapped[str] = mapped_column(String(12), default="text")
```

- [ ] **Step 4: Register the mappers**

In `nexus/models/__init__.py`, add the import and `__all__` entries:

```python
from nexus.models.chat import ChatMessage, ChatSession, CustomFieldDef
```

Add `"ChatSession"`, `"ChatMessage"`, `"CustomFieldDef"` to the `__all__` list.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_chat_models.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add nexus/models/chat.py nexus/models/__init__.py tests/test_chat_models.py
git commit -m "feat(chat): add ChatSession/ChatMessage/CustomFieldDef models"
```

### Task 1.2: Column additions — Account/Contact.custom_fields, OrchestrationRun.chat_session_id

**Files:**
- Modify: `nexus/models/account.py`, `nexus/models/orchestration.py`
- Test: `tests/test_chat_models.py` (append)

- [ ] **Step 1: Add the failing test (append to `tests/test_chat_models.py`)**

```python
async def test_account_contact_custom_fields_default_empty():
    from nexus.models.account import Account, Contact
    tid = await make_tenant("t-custom")
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
        ts.add(acc)
        await ts.flush()
        c = Contact(tenant_id=tid, account_id=acc.id, full_name="Jo")
        ts.add(c)
        await ts.flush()
        assert acc.custom_fields == {}
        assert c.custom_fields == {}
        assert acc.source is None
        acc.custom_fields = {"arr": 120000}
        acc.source = "discovery"
        await ts.flush()
        again = await ts.get(Account, acc.id)
        assert again.custom_fields["arr"] == 120000
        assert again.source == "discovery"


async def test_run_has_chat_session_id_column():
    from nexus.models.orchestration import OrchestrationRun
    assert hasattr(OrchestrationRun, "chat_session_id")
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_chat_models.py -q`
Expected: FAIL — `AttributeError: 'Account' object has no attribute 'custom_fields'`.

- [ ] **Step 3: Add the columns**

In `nexus/models/account.py`, add to **both** `Account` and `Contact` (after their last column, before relationships):

```python
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)
```

Additionally add to `Account` **only** (provenance for discovery-sourced rows; lets the
results view filter own-data vs web-discovered accounts):

```python
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
```

`JSON` and `String` are already imported in that file.

In `nexus/models/orchestration.py`, add to `OrchestrationRun` (after `created_by`):

```python
    chat_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_sessions.id"), nullable=True, index=True
    )
```

`ForeignKey` is already imported.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_chat_models.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Run the full suite (guard against mapper/FK regressions)**

Run: `python -m pytest -q`
Expected: PASS (was 62; now 62 + new chat-model tests).

- [ ] **Step 6: Commit**

```bash
git add nexus/models/account.py nexus/models/orchestration.py tests/test_chat_models.py
git commit -m "feat(chat): add custom_fields JSON + run.chat_session_id link"
```

### Task 1.3: Settings — chat token budget + discovery cap

**Files:**
- Modify: `nexus/core/config.py`
- Test: `tests/test_chat_models.py` (append)

- [ ] **Step 1: Add the failing test**

```python
def test_chat_settings_defaults():
    from nexus.core.config import Settings
    s = Settings()
    assert s.orch_chat_token_budget == 1200
    assert s.orch_chat_recency_window == 4
    assert s.orch_chat_summary_token_cap == 150
    assert s.discovery_max_candidates == 25
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_chat_models.py::test_chat_settings_defaults -q`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Add the settings**

In `nexus/core/config.py`, after the `# Orchestration engine` block, add:

```python
    # Conversational orchestrator (chat) — token-frugal context envelope.
    orch_chat_token_budget: int = 1200       # hard cap on the per-turn LLM payload
    orch_chat_recency_window: int = 4         # last K raw messages kept verbatim
    orch_chat_summary_token_cap: int = 150    # rolling summary ceiling (approx tokens)
    discovery_max_candidates: int = 25        # cap on discovery result list size
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_chat_models.py::test_chat_settings_defaults -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nexus/core/config.py tests/test_chat_models.py
git commit -m "feat(chat): add chat token-budget + discovery cap settings"
```

---

## Phase 2 — IntakeController + context envelope

**Spec:** §4. Implements the orchestrator "brain" in `nexus/orchestration/intake.py` plus two new
deterministic stub branches in `nexus/agents/llm.py`.

**Design note (deliberate, spec-aligned):** Spec §4.2 lists three LLM units (Extractor, Phraser,
Summarizer). We implement **Extractor as deterministic pure-Python** (country map + size regex +
industry keywords + slot coercion on the pending slot) and reserve the LLM only for **Phraser** and
**Summarizer**. This is the most token-frugal reading of the spec's overriding requirement ("consume
very low tokens per turn") and keeps slot-filling fully reproducible offline. The `LLMProvider` seam
is unchanged, so a model-backed extractor remains a drop-in later. Phraser/Summarizer each get a
deterministic stub branch so the whole brain runs offline in CI.

### Task 2.1: Slot schema + `missing_required` (pure Python)

**Files:**
- Create: `nexus/orchestration/intake.py`
- Test: `tests/test_intake.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intake.py
"""Unit tests for the deterministic orchestrator brain (no DB, no network)."""
from __future__ import annotations

from nexus.orchestration.intake import missing_required


def test_missing_required_companies_truth_table():
    # Empty ICP, companies target → industries, geo, company_size all missing.
    assert missing_required({}, "companies") == ["industries", "geo", "company_size"]
    # Industries present (or description) clears the first slot.
    assert missing_required({"industries": ["Fintech"]}, "companies") == ["geo", "company_size"]
    assert missing_required({"icp_description": "B2B fintech"}, "companies") == [
        "geo",
        "company_size",
    ]
    # Geo present.
    assert missing_required(
        {"industries": ["Fintech"], "geo": ["United States"]}, "companies"
    ) == ["company_size"]
    # Fully specified companies ICP → nothing missing.
    assert (
        missing_required(
            {
                "industries": ["Fintech"],
                "geo": ["United States"],
                "company_size": {"min": 200, "max": 5000},
            },
            "companies",
        )
        == []
    )


def test_missing_required_contacts_needs_titles_not_size():
    base = {"industries": ["Fintech"], "geo": ["US"]}
    assert missing_required(base, "contacts") == ["titles"]
    assert missing_required({**base, "titles": ["VP Sales"]}, "contacts") == []


def test_missing_required_defaults_target_to_companies():
    # target None behaves like "companies".
    assert missing_required({}, None) == ["industries", "geo", "company_size"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_intake.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.orchestration.intake'`.

- [ ] **Step 3: Create the slot schema + `missing_required`**

```python
# nexus/orchestration/intake.py
"""The orchestrator brain: deterministic ICP slot-filling + token-frugal context envelope.

The control surface is intentionally split:
* **Deterministic core** (this is the bulk): the slot schema, ``missing_required`` truth table,
  the pure-Python ``extract_slots`` (country map + size regex + industry keywords + coercion on
  the pending slot), and the merge rules. No LLM, no DB, no network — fully unit-testable.
* **LLM phrasing only**: :class:`IntakeController` calls the provider for two things — phrasing the
  single next question (purpose ``clarify_question``) and folding the rolling summary (purpose
  ``chat_summary``). Both have deterministic stub branches so CI is reproducible.

The :class:`ContextEnvelope` is the token-frugal payload: structured state + a capped rolling
summary + the last K raw messages, hard-bounded by ``orch_chat_token_budget``. The full transcript
is persisted for display but never replayed to the model, so per-turn cost stays ~flat.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from nexus.agents.llm import LLMMessage, LLMProvider, get_llm_provider
from nexus.core.config import get_settings

# -- Slot schema -----------------------------------------------------------------------
# List-valued slots are merged by case-insensitive ordered union; scalar/dict slots override.
LIST_SLOTS = ("industries", "geo", "required_tech", "titles", "intent_signals", "exclusions")
TARGET_COMPANIES = "companies"
TARGET_CONTACTS = "contacts"


def _norm_target(target: str | None) -> str:
    return target if target in (TARGET_COMPANIES, TARGET_CONTACTS) else TARGET_COMPANIES


def missing_required(icp_state: dict, target: str | None) -> list[str]:
    """Pure-Python truth table: which required slots are still empty.

    Always-required: an industry signal (``industries`` or a free-text ``icp_description``) and
    ``geo``. Companies additionally require ``company_size``; contacts require ``titles``/seniority.
    Order is priority order — the controller asks for ``missing[0]`` first.
    """
    target = _norm_target(target)
    missing: list[str] = []
    if not (icp_state.get("industries") or icp_state.get("icp_description")):
        missing.append("industries")
    if not icp_state.get("geo"):
        missing.append("geo")
    if target == TARGET_COMPANIES:
        size = icp_state.get("company_size") or {}
        if not (size.get("min") or size.get("max")):
            missing.append("company_size")
    else:  # contacts
        if not (icp_state.get("titles") or icp_state.get("seniority")):
            missing.append("titles")
    return missing
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_intake.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add nexus/orchestration/intake.py tests/test_intake.py
git commit -m "feat(intake): ICP slot schema + missing_required truth table"
```

### Task 2.2: Deterministic extractor + merge

**Files:**
- Modify: `nexus/orchestration/intake.py`
- Test: `tests/test_intake.py` (append)

- [ ] **Step 1: Add the failing test (append to `tests/test_intake.py`)**

```python
from nexus.orchestration.intake import extract_slots, merge_icp


def test_extract_rich_first_message_fills_multiple_slots():
    delta = extract_slots("Find B2B fintech companies in the US with 200-5000 employees", {}, None)
    assert "Fintech" in delta["industries"]
    assert "United States" in delta["geo"]
    assert delta["company_size"] == {"min": 200, "max": 5000}


def test_extract_named_size_bands():
    assert extract_slots("mid-market", {}, "company_size")["company_size"] == {"min": 200, "max": 1000}
    assert extract_slots("enterprise only", {}, "company_size")["company_size"] == {"min": 1000, "max": None}
    assert extract_slots("under 500", {}, "company_size")["company_size"] == {"min": None, "max": 500}
    assert extract_slots("over 1000", {}, "company_size")["company_size"] == {"min": 1000, "max": None}


def test_extract_coerces_bare_answer_to_pending_slot():
    # Answering a geo question with a bare country name still fills geo.
    assert extract_slots("Canada and Germany", {}, "geo")["geo"] == ["Canada", "Germany"]
    # Answering an industries question with an unknown noun phrase still fills industries.
    assert extract_slots("logistics tech", {}, "industries")["industries"] == ["Logistics Tech"]
    # Answering a titles question.
    assert extract_slots("VP Sales, CRO", {}, "titles")["titles"] == ["VP Sales", "CRO"]


def test_merge_unions_lists_and_overrides_size():
    state = {"industries": ["Fintech"], "company_size": {"min": 10, "max": 50}}
    out = merge_icp(state, {"industries": ["fintech", "SaaS"], "company_size": {"min": 200, "max": 5000}})
    # Case-insensitive dedupe, order preserved, new value appended.
    assert out["industries"] == ["Fintech", "SaaS"]
    assert out["company_size"] == {"min": 200, "max": 5000}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_intake.py -q`
Expected: FAIL — `ImportError: cannot import name 'extract_slots'`.

- [ ] **Step 3: Implement the extractor + merge (append to `nexus/orchestration/intake.py`)**

```python
# -- Deterministic extraction ----------------------------------------------------------
_COUNTRY_ALIASES = {
    "us": "United States", "u.s.": "United States", "usa": "United States",
    "u.s.a.": "United States", "united states": "United States", "america": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom", "united kingdom": "United Kingdom",
    "britain": "United Kingdom", "england": "United Kingdom",
    "canada": "Canada", "germany": "Germany", "france": "France", "spain": "Spain",
    "italy": "Italy", "netherlands": "Netherlands", "australia": "Australia",
    "india": "India", "singapore": "Singapore", "japan": "Japan", "brazil": "Brazil",
    "eu": "European Union", "europe": "Europe", "apac": "APAC", "emea": "EMEA",
}
_INDUSTRY_KEYWORDS = {
    "fintech": "Fintech", "saas": "SaaS", "healthcare": "Healthcare", "health": "Healthcare",
    "ecommerce": "E-commerce", "e-commerce": "E-commerce", "retail": "Retail",
    "manufacturing": "Manufacturing", "logistics": "Logistics", "edtech": "EdTech",
    "insurance": "Insurance", "banking": "Banking", "biotech": "Biotech",
    "cybersecurity": "Cybersecurity", "security": "Cybersecurity", "marketing": "Marketing",
    "real estate": "Real Estate", "gaming": "Gaming", "telecom": "Telecom", "energy": "Energy",
}
_NAMED_BANDS = {
    "startup": {"min": 1, "max": 50},
    "smb": {"min": 1, "max": 200},
    "small business": {"min": 1, "max": 200},
    "mid-market": {"min": 200, "max": 1000},
    "midmarket": {"min": 200, "max": 1000},
    "mid market": {"min": 200, "max": 1000},
    "enterprise": {"min": 1000, "max": None},
}
_TITLE_TOKENS = ("vp", "vice president", "cro", "cmo", "ceo", "cto", "cfo", "coo",
                 "head of", "director", "manager", "chief")
_RANGE_RE = re.compile(r"(\d[\d,]*)\s*(?:-|to|–)\s*(\d[\d,]*)")
_UNDER_RE = re.compile(r"(?:under|below|less than|fewer than|<)\s*(\d[\d,]*)")
_OVER_RE = re.compile(r"(?:over|above|more than|>|at least)\s*(\d[\d,]*)|(\d[\d,]*)\s*\+")


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def _parse_size(text: str) -> dict | None:
    t = text.lower()
    for name, band in _NAMED_BANDS.items():
        if name in t:
            return dict(band)
    m = _RANGE_RE.search(t)
    if m:
        return {"min": _int(m.group(1)), "max": _int(m.group(2))}
    m = _UNDER_RE.search(t)
    if m:
        return {"min": None, "max": _int(m.group(1))}
    m = _OVER_RE.search(t)
    if m:
        return {"min": _int(m.group(1) or m.group(2)), "max": None}
    return None


def _split_phrases(text: str) -> list[str]:
    """Split a free answer into clean phrases on commas / 'and' / slashes."""
    parts = re.split(r",|/|\band\b|\bor\b|;", text, flags=re.IGNORECASE)
    return [p.strip(" .\t").strip() for p in parts if p.strip(" .\t").strip()]


def _title_case_phrase(p: str) -> str:
    # Preserve well-known acronyms; otherwise title-case words.
    acronyms = {"vp": "VP", "cro": "CRO", "cmo": "CMO", "ceo": "CEO", "cto": "CTO",
                "cfo": "CFO", "coo": "COO", "us": "US", "uk": "UK", "saas": "SaaS"}
    words = []
    for w in p.split():
        words.append(acronyms.get(w.lower(), w if w[:1].isupper() else w.capitalize()))
    return " ".join(words)


def extract_slots(text: str, icp_state: dict, pending_slot: str | None) -> dict:
    """Pure-Python slot extraction. Returns a *delta* (only slots it learned).

    Two passes: (1) keyword/regex detection that fires anywhere in the message, so one rich
    sentence fills many slots; (2) coercion — the user is answering ``pending_slot``, so for
    open-vocabulary slots (``industries``/``titles``) the full answer phrases win over an
    incidental keyword hit, and ``geo`` is coerced only when alias detection missed it. Never
    raises: an unparseable message yields an empty delta.
    """
    delta: dict = {}
    low = text.lower()

    # (1) Global detection.
    industries = [v for k, v in _INDUSTRY_KEYWORDS.items() if re.search(rf"\b{re.escape(k)}\b", low)]
    if industries:
        delta["industries"] = list(dict.fromkeys(industries))

    geo = [v for k, v in _COUNTRY_ALIASES.items() if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", low)]
    if geo:
        delta["geo"] = list(dict.fromkeys(geo))

    size = _parse_size(text)
    if size is not None:
        delta["company_size"] = size

    titles = [_title_case_phrase(p) for p in _split_phrases(text)
              if any(tok in p.lower() for tok in _TITLE_TOKENS)]
    if titles:
        delta["titles"] = list(dict.fromkeys(titles))

    # (2) Coercion on the pending slot. For open-vocabulary slots the full answer wins
    # unconditionally (the user is explicitly answering that question); for geo we keep the
    # normalized alias hit when we have one and only coerce phrases when detection missed.
    if pending_slot in ("industries", "titles"):
        phrases = [_title_case_phrase(p) for p in _split_phrases(text)]
        if phrases:
            delta[pending_slot] = phrases
    elif pending_slot == "geo" and "geo" not in delta:
        phrases = [_title_case_phrase(p) for p in _split_phrases(text)]
        if phrases:
            delta["geo"] = phrases
    # company_size: only the regex/named-band parser fills it; leave missing to re-ask.
    return delta


def _union_ci(existing: list, incoming: list) -> list:
    """Ordered, case-insensitive union (existing first, then new)."""
    out = list(existing or [])
    seen = {str(x).lower() for x in out}
    for x in incoming or []:
        if str(x).lower() not in seen:
            out.append(x)
            seen.add(str(x).lower())
    return out


def merge_icp(icp_state: dict, delta: dict) -> dict:
    """Merge a slot-delta into the working ICP. Lists union (CI), scalars/dicts override."""
    out = dict(icp_state)
    for k, v in delta.items():
        if k in LIST_SLOTS:
            out[k] = _union_ci(out.get(k), v)
        else:
            out[k] = v
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_intake.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add nexus/orchestration/intake.py tests/test_intake.py
git commit -m "feat(intake): deterministic token-free extractor + ICP merge"
```

### Task 2.3: LLM stub branches — `clarify_question` + `chat_summary`

**Files:**
- Modify: `nexus/agents/llm.py`
- Test: `tests/test_intake.py` (append)

- [ ] **Step 1: Add the failing test (append to `tests/test_intake.py`)**

```python
import pytest

from nexus.agents.llm import LLMMessage, StubLLMProvider


@pytest.mark.asyncio
async def test_stub_clarify_question_per_slot():
    stub = StubLLMProvider()
    r = await stub.complete(
        [LLMMessage(role="user", content="x")],
        purpose="clarify_question",
        variables={"slot": "geo", "icp_state": {"industries": ["Fintech"]}},
    )
    assert "countr" in r.text.lower() or "region" in r.text.lower()


@pytest.mark.asyncio
async def test_stub_chat_summary_is_capped_and_structured():
    stub = StubLLMProvider()
    r = await stub.complete(
        [LLMMessage(role="user", content="x")],
        purpose="chat_summary",
        variables={
            "prior": "",
            "target": "companies",
            "icp_state": {"industries": ["Fintech"], "geo": ["United States"]},
        },
    )
    assert "Fintech" in r.text
    assert len(r.text) <= 600  # ~150-token cap * 4 chars
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_intake.py -q`
Expected: FAIL — the stub returns the `[stub] x` fallback, so the assertions fail.

- [ ] **Step 3: Add the two stub branches**

In `nexus/agents/llm.py`, inside `StubLLMProvider._render`, **before** the `# Fallback` comment,
add:

```python
        if purpose == "clarify_question":
            slot = v.get("slot", "")
            prompts = {
                "industries": "Which industries should I focus on? "
                "(e.g. SaaS, Fintech, Healthcare)",
                "geo": "Which countries or regions are you targeting? "
                "(e.g. United States, United Kingdom)",
                "company_size": "What company size range should I target? "
                "(e.g. 200–5000 employees)",
                "titles": "Which job titles or personas should I look for? "
                "(e.g. VP Sales, CRO)",
            }
            return prompts.get(slot, f"Could you tell me more about {slot or 'your ICP'}?")
        if purpose == "chat_summary":
            icp = v.get("icp_state", {}) or {}
            cap_chars = max(40, get_settings().orch_chat_summary_token_cap * 4)
            parts: list[str] = []
            if icp.get("industries"):
                parts.append("industries: " + ", ".join(map(str, icp["industries"])))
            if icp.get("icp_description"):
                parts.append("desc: " + str(icp["icp_description"]))
            if icp.get("geo"):
                parts.append("geo: " + ", ".join(map(str, icp["geo"])))
            size = icp.get("company_size") or {}
            if size.get("min") or size.get("max"):
                parts.append(f"size: {size.get('min', 0)}–{size.get('max', '∞')}")
            if icp.get("titles"):
                parts.append("titles: " + ", ".join(map(str, icp["titles"])))
            body = "; ".join(parts) if parts else "no ICP details captured yet"
            return (f"Target {v.get('target', 'companies')}; {body}.")[:cap_chars]
```

`get_settings` is already imported at the top of `llm.py`.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_intake.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add nexus/agents/llm.py tests/test_intake.py
git commit -m "feat(intake): deterministic stub branches for clarify + summary"
```

### Task 2.4: ContextEnvelope (token-budgeted) + IntakeController

**Files:**
- Modify: `nexus/orchestration/intake.py`
- Test: `tests/test_intake.py` (append)

- [ ] **Step 1: Add the failing test (append to `tests/test_intake.py`)**

```python
from nexus.orchestration.intake import ContextEnvelope, IntakeController


def test_envelope_trims_recency_before_summary():
    msgs = [{"role": "user", "content": "m" * 400} for _ in range(8)]
    env = ContextEnvelope.build(
        icp_state={"industries": ["Fintech"]},
        target="companies",
        account_id=None,
        missing_slots=["geo"],
        context_summary="s" * 400,
        recent_messages=msgs,
        budget=120,            # tiny budget forces trimming
        recency_window=4,
        summary_token_cap=150,
    )
    # Recency window is trimmed first (oldest dropped); never exceeds K.
    assert len(env.recent_messages) < 4
    # Summary survives if possible; here the budget is so tight it is also truncated.
    assert env.token_estimate <= 120


def test_envelope_keeps_recent_when_budget_allows():
    msgs = [{"role": "user", "content": "hi"} for _ in range(6)]
    env = ContextEnvelope.build(
        icp_state={}, target="companies", account_id=None, missing_slots=[],
        context_summary="short", recent_messages=msgs,
        budget=1200, recency_window=4, summary_token_cap=150,
    )
    assert len(env.recent_messages) == 4  # last K kept


@pytest.mark.asyncio
async def test_controller_asks_one_question_when_missing():
    ctrl = IntakeController()
    d = await ctrl.advance(
        icp_state={}, target="companies", missing_slots=[],
        context_summary="", user_text="find me some companies", is_first_turn=True,
    )
    assert d.action == "clarify"
    assert d.data["slot"] == "industries"
    assert d.assistant_kind == "clarifying_question"
    assert d.assistant_text  # phrased question
    assert d.data["suggestions"]


@pytest.mark.asyncio
async def test_controller_launches_on_complete_first_turn():
    ctrl = IntakeController()
    d = await ctrl.advance(
        icp_state={}, target="companies", missing_slots=[], context_summary="",
        user_text="Find Fintech companies in the US with 200-5000 employees",
        is_first_turn=True,
    )
    assert d.action == "launch"
    assert d.missing_slots == []
    assert d.icp_state["company_size"] == {"min": 200, "max": 5000}


@pytest.mark.asyncio
async def test_controller_confirms_then_launches_on_go():
    ctrl = IntakeController()
    complete = {"industries": ["Fintech"], "geo": ["United States"],
                "company_size": {"min": 200, "max": 5000}}
    # Not first turn, no affirmative → confirm (ready), do not launch.
    ready = await ctrl.advance(icp_state=complete, target="companies", missing_slots=[],
                               context_summary="", user_text="200 to 5000", is_first_turn=False)
    assert ready.action == "ready"
    # Explicit go → launch.
    go = await ctrl.advance(icp_state=complete, target="companies", missing_slots=[],
                            context_summary="", user_text="go", is_first_turn=False)
    assert go.action == "launch"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_intake.py -q`
Expected: FAIL — `ImportError: cannot import name 'ContextEnvelope'`.

- [ ] **Step 3: Implement the envelope + controller (append to `nexus/orchestration/intake.py`)**

```python
# -- Token-frugal context envelope -----------------------------------------------------
def _approx_tokens(text: str) -> int:
    """Cheap, monotonic token estimate (~4 chars/token). Good enough for a budget guard."""
    return max(1, (len(text) + 3) // 4)


@dataclass(slots=True)
class ContextEnvelope:
    icp_state: dict
    target: str
    account_id: str | None
    missing_slots: list[str]
    context_summary: str
    recent_messages: list[dict]
    token_estimate: int = 0

    @classmethod
    def build(
        cls,
        *,
        icp_state: dict,
        target: str | None,
        account_id: str | None,
        missing_slots: list[str],
        context_summary: str,
        recent_messages: list[dict],
        budget: int,
        recency_window: int,
        summary_token_cap: int,
    ) -> "ContextEnvelope":
        """Assemble the per-turn payload and enforce the budget.

        Structured state is authoritative and always kept (it is tiny). The summary is capped at
        ``summary_token_cap``. On overflow we **trim the recency window first** (oldest dropped),
        then truncate the summary — matching §4.3.
        """
        structured = json.dumps(
            {"icp_state": icp_state, "target": _norm_target(target),
             "account_id": account_id, "missing_slots": missing_slots},
            separators=(",", ":"),
        )
        # Pre-cap the summary to its own ceiling.
        summary = context_summary or ""
        if _approx_tokens(summary) > summary_token_cap:
            summary = summary[: summary_token_cap * 4]
        recent = list(recent_messages or [])[-recency_window:]

        base = _approx_tokens(structured)

        def total(rs: list[dict], summ: str) -> int:
            return base + _approx_tokens(summ) + sum(_approx_tokens(m.get("content", "")) for m in rs)

        # 1) Trim recency window (oldest first).
        while recent and total(recent, summary) > budget:
            recent = recent[1:]
        # 2) Truncate the summary if still over.
        if total(recent, summary) > budget:
            room = max(0, budget - base - sum(_approx_tokens(m.get("content", "")) for m in recent))
            summary = summary[: room * 4]
        return cls(
            icp_state=icp_state, target=_norm_target(target), account_id=account_id,
            missing_slots=missing_slots, context_summary=summary, recent_messages=recent,
            token_estimate=total(recent, summary),
        )


# -- Controller ------------------------------------------------------------------------
_SUGGESTIONS = {
    "industries": ["SaaS", "Fintech", "Healthcare", "E-commerce", "Manufacturing"],
    "geo": ["United States", "Canada", "United Kingdom", "Germany", "Australia"],
    "company_size": ["1–50", "51–200", "201–1000", "1001–5000", "5000+"],
    "titles": ["VP Sales", "CRO", "Head of RevOps", "CMO"],
}
_AFFIRMATIVE_PREFIXES = ("go", "yes", "yep", "yeah", "launch", "start", "run", "find",
                         "sure", "ok", "okay", "proceed", "do it")


def _is_affirmative(text: str) -> bool:
    t = text.strip().lower()
    return any(t == p or t.startswith(p + " ") for p in _AFFIRMATIVE_PREFIXES)


def infer_target(text: str, current: str | None) -> str:
    if current in (TARGET_COMPANIES, TARGET_CONTACTS):
        return current
    low = text.lower()
    if any(w in low for w in ("contact", "people", "persona", "title", "decision maker")):
        return TARGET_CONTACTS
    return TARGET_COMPANIES


def _icp_phrase(icp_state: dict, target: str) -> str:
    bits = []
    if icp_state.get("industries"):
        bits.append(", ".join(map(str, icp_state["industries"])))
    elif icp_state.get("icp_description"):
        bits.append(str(icp_state["icp_description"]))
    if icp_state.get("geo"):
        bits.append("in " + ", ".join(map(str, icp_state["geo"])))
    size = icp_state.get("company_size") or {}
    if size.get("min") or size.get("max"):
        bits.append(f"{size.get('min', 0)}–{size.get('max', '∞')} employees")
    return " ".join(bits) or "your ICP"


@dataclass(slots=True)
class IntakeDecision:
    icp_state: dict
    missing_slots: list[str]
    target: str
    action: str           # "clarify" | "ready" | "launch"
    assistant_kind: str   # KIND_* string
    assistant_text: str
    data: dict = field(default_factory=dict)
    summary: str = ""


class IntakeController:
    """The brain. Pure-Python decisioning over structured state; LLM only phrases + summarizes."""

    def __init__(self, llm: LLMProvider | None = None) -> None:
        self.llm = llm or get_llm_provider()

    async def advance(
        self,
        *,
        icp_state: dict,
        target: str | None,
        missing_slots: list[str],
        context_summary: str,
        user_text: str,
        is_first_turn: bool,
    ) -> IntakeDecision:
        target = infer_target(user_text, target)
        pending = missing_slots[0] if missing_slots else None
        new_state = merge_icp(icp_state, extract_slots(user_text, icp_state, pending))
        missing = missing_required(new_state, target)

        if missing:
            slot = missing[0]
            question = await self._phrase(slot, new_state)
            summary = await self._summarize(context_summary, new_state, target)
            return IntakeDecision(
                icp_state=new_state, missing_slots=missing, target=target,
                action="clarify", assistant_kind="clarifying_question",
                assistant_text=question,
                data={"slot": slot, "suggestions": _SUGGESTIONS.get(slot, [])},
                summary=summary,
            )

        summary = await self._summarize(context_summary, new_state, target)
        if is_first_turn or _is_affirmative(user_text):
            return IntakeDecision(
                icp_state=new_state, missing_slots=[], target=target,
                action="launch", assistant_kind="run_launched",
                assistant_text=f"Finding {target} matching {_icp_phrase(new_state, target)}…",
                data={}, summary=summary,
            )
        return IntakeDecision(
            icp_state=new_state, missing_slots=[], target=target,
            action="ready", assistant_kind="text",
            assistant_text=(
                f"I can find {target} matching {_icp_phrase(new_state, target)}. "
                "Reply 'go' to start, or refine the criteria."
            ),
            data={}, summary=summary,
        )

    async def _phrase(self, slot: str, icp_state: dict) -> str:
        resp = await self.llm.complete(
            [LLMMessage(role="user", content=f"Ask for the {slot} slot.")],
            purpose="clarify_question",
            variables={"slot": slot, "icp_state": icp_state},
            max_tokens=80,
        )
        return resp.text.strip()

    async def _summarize(self, prior: str, icp_state: dict, target: str) -> str:
        s = get_settings()
        resp = await self.llm.complete(
            [LLMMessage(role="user", content="Fold the ICP into a compact summary.")],
            purpose="chat_summary",
            variables={"prior": prior, "icp_state": icp_state, "target": target},
            max_tokens=s.orch_chat_summary_token_cap,
        )
        text = resp.text.strip()
        cap = s.orch_chat_summary_token_cap * 4
        return text[:cap]
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_intake.py -q`
Expected: PASS (14 passed).

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (baseline 62 + chat-model + intake tests).

- [ ] **Step 6: Commit**

```bash
git add nexus/orchestration/intake.py tests/test_intake.py
git commit -m "feat(intake): context envelope budget guard + IntakeController loop"
```

---

## Phase 3 — Cross-workspace tenant switch

**Spec:** §5. Two endpoints on the existing auth router that authenticate the *user* (current
valid token) but operate on `Membership`/`Tenant` directly — they are not tenant-data operations,
so they bypass `TenantSession` exactly like login. The switch endpoint **re-verifies membership
server-side** and never trusts the client.

### Task 3.1: `GET /api/auth/tenants` + `POST /api/auth/switch`

**Files:**
- Modify: `nexus/api/schemas.py`, `nexus/api/routers/auth.py`
- Test: `tests/test_tenant_switch.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tenant_switch.py
"""Cross-workspace switching: list memberships, switch re-issues a tenant-pinned JWT."""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


async def _add_membership(user_email: str, slug: str, name: str, role: str = "admin") -> str:
    """Provision a second tenant + workspace and bind the existing user into it."""
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Membership, Tenant, User, Workspace
    from sqlalchemy import select

    async with get_sessionmaker()() as s:
        tenant = Tenant(name=name, slug=slug)
        s.add(tenant)
        await s.flush()
        ws = Workspace(tenant_id=tenant.id, name=f"{name} WS")
        s.add(ws)
        await s.flush()
        user = (await s.scalars(select(User).where(User.email == user_email))).first()
        s.add(Membership(tenant_id=tenant.id, user_id=user.id, workspace_id=ws.id, role=role))
        await s.commit()
        return tenant.id


@pytest.mark.asyncio
async def test_list_tenants_returns_all_memberships(client):
    token = await signup(client, slug="acme", email="rep@acme.com", company="Acme")
    other_id = await _add_membership("rep@acme.com", "beta", "Beta Inc", role="manager")
    r = await client.get("/api/auth/tenants", headers=auth(token))
    assert r.status_code == 200, r.text
    slugs = {t["slug"]: t for t in r.json()}
    assert {"acme", "beta"} <= set(slugs)
    assert slugs["beta"]["role"] == "manager"
    assert slugs["acme"]["role"] == "owner"


@pytest.mark.asyncio
async def test_switch_reissues_token_for_member_tenant(client):
    token = await signup(client, slug="acme", email="rep@acme.com", company="Acme")
    other_id = await _add_membership("rep@acme.com", "beta", "Beta Inc", role="manager")
    r = await client.post("/api/auth/switch", json={"tenant_id": other_id}, headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == other_id
    assert body["role"] == "manager"
    # The new token must work and be pinned to the new tenant.
    r2 = await client.get("/api/auth/tenants", headers=auth(body["access_token"]))
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_switch_rejects_non_member_tenant(client):
    token = await signup(client, slug="acme", email="rep@acme.com", company="Acme")
    # A tenant the user is NOT a member of.
    from tests.conftest import make_tenant

    foreign = await make_tenant("foreign", "Foreign Co")
    r = await client.post("/api/auth/switch", json={"tenant_id": foreign}, headers=auth(token))
    assert r.status_code == 403, r.text
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tenant_switch.py -q`
Expected: FAIL — `404 Not Found` (routes don't exist yet).

- [ ] **Step 3: Add the schemas**

In `nexus/api/schemas.py`, after `TokenResponse`, add:

```python
class TenantOut(BaseModel):
    tenant_id: str
    name: str
    slug: str
    role: str


class SwitchTenantRequest(BaseModel):
    tenant_id: str
```

- [ ] **Step 4: Add the endpoints**

In `nexus/api/routers/auth.py`, extend the imports and add the two routes.

Update the import lines:

```python
from nexus.api.deps import Principal, get_db_session, get_principal
from nexus.api.schemas import (
    LoginRequest,
    SignupRequest,
    SwitchTenantRequest,
    TenantOut,
    TokenResponse,
)
```

Then append the routes at the end of the file:

```python
@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db_session),
) -> list[TenantOut]:
    """Tenants the authenticated user is a member of (for the workspace switcher)."""
    rows = (
        await db.execute(
            select(Tenant, Membership.role)
            .join(Membership, Membership.tenant_id == Tenant.id)
            .where(Membership.user_id == principal.user_id)
            .order_by(Tenant.name)
        )
    ).all()
    return [
        TenantOut(tenant_id=t.id, name=t.name, slug=t.slug, role=role) for (t, role) in rows
    ]


@router.post("/switch", response_model=TokenResponse)
async def switch_tenant(
    req: SwitchTenantRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    """Re-verify membership server-side and re-issue a JWT pinned to the requested tenant."""
    membership = (
        await db.scalars(
            select(Membership).where(
                Membership.user_id == principal.user_id,
                Membership.tenant_id == req.tenant_id,
            )
        )
    ).first()
    if membership is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No membership in the requested tenant")
    token = create_access_token(
        user_id=principal.user_id, tenant_id=membership.tenant_id, role=membership.role
    )
    return TokenResponse(
        access_token=token, tenant_id=membership.tenant_id, role=membership.role
    )
```

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_tenant_switch.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add nexus/api/schemas.py nexus/api/routers/auth.py tests/test_tenant_switch.py
git commit -m "feat(auth): list tenants + switch-workspace endpoints"
```

---

## Phase 4 — Discovery (agent + tool + `discover` recipe)

**Spec:** §6. A read-only `DiscoveryAgent` that ranks own accounts by ICP fit, then fills gaps from
the web (deduped by domain, marked `source="discovery"`). Wrapped by a `DiscoveryTool` and a single
read-only planner recipe `discover`. Offline-deterministic: own-data ranking is pure scoring; web
gap-fill is a no-op when the browser returns no hits.

### Task 4.1: `DiscoveryAgent`

**Files:**
- Create: `nexus/agents/discovery.py`
- Modify: `nexus/agents/runtime.py` (register the agent in `_ensure_agents_loaded`)
- Test: `tests/test_discovery.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_discovery.py
"""DiscoveryAgent: own-data ICP ranking + web gap-fill (deterministic, offline)."""
from __future__ import annotations

import pytest

from tests.conftest import FakeBrowser, make_tenant, tenant_session


def _runtime(browser):
    from nexus.agents.llm import StubLLMProvider
    from nexus.agents.runtime import AgentRuntime
    from nexus.relevance import get_relevance_engine

    return AgentRuntime(llm=StubLLMProvider(), relevance=get_relevance_engine(), browser=browser)


async def _seed_accounts(ts, tid):
    from nexus.models.account import Account

    a = Account(tenant_id=tid, name="FinOne", domain="finone.com", industry="Fintech",
                employee_count=800, country="United States")
    b = Account(tenant_id=tid, name="HealthCo", domain="healthco.com", industry="Healthcare",
                employee_count=300, country="United States")
    c = Account(tenant_id=tid, name="FinTwo", domain="fintwo.com", industry="Fintech",
                employee_count=50, country="United States")  # too small for the band
    for acc in (a, b, c):
        ts.add(acc)
    await ts.flush()


@pytest.mark.asyncio
async def test_discovery_ranks_own_data_by_icp_fit():
    tid = await make_tenant("t-disc")
    icp = {"industries": ["Fintech"], "geo": ["United States"],
           "company_size": {"min": 200, "max": 5000}}
    async with tenant_session(tid) as ts:
        await _seed_accounts(ts, tid)
        rt = _runtime(FakeBrowser([]))  # no web hits → own data only
        res = await rt.run("discovery", ts, account_id=None,
                           target="companies", icp=icp, max_candidates=10)
    out = res.output
    assert res.status == "completed"
    assert out["counts"]["new"] == 0
    names = [c["name"] for c in out["candidates"]]
    # FinOne (in-band Fintech/US) ranks above HealthCo (wrong industry) and FinTwo (too small).
    assert names[0] == "FinOne"
    assert all(c["source"] == "own" for c in out["candidates"])


@pytest.mark.asyncio
async def test_discovery_web_gapfill_creates_discovery_accounts():
    tid = await make_tenant("t-disc2")
    icp = {"industries": ["Fintech"], "geo": ["United States"],
           "company_size": {"min": 200, "max": 5000}}
    hits = [{"title": "NewBank", "url": "https://newbank.com/about", "domain": "newbank.com",
             "snippet": "A fintech challenger bank."}]
    async with tenant_session(tid) as ts:
        rt = _runtime(FakeBrowser(hits))  # empty own data, one web hit
        res = await rt.run("discovery", ts, account_id=None,
                           target="companies", icp=icp, max_candidates=10)
        out = res.output
        assert out["counts"]["new"] == 1
        cand = out["candidates"][0]
        assert cand["source"] == "discovery" and cand["is_new"] is True
        # The net-new account is persisted with source="discovery".
        from nexus.models.account import Account
        acc = await ts.first(Account, Account.domain == "newbank.com")
        assert acc is not None and acc.source == "discovery"


@pytest.mark.asyncio
async def test_discovery_dedupes_web_hit_against_existing_domain():
    tid = await make_tenant("t-disc3")
    icp = {"industries": ["Fintech"], "geo": ["United States"],
           "company_size": {"min": 200, "max": 5000}}
    hits = [{"title": "FinOne", "url": "https://finone.com", "domain": "finone.com",
             "snippet": "x"}]
    async with tenant_session(tid) as ts:
        from nexus.models.account import Account
        ts.add(Account(tenant_id=tid, name="FinOne", domain="finone.com", industry="Fintech",
                       employee_count=800, country="United States"))
        await ts.flush()
        rt = _runtime(FakeBrowser(hits))
        res = await rt.run("discovery", ts, account_id=None,
                           target="companies", icp=icp, max_candidates=10)
        out = res.output
        # The hit collapses onto the existing account — no net-new row.
        assert out["counts"]["new"] == 0
        domains = [c["domain"] for c in out["candidates"]]
        assert domains.count("finone.com") == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_discovery.py -q`
Expected: FAIL — `ValueError: Unknown agent 'discovery'`.

- [ ] **Step 3: Create the agent**

```python
# nexus/agents/discovery.py
"""Discovery Agent — surface companies (or contacts) matching a conversation's ICP.

Read-only by design: it ranks the tenant's own accounts by deterministic ICP fit, then fills any
gap from the web (deduped by domain, persisted as ``source="discovery"`` so the results table can
tell own data from net-new). The conversation's ICP — not the saved RelevanceProfile — drives the
scoring, so discovery reflects exactly what the rep asked for. Fully offline: scoring needs no LLM
and the browser returns no hits in tests, making counts and ordering reproducible.
"""
from __future__ import annotations

from urllib.parse import urlparse

from nexus.agents.runtime import AgentContext, BaseAgent, register_agent
from nexus.core.config import get_settings
from nexus.models.account import Account, Contact
from nexus.models.relevance import RelevanceProfile


def _icp_to_scoring(icp: dict) -> dict:
    """Map conversational ICP slots onto the RelevanceEngine's profile.icp keys."""
    size = icp.get("company_size") or {}
    return {
        "industries": icp.get("industries", []),
        "employee_min": size.get("min"),
        "employee_max": size.get("max"),
        "countries": icp.get("geo", []),
        "required_tech": icp.get("required_tech", []),
    }


def _passes_hard_filters(account: Account, scoring_icp: dict) -> bool:
    """Hard-exclude only on the explicit numeric size band the user stated.

    Size is a precise, user-asserted bound, so an account whose known headcount falls
    outside it is a definitive non-match and is dropped. Industry and geo are deliberately
    *not* hard filters: CRM industry/country labels are inconsistent ("Financial Services"
    vs "Fintech", "US" vs "United States"), so exact-string exclusion would hide good but
    mislabeled accounts. They instead drive the ICP-fit ranking — near-matches still surface,
    ranked lower, where the rep can see and refine them. Unknown headcount (None) is kept.
    """
    lo, hi = scoring_icp.get("employee_min"), scoring_icp.get("employee_max")
    if account.employee_count is not None:
        if lo is not None and account.employee_count < lo:
            return False
        if hi is not None and account.employee_count > hi:
            return False
    return True


def _domain_of(hit: dict) -> str | None:
    domain = (hit.get("domain") or "").strip().lower()
    if not domain and hit.get("url"):
        domain = (urlparse(hit["url"]).netloc or "").lower()
    return domain[4:] if domain.startswith("www.") else (domain or None)


class DiscoveryAgent(BaseAgent):
    name = "discovery"

    async def run(self, ctx: AgentContext) -> dict:
        icp = ctx.inputs.get("icp") or {}
        target = ctx.inputs.get("target") or "companies"
        max_candidates = int(
            ctx.inputs.get("max_candidates") or get_settings().discovery_max_candidates
        )
        scoring_icp = _icp_to_scoring(icp)
        # Transient (unsaved) profile: scores against the conversation's ICP, not the saved one.
        profile = RelevanceProfile(tenant_id=ctx.ts.tenant_id, icp=scoring_icp,
                                   value_props=[], product_context="")

        if target == "contacts":
            return await self._discover_contacts(ctx, profile, max_candidates)
        return await self._discover_companies(ctx, profile, scoring_icp, icp, max_candidates)

    async def _discover_companies(
        self, ctx: AgentContext, profile, scoring_icp: dict, icp: dict, max_candidates: int
    ) -> dict:
        accounts = await ctx.ts.list(Account)
        survivors = [a for a in accounts if _passes_hard_filters(a, scoring_icp)]
        scored = []
        for a in survivors:
            fit = ctx.relevance.score_icp_fit(profile, a)
            scored.append((fit, a))
        scored.sort(key=lambda t: (-t[0].score, t[1].name))
        own = scored[:max_candidates]
        candidates = [self._candidate(a, fit, source="own", is_new=False) for (fit, a) in own]
        seen_domains = {a.domain for (_, a) in own if a.domain}

        new_count = 0
        if len(candidates) < max_candidates and hasattr(ctx.browser, "search"):
            need = max_candidates - len(candidates)
            query = self._web_query(icp)
            hits = await ctx.browser.search(query, limit=need)
            for hit in hits:
                domain = _domain_of(hit)
                if not domain or domain in seen_domains:
                    continue
                existing = await ctx.ts.first(Account, Account.domain == domain)
                if existing is not None:
                    continue
                acc = Account(
                    tenant_id=ctx.ts.tenant_id,
                    name=(hit.get("title") or domain).strip(),
                    domain=domain,
                    industry=(icp.get("industries") or [None])[0],
                    country=(icp.get("geo") or [None])[0],
                    source="discovery",
                )
                ctx.ts.add(acc)
                await ctx.ts.flush()
                fit = ctx.relevance.score_icp_fit(profile, acc)
                candidates.append(self._candidate(acc, fit, source="discovery", is_new=True))
                seen_domains.add(domain)
                new_count += 1
                if len(candidates) >= max_candidates:
                    break

        return {
            "target": "companies",
            "counts": {"own": len(own), "new": new_count},
            "candidates": candidates,
        }

    async def _discover_contacts(self, ctx: AgentContext, profile, max_candidates: int) -> dict:
        """Own-data only this slice: rank contacts by their account's ICP fit."""
        contacts = await ctx.ts.list(Contact)
        scored = []
        for c in contacts:
            acc = await ctx.ts.get(Account, c.account_id)
            if acc is None:
                continue
            fit = ctx.relevance.score_icp_fit(profile, acc)
            scored.append((fit, c, acc))
        scored.sort(key=lambda t: (-t[0].score, t[1].full_name))
        top = scored[:max_candidates]
        candidates = [
            {
                "entity": "contact", "id": c.id, "name": c.full_name, "title": c.title,
                "domain": acc.domain, "fit_score": fit.score, "fit_reasons": fit.reasons,
                "source": "own", "is_new": False, "custom_fields": c.custom_fields or {},
            }
            for (fit, c, acc) in top
        ]
        return {"target": "contacts", "counts": {"own": len(top), "new": 0},
                "candidates": candidates}

    @staticmethod
    def _candidate(account: Account, fit, *, source: str, is_new: bool) -> dict:
        return {
            "entity": "account",
            "id": account.id,
            "name": account.name,
            "domain": account.domain,
            "industry": account.industry,
            "employee_count": account.employee_count,
            "country": account.country,
            "fit_score": fit.score,
            "fit_reasons": fit.reasons,
            "source": source,
            "is_new": is_new,
            "custom_fields": account.custom_fields or {},
        }

    @staticmethod
    def _web_query(icp: dict) -> str:
        parts = []
        if icp.get("industries"):
            parts.append(" ".join(map(str, icp["industries"])))
        parts.append("companies")
        if icp.get("geo"):
            parts.append("in " + " ".join(map(str, icp["geo"])))
        if icp.get("intent_signals"):
            parts.append(" ".join(map(str, icp["intent_signals"])))
        return " ".join(parts)


register_agent(DiscoveryAgent())
```

- [ ] **Step 4: Register the agent for lazy loading**

In `nexus/agents/runtime.py`, update the import inside `_ensure_agents_loaded` so discovery is
registered alongside the others:

```python
    from nexus.agents import contact_rec, discovery, messaging, qa, research, scoring  # noqa: F401
```

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_discovery.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add nexus/agents/discovery.py nexus/agents/runtime.py tests/test_discovery.py
git commit -m "feat(discovery): DiscoveryAgent — own-data ICP ranking + web gap-fill"
```

### Task 4.2: `DiscoveryTool` + `discover` planner recipe

**Files:**
- Modify: `nexus/orchestration/tools.py`, `nexus/orchestration/planner.py`
- Test: `tests/test_discovery.py` (append)

- [ ] **Step 1: Add the failing test (append to `tests/test_discovery.py`)**

```python
@pytest.mark.asyncio
async def test_discover_goal_runs_inline_and_writes_blackboard():
    from nexus.orchestration.engine import get_orchestration_engine
    from nexus.models.orchestration import RUN_COMPLETED

    tid = await make_tenant("t-disc-goal")
    icp = {"industries": ["Fintech"], "geo": ["United States"],
           "company_size": {"min": 200, "max": 5000}}
    async with tenant_session(tid) as ts:
        await _seed_accounts(ts, tid)
        engine = get_orchestration_engine()
        run = await engine.create_run(
            ts, "discover",
            goal_input={"target": "companies", "icp": icp, "max_candidates": 10},
        )
        await engine.execute_run(ts, run)
        assert run.status == RUN_COMPLETED
        disc = run.blackboard["discovery"]
        assert disc["counts"]["own"] == 2  # FinOne + HealthCo survive (FinTwo too small)
        assert any(c["source"] == "own" for c in disc["candidates"])


def test_discover_goal_is_available_and_read_only():
    from nexus.orchestration.planner import available_goals, get_planner

    assert "discover" in available_goals()
    plan = get_planner().plan("discover", {"target": "companies", "icp": {}})
    assert len(plan) == 1
    assert plan[0]["tool"] == "discovery"
    assert plan[0]["requires_approval"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_discovery.py -q`
Expected: FAIL — `PlanError: Unknown goal 'discover'`.

- [ ] **Step 3: Add the tool**

In `nexus/orchestration/tools.py`, add the `DiscoveryTool` class (after `SendMessageTool`) and
register it. It reads the run's `goal_input` (not step inputs) per §6.2:

```python
class DiscoveryTool(_AgentTool):
    name = "discovery"
    description = "Surface companies/contacts matching the conversation's ICP (read-only)."
    agent_name = "discovery"
    requires_approval = False

    async def run(self, tc: ToolContext) -> dict:
        gi = tc.run.goal_input or {}
        result = await tc.runtime.run(
            self.agent_name,
            tc.ts,
            account_id=None,
            target=gi.get("target", "companies"),
            icp=gi.get("icp") or {},
            max_candidates=gi.get("max_candidates"),
        )
        if result.status != "completed":
            raise ToolError(result.error or "discovery failed")
        if isinstance(result.output, dict) and result.output.get("error"):
            raise ToolError(str(result.output["error"]))
        tc.blackboard["discovery"] = result.output
        return result.output
```

Update the registration loop at the bottom of the file:

```python
for _t in (ResearchTool(), ScoringTool(), ComposeMessageTool(), SendMessageTool(), DiscoveryTool()):
    register_tool(_t)
```

- [ ] **Step 4: Add the recipe**

In `nexus/orchestration/planner.py`, add the recipe function (after `_research_only_plan`) and
register it:

```python
def _discover_plan(goal_input: dict) -> list[PlanStep]:
    """A single read-only discovery step. ICP/target/max_candidates ride on run.goal_input,
    which the DiscoveryTool reads directly — so the step itself needs no inputs."""
    return [PlanStep(idx=0, tool="discovery", depends_on=[], requires_approval=False)]
```

Update the `_RECIPES` dict:

```python
_RECIPES = {
    "research_account": _research_account_plan,
    "research_only": _research_only_plan,
    "discover": _discover_plan,
}
```

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_discovery.py -q`
Expected: PASS (5 passed).

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (no regressions; new discovery tests added).

- [ ] **Step 7: Commit**

```bash
git add nexus/orchestration/tools.py nexus/orchestration/planner.py tests/test_discovery.py
git commit -m "feat(discovery): DiscoveryTool + discover goal recipe"
```

---

## Phase 5 — Chat + results + custom-fields API

**Spec:** §7. The chat router (`/api/orchestration/chat/...`), the filtered discovery-results
endpoint (`/api/orchestration/runs/{id}/results`), and the proprietary-data API
(`/api/custom-fields`). All tenant-scoped via `TenantSession`; writes that launch work require
`run_orchestration` (manager+); reads allow any member.

**SSE note:** `/chat/sessions/{id}/stream` replays + follows the session's append-only
`ChatMessage` log (keyed on `seq`, mirroring the existing run SSE). When a turn launches a run, the
`run_launched` message carries `run_id`; the client streams that run's progress from the existing
`/api/orchestration/runs/{id}/events`. This keeps each stream single-responsibility and fully
testable, satisfying §7.1's "SSE of new ChatMessages … and the linked run's RunEvents".

### Task 5.1: `chat_schemas` + `ChatService`

**Files:**
- Create: `nexus/orchestration/chat_schemas.py`, `nexus/orchestration/chat_service.py`
- Test: `tests/test_chat_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_service.py
"""ChatService: ICP seeding, the clarify→launch control loop, and save-icp (offline)."""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session


@pytest.mark.asyncio
async def test_create_session_seeds_icp_from_saved_profile():
    from nexus.orchestration.chat_service import ChatService
    from nexus.relevance.engine import get_or_create_profile

    tid = await make_tenant("t-cs")
    async with tenant_session(tid) as ts:
        prof = await get_or_create_profile(ts)
        prof.icp = {"industries": ["Fintech"], "countries": ["United States"]}
        await ts.flush()
        session, _ = await ChatService().create_session(ts, created_by=None)
        assert session.icp_state["industries"] == ["Fintech"]
        assert session.icp_state["geo"] == ["United States"]


@pytest.mark.asyncio
async def test_post_message_clarifies_then_launches_discovery():
    from nexus.models.account import Account
    from nexus.models.orchestration import OrchestrationRun
    from nexus.orchestration.chat_service import ChatService

    tid = await make_tenant("t-cs2")
    async with tenant_session(tid) as ts:
        ts.add(Account(tenant_id=tid, name="FinOne", domain="finone.com", industry="Fintech",
                       employee_count=800, country="United States"))
        await ts.flush()
        svc = ChatService()
        session, _ = await svc.create_session(ts, created_by=None)

        a1 = await svc.post_message(ts, session, "find fintech companies", created_by=None)
        assert a1[0].kind == "clarifying_question"          # missing geo
        await svc.post_message(ts, session, "United States", created_by=None)  # → missing size
        a3 = await svc.post_message(ts, session, "200 to 5000", created_by=None)
        assert a3[0].kind in ("text", "run_launched")       # complete → confirm
        a4 = await svc.post_message(ts, session, "go", created_by=None)
        assert a4[-1].kind == "run_launched"
        run = await ts.get(OrchestrationRun, a4[-1].data["run_id"])
        assert run is not None and run.goal == "discover"
        assert run.chat_session_id == session.id
        assert "discovery" in (run.blackboard or {})


@pytest.mark.asyncio
async def test_save_icp_persists_chat_state_to_profile():
    from nexus.orchestration.chat_service import ChatService

    tid = await make_tenant("t-cs3")
    async with tenant_session(tid) as ts:
        svc = ChatService()
        session, _ = await svc.create_session(ts, created_by=None)
        session.icp_state = {"industries": ["SaaS"], "geo": ["Canada"],
                             "company_size": {"min": 50, "max": 500}}
        await ts.flush()
        prof = await svc.save_icp(ts, session)
        assert prof.icp["industries"] == ["SaaS"]
        assert prof.icp["countries"] == ["Canada"]
        assert prof.icp["employee_min"] == 50 and prof.icp["employee_max"] == 500


@pytest.mark.asyncio
async def test_child_session_inherits_summary_and_icp():
    from nexus.orchestration.chat_service import ChatService

    tid = await make_tenant("t-cs4")
    async with tenant_session(tid) as ts:
        svc = ChatService()
        parent, _ = await svc.create_session(ts, created_by=None)
        parent.icp_state = {"industries": ["Fintech"]}
        parent.context_summary = "Target companies; industries: Fintech."
        await ts.flush()
        child, _ = await svc.create_session(ts, created_by=None, parent_session_id=parent.id)
        assert child.icp_state["industries"] == ["Fintech"]
        assert child.context_summary == "Target companies; industries: Fintech."
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_chat_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.orchestration.chat_service'`.

- [ ] **Step 3: Create the wire schemas**

```python
# nexus/orchestration/chat_schemas.py
"""Wire contracts for the conversational orchestrator. Projections only — no raw ORM rows."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from nexus.models.chat import ChatMessage, ChatSession


class CreateSessionRequest(BaseModel):
    account_id: str | None = None
    parent_session_id: str | None = None
    message: str | None = Field(default=None, max_length=4000)


class PostMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)


class ChatMessageOut(BaseModel):
    id: str
    seq: int
    role: str
    kind: str
    content: str
    data: dict = Field(default_factory=dict)
    created_at: datetime

    @classmethod
    def from_model(cls, m: ChatMessage) -> "ChatMessageOut":
        return cls(id=m.id, seq=m.seq, role=m.role, kind=m.kind, content=m.content,
                   data=m.data or {}, created_at=m.created_at)


class ChatSessionOut(BaseModel):
    id: str
    title: str
    status: str
    target: str | None = None
    account_id: str | None = None
    icp_state: dict = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)
    context_summary: str = ""
    created_at: datetime

    @classmethod
    def from_model(cls, s: ChatSession) -> "ChatSessionOut":
        return cls(id=s.id, title=s.title, status=s.status, target=s.target,
                   account_id=s.account_id, icp_state=s.icp_state or {},
                   missing_slots=list(s.missing_slots or []),
                   context_summary=s.context_summary or "", created_at=s.created_at)


class ChatTurnResponse(BaseModel):
    """A session + the messages to render (full list on create, appended on post)."""
    session: ChatSessionOut
    messages: list[ChatMessageOut] = Field(default_factory=list)


class SaveIcpResponse(BaseModel):
    ok: bool = True
    icp: dict = Field(default_factory=dict)
```

- [ ] **Step 4: Create the service**

```python
# nexus/orchestration/chat_service.py
"""ChatService — drives the conversational orchestrator over the durable chat model.

Persists the append-only message log, runs the deterministic :class:`IntakeController` per user
turn, and — when the ICP is complete and the rep says go — launches a read-only ``discover`` run
inline (snappy feedback; discovery never parks at an approval). The control loop's *decisioning* is
pure Python (unit-tested in ``test_intake``); this layer owns persistence, sequencing, and the
run handoff. ICP seeding/saving maps between the conversational slot shape (``geo``,
``company_size``) and the saved ``RelevanceProfile.icp`` shape (``countries``, ``employee_min/max``).
"""
from __future__ import annotations

from sqlalchemy import func, select

from nexus.core.config import get_settings
from nexus.core.tenancy import TenantSession
from nexus.models.chat import (
    KIND_RUN_LAUNCHED,
    ChatMessage,
    ChatSession,
)
from nexus.models.relevance import RelevanceProfile
from nexus.orchestration.engine import OrchestrationEngine, get_orchestration_engine
from nexus.orchestration.intake import IntakeController, missing_required
from nexus.relevance.engine import get_or_create_profile, get_profile


def _seed_icp_from_profile(profile: RelevanceProfile | None) -> dict:
    """Map saved profile.icp → conversational slot shape (the inverse of save-icp)."""
    if profile is None or not profile.icp:
        return {}
    icp = profile.icp
    out: dict = {}
    if icp.get("industries"):
        out["industries"] = list(icp["industries"])
    if icp.get("countries"):
        out["geo"] = list(icp["countries"])
    if icp.get("employee_min") is not None or icp.get("employee_max") is not None:
        out["company_size"] = {"min": icp.get("employee_min"), "max": icp.get("employee_max")}
    if icp.get("required_tech"):
        out["required_tech"] = list(icp["required_tech"])
    return out


def _icp_state_to_profile(icp_state: dict) -> dict:
    """Map conversational slots → profile.icp shape for persistence."""
    size = icp_state.get("company_size") or {}
    out: dict = {
        "industries": list(icp_state.get("industries", [])),
        "countries": list(icp_state.get("geo", [])),
        "required_tech": list(icp_state.get("required_tech", [])),
    }
    if size.get("min") is not None:
        out["employee_min"] = size["min"]
    if size.get("max") is not None:
        out["employee_max"] = size["max"]
    return out


class ChatService:
    def __init__(
        self,
        controller: IntakeController | None = None,
        engine: OrchestrationEngine | None = None,
    ) -> None:
        self.controller = controller or IntakeController()
        self.engine = engine or get_orchestration_engine()

    async def create_session(
        self,
        ts: TenantSession,
        *,
        created_by: str | None,
        account_id: str | None = None,
        parent_session_id: str | None = None,
        message: str | None = None,
    ) -> tuple[ChatSession, list[ChatMessage]]:
        icp_state: dict = {}
        summary = ""
        if parent_session_id:
            parent = await ts.get(ChatSession, parent_session_id)
            if parent is not None:
                icp_state = dict(parent.icp_state or {})
                summary = parent.context_summary or ""
        else:
            icp_state = _seed_icp_from_profile(await get_profile(ts))

        session = ChatSession(
            tenant_id=ts.tenant_id,
            created_by=created_by,
            account_id=account_id,
            parent_session_id=parent_session_id,
            icp_state=icp_state,
            missing_slots=missing_required(icp_state, None),
            context_summary=summary,
        )
        ts.add(session)
        await ts.flush()

        if message:
            await self.post_message(ts, session, message, created_by=created_by)
        return session, await self.get_messages(ts, session.id)

    async def list_sessions(
        self, ts: TenantSession, *, account_id: str | None = None, status: str | None = None
    ) -> list[ChatSession]:
        where = []
        if account_id:
            where.append(ChatSession.account_id == account_id)
        if status:
            where.append(ChatSession.status == status)
        stmt = ts.select(ChatSession, *where).order_by(ChatSession.created_at.desc()).limit(100)
        return list((await ts.session.scalars(stmt)).all())

    async def get_messages(self, ts: TenantSession, session_id: str) -> list[ChatMessage]:
        stmt = ts.select(ChatMessage, ChatMessage.session_id == session_id).order_by(
            ChatMessage.seq
        )
        return list((await ts.session.scalars(stmt)).all())

    async def post_message(
        self, ts: TenantSession, session: ChatSession, content: str, *, created_by: str | None
    ) -> list[ChatMessage]:
        """Append the user message, run one control-loop turn, append assistant message(s)."""
        seq = await self._next_seq(ts, session.id)
        ts.add(ChatMessage(tenant_id=ts.tenant_id, session_id=session.id, seq=seq,
                           role="user", kind="text", content=content))
        await ts.flush()
        if not session.title or session.title == "New conversation":
            session.title = content[:160]

        decision = await self.controller.advance(
            icp_state=session.icp_state or {},
            target=session.target,
            missing_slots=session.missing_slots or [],
            context_summary=session.context_summary or "",
            user_text=content,
            is_first_turn=(seq == 1),
        )
        session.icp_state = decision.icp_state
        session.missing_slots = decision.missing_slots
        session.target = decision.target
        session.context_summary = decision.summary
        await ts.flush()

        appended: list[ChatMessage] = []
        if decision.action == "launch":
            run = await self.engine.create_run(
                ts,
                "discover",
                goal_input={
                    "target": decision.target,
                    "icp": decision.icp_state,
                    "max_candidates": get_settings().discovery_max_candidates,
                    "chat_session_id": session.id,
                },
                account_id=session.account_id,
                created_by=created_by,
            )
            run.chat_session_id = session.id
            await ts.flush()
            await self.engine.execute_run(ts, run)
            appended.append(
                await self._assistant(ts, session, KIND_RUN_LAUNCHED, decision.assistant_text,
                                      {"run_id": run.id, "goal": "discover"})
            )
        else:
            appended.append(
                await self._assistant(ts, session, decision.assistant_kind,
                                      decision.assistant_text, decision.data)
            )
        return appended

    async def save_icp(self, ts: TenantSession, session: ChatSession) -> RelevanceProfile:
        profile = await get_or_create_profile(ts)
        profile.icp = _icp_state_to_profile(session.icp_state or {})
        await ts.flush()
        return profile

    async def _assistant(
        self, ts: TenantSession, session: ChatSession, kind: str, content: str, data: dict
    ) -> ChatMessage:
        seq = await self._next_seq(ts, session.id)
        msg = ChatMessage(tenant_id=ts.tenant_id, session_id=session.id, seq=seq,
                          role="assistant", kind=kind, content=content, data=data)
        ts.add(msg)
        await ts.flush()
        return msg

    async def _next_seq(self, ts: TenantSession, session_id: str) -> int:
        cur = await ts.session.scalar(
            select(func.max(ChatMessage.seq)).where(
                ChatMessage.session_id == session_id,
                ChatMessage.tenant_id == ts.tenant_id,
            )
        )
        return int(cur or 0) + 1
```

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_chat_service.py -q`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add nexus/orchestration/chat_schemas.py nexus/orchestration/chat_service.py tests/test_chat_service.py
git commit -m "feat(chat): ChatService control loop + wire schemas"
```

### Task 5.2: Chat router + registration

**Files:**
- Create: `nexus/api/routers/chat.py`
- Modify: `nexus/api/routers/__init__.py`
- Test: `tests/test_chat_api.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_api.py
"""Chat HTTP surface: sessions, the turn loop, SSE replay, save-icp, RBAC + tenant scoping."""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


@pytest.mark.asyncio
async def test_create_list_get_and_post_turn(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    r = await client.post(
        "/api/orchestration/chat/sessions",
        json={"message": "find fintech companies"},
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    sid = body["session"]["id"]
    assert any(m["kind"] == "clarifying_question" for m in body["messages"])

    r = await client.get("/api/orchestration/chat/sessions", headers=auth(token))
    assert r.status_code == 200
    assert any(s["id"] == sid for s in r.json())

    r = await client.post(
        f"/api/orchestration/chat/sessions/{sid}/messages",
        json={"content": "United States"},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text

    r = await client.get(f"/api/orchestration/chat/sessions/{sid}", headers=auth(token))
    assert r.json()["session"]["id"] == sid
    assert len(r.json()["messages"]) >= 3


@pytest.mark.asyncio
async def test_stream_replays_session_messages(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    r = await client.post(
        "/api/orchestration/chat/sessions",
        json={"message": "find fintech companies"},
        headers=auth(token),
    )
    sid = r.json()["session"]["id"]
    r = await client.get(
        f"/api/orchestration/chat/sessions/{sid}/stream",
        headers={**auth(token), "Last-Event-ID": "0"},
    )
    assert r.status_code == 200
    assert "data:" in r.text  # SSE frames for the persisted messages


@pytest.mark.asyncio
async def test_save_icp_endpoint(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    r = await client.post(
        "/api/orchestration/chat/sessions",
        json={"message": "Find Fintech companies in the US with 200-5000 employees"},
        headers=auth(token),
    )
    sid = r.json()["session"]["id"]
    r = await client.post(
        f"/api/orchestration/chat/sessions/{sid}/save-icp", headers=auth(token)
    )
    assert r.status_code == 200, r.text
    assert "Fintech" in r.json()["icp"]["industries"]


@pytest.mark.asyncio
async def test_chat_requires_auth(client):
    r = await client.get("/api/orchestration/chat/sessions")
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_chat_api.py -q`
Expected: FAIL — `404 Not Found` (router not registered).

- [ ] **Step 3: Create the router**

```python
# nexus/api/routers/chat.py
"""Conversational orchestrator HTTP surface (`/api/orchestration/chat`).

Sessions persist per workspace and are scoped to an optional account/"client". A turn appends the
user message, runs one deterministic control-loop step, and appends the assistant reply (a
clarifying question, a confirmation, or a ``run_launched`` pointer to a discovery run). The SSE
endpoint replays + follows the session's append-only message log; the linked run streams its own
progress from ``/api/orchestration/runs/{id}/events``.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from nexus.api.deps import Principal, get_principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.chat import ChatMessage, ChatSession
from nexus.orchestration.chat_schemas import (
    ChatMessageOut,
    ChatSessionOut,
    ChatTurnResponse,
    CreateSessionRequest,
    PostMessageRequest,
    SaveIcpResponse,
)
from nexus.orchestration.chat_service import ChatService
from nexus.workers.tasks import tenant_session

router = APIRouter(prefix="/orchestration/chat", tags=["chat"])


def _turn(session: ChatSession, messages: list[ChatMessage]) -> ChatTurnResponse:
    return ChatTurnResponse(
        session=ChatSessionOut.from_model(session),
        messages=[ChatMessageOut.from_model(m) for m in messages],
    )


@router.post("/sessions", response_model=ChatTurnResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_orchestration)),
) -> ChatTurnResponse:
    session, messages = await ChatService().create_session(
        ts,
        created_by=principal.user_id,
        account_id=body.account_id,
        parent_session_id=body.parent_session_id,
        message=body.message,
    )
    return _turn(session, messages)


@router.get("/sessions", response_model=list[ChatSessionOut])
async def list_sessions(
    account_id: str | None = None,
    status_filter: str | None = None,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(get_principal),
) -> list[ChatSessionOut]:
    sessions = await ChatService().list_sessions(ts, account_id=account_id, status=status_filter)
    return [ChatSessionOut.from_model(s) for s in sessions]


@router.get("/sessions/{session_id}", response_model=ChatTurnResponse)
async def get_session(
    session_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(get_principal),
) -> ChatTurnResponse:
    session = await ts.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    messages = await ChatService().get_messages(ts, session_id)
    return _turn(session, messages)


@router.post("/sessions/{session_id}/messages", response_model=ChatTurnResponse)
async def post_message(
    session_id: str,
    body: PostMessageRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_orchestration)),
) -> ChatTurnResponse:
    session = await ts.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    appended = await ChatService().post_message(
        ts, session, body.content, created_by=principal.user_id
    )
    return _turn(session, appended)


@router.post("/sessions/{session_id}/save-icp", response_model=SaveIcpResponse)
async def save_icp(
    session_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> SaveIcpResponse:
    session = await ts.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    profile = await ChatService().save_icp(ts, session)
    return SaveIcpResponse(ok=True, icp=profile.icp or {})


def _format_sse(seq: int, type_: str, data: dict) -> str:
    return f"id: {seq}\nevent: {type_}\ndata: {json.dumps(data)}\n\n"


async def _message_stream(
    tenant_id: str, session_id: str, last_seq: int, request: Request
) -> AsyncIterator[str]:
    """Replay-then-follow the session's append-only message log as SSE (keyed on ``seq``)."""
    idle = 0
    for _ in range(120):  # ~60s ceiling
        if await request.is_disconnected():
            return
        async with tenant_session(tenant_id) as ts:
            session = await ts.get(ChatSession, session_id)
            if session is None:
                yield _format_sse(last_seq, "error", {"detail": "session not found"})
                return
            stmt = ts.select(
                ChatMessage,
                ChatMessage.session_id == session_id,
                ChatMessage.seq > last_seq,
            ).order_by(ChatMessage.seq)
            msgs = list((await ts.session.scalars(stmt)).all())
            for m in msgs:
                yield _format_sse(
                    m.seq, m.kind,
                    {"id": m.id, "role": m.role, "kind": m.kind,
                     "content": m.content, "data": m.data or {}},
                )
                last_seq = m.seq
        idle = idle + 1 if not msgs else 0
        if idle >= 4:  # ~2s quiet → close; client resumes with Last-Event-ID
            yield ": idle\n\n"
            return
        await asyncio.sleep(0.5)


@router.get("/sessions/{session_id}/stream")
async def stream_session(
    session_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: Principal = Depends(get_principal),
) -> StreamingResponse:
    try:
        last_seq = int(last_event_id) if last_event_id else 0
    except ValueError:
        last_seq = 0
    return StreamingResponse(
        _message_stream(principal.tenant_id, session_id, last_seq, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 4: Register the router**

In `nexus/api/routers/__init__.py`, add `chat` to the import block and `chat.router` to
`all_routers`:

```python
from nexus.api.routers import (
    accounts,
    agents,
    alerts,
    auth,
    chat,
    integrations,
    orchestration,
    relevance,
    signals,
    workflow,
    workspace,
)

all_routers = [
    auth.router,
    relevance.router,
    accounts.router,
    agents.router,
    workflow.router,
    alerts.router,
    integrations.router,
    workspace.router,
    signals.router,
    orchestration.router,
    chat.router,
]
```

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_chat_api.py -q`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add nexus/api/routers/chat.py nexus/api/routers/__init__.py tests/test_chat_api.py
git commit -m "feat(chat): chat router (sessions/messages/stream/save-icp) + registration"
```

---

### Task 5.3: Discovery results endpoint (server-side filter + dynamic columns)

The chat turn returns a small inline preview; this endpoint backs the full, filterable
results table. It reads the candidates the discovery run already wrote to
`run.blackboard["discovery"]` and filters them **server-side** so large lists never ship
whole to the client. It also returns the tenant's `CustomFieldDef` columns for the matching
entity so the table can render dynamic proprietary-data columns. Spec §7.2.

**Files:**
- Modify: `nexus/orchestration/schemas.py` (add `ResultColumn`, `ResultsResponse`)
- Modify: `nexus/api/routers/orchestration.py` (add `GET /runs/{id}/results`)
- Test: `tests/test_discovery_results.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_discovery_results.py
"""GET /runs/{id}/results: server-side source/min_fit/q/cf_<key> filters + dynamic columns."""
from __future__ import annotations

import pytest
from sqlalchemy.orm.attributes import flag_modified

from nexus.models.chat import CustomFieldDef
from nexus.models.orchestration import OrchestrationRun
from nexus.workers.tasks import tenant_session
from tests.conftest import auth, signup, principal_from_token


_CANDS = [
    {"entity": "account", "id": "a1", "name": "NorthBank", "domain": "northbank.com",
     "industry": "Fintech", "employee_count": 800, "country": "US",
     "fit_score": 91, "fit_reasons": ["industry match"], "source": "own",
     "is_new": False, "custom_fields": {"tier": "A"}},
    {"entity": "account", "id": "a2", "name": "WestPay", "domain": "westpay.io",
     "industry": "Fintech", "employee_count": 120, "country": "US",
     "fit_score": 64, "fit_reasons": [], "source": "own",
     "is_new": False, "custom_fields": {"tier": "B"}},
    {"entity": "account", "id": "a3", "name": "NewBank", "domain": "newbank.com",
     "industry": "Fintech", "employee_count": 300, "country": "US",
     "fit_score": 70, "fit_reasons": [], "source": "discovery",
     "is_new": True, "custom_fields": {}},
]


async def _seed_run(token, client) -> str:
    p = principal_from_token(token)
    async with tenant_session(p.tenant_id) as ts:
        run = OrchestrationRun(
            tenant_id=p.tenant_id, goal="discover companies", status="succeeded",
            blackboard={"discovery": {"target": "companies",
                                      "counts": {"own": 2, "new": 1},
                                      "candidates": _CANDS}},
        )
        ts.session.add(run)
        ts.session.add(CustomFieldDef(
            tenant_id=p.tenant_id, entity="account", key="tier",
            label="Tier", kind="text"))
        flag_modified(run, "blackboard")
        await ts.session.flush()
        return run.id


@pytest.mark.asyncio
async def test_results_unfiltered_returns_all_and_columns(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    run_id = await _seed_run(token, client)
    r = await client.get(f"/api/orchestration/runs/{run_id}/results", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert len(body["candidates"]) == 3
    assert body["target"] == "companies"
    assert any(col["key"] == "tier" for col in body["columns"])


@pytest.mark.asyncio
async def test_results_filters(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    run_id = await _seed_run(token, client)

    r = await client.get(
        f"/api/orchestration/runs/{run_id}/results?source=discovery", headers=auth(token))
    assert [c["id"] for c in r.json()["candidates"]] == ["a3"]

    r = await client.get(
        f"/api/orchestration/runs/{run_id}/results?min_fit=70", headers=auth(token))
    assert sorted(c["id"] for c in r.json()["candidates"]) == ["a1", "a3"]

    r = await client.get(
        f"/api/orchestration/runs/{run_id}/results?q=west", headers=auth(token))
    assert [c["id"] for c in r.json()["candidates"]] == ["a2"]

    r = await client.get(
        f"/api/orchestration/runs/{run_id}/results?cf_tier=A", headers=auth(token))
    assert [c["id"] for c in r.json()["candidates"]] == ["a1"]


@pytest.mark.asyncio
async def test_results_pagination(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    run_id = await _seed_run(token, client)
    r = await client.get(
        f"/api/orchestration/runs/{run_id}/results?limit=2&offset=0", headers=auth(token))
    body = r.json()
    assert body["total"] == 3
    assert len(body["candidates"]) == 2


@pytest.mark.asyncio
async def test_results_missing_run_404(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    r = await client.get("/api/orchestration/runs/does-not-exist/results", headers=auth(token))
    assert r.status_code == 404
```

This needs a `principal_from_token` test helper. Add it to `tests/conftest.py` near
`auth()`/`signup()` — it decodes the JWT exactly as `nexus/api/deps.py::get_principal`
does (claim keys are `sub`, `tid`, `role`):

```python
# tests/conftest.py — add near auth()/signup()
from nexus.core.security import decode_access_token  # add to the top-level imports
from nexus.api.deps import Principal                 # add to the top-level imports


def principal_from_token(token: str) -> Principal:
    payload = decode_access_token(token)
    return Principal(
        user_id=payload["sub"],
        tenant_id=payload["tid"],
        role=payload.get("role", "rep"),
    )
```

Add `"principal_from_token"` to the `__all__` list in `tests/conftest.py`. Note this helper
is synchronous; call it without `await` (the test above does `await principal_from_token(...)`
— change that to `principal_from_token(token)`).

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_discovery_results.py -q`
Expected: FAIL — `404` / `AttributeError` (endpoint not defined yet).

- [ ] **Step 3: Add the response schemas**

In `nexus/orchestration/schemas.py`, append after `ApprovalDecisionRequest`:

```python
class ResultColumn(BaseModel):
    key: str
    label: str
    kind: str


class ResultsResponse(BaseModel):
    """Server-side-filtered discovery results plus the dynamic custom-field columns the
    table should render. ``candidates`` stay as plain dicts — they are already a flat,
    frontend-ready projection written by the discovery agent."""

    run_id: str
    target: str | None = None
    total: int
    counts: dict = Field(default_factory=dict)
    columns: list[ResultColumn] = Field(default_factory=list)
    candidates: list[dict] = Field(default_factory=list)
```

- [ ] **Step 4: Add the endpoint**

In `nexus/api/routers/orchestration.py`, extend the imports and add the route after
`get_run`. First, widen the schema import:

```python
from nexus.orchestration.schemas import (
    ApprovalDecisionRequest,
    ApprovalOut,
    ResultColumn,
    ResultsResponse,
    RunCreateRequest,
    RunOut,
)
```

Add the `CustomFieldDef` import (new line near the other model imports):

```python
from nexus.models.chat import CustomFieldDef
```

Then add the route (place it directly after the `get_run` handler):

```python
@router.get("/runs/{run_id}/results", response_model=ResultsResponse)
async def get_run_results(
    run_id: str,
    request: Request,
    source: str | None = None,
    min_fit: int | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_orchestration)),
) -> ResultsResponse:
    run = await ts.get(OrchestrationRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    discovery = (run.blackboard or {}).get("discovery") or {}
    candidates: list[dict] = list(discovery.get("candidates") or [])
    entity = "contact" if discovery.get("target") == "contacts" else "account"

    # Arbitrary cf_<key>=<value> filters come straight off the raw query string so the
    # table can filter on any custom field without a fixed schema. Empty values are ignored.
    cf_filters = {
        k[3:]: v
        for k, v in request.query_params.items()
        if k.startswith("cf_") and v != ""
    }

    def _matches(c: dict) -> bool:
        if source and c.get("source") != source:
            return False
        if min_fit is not None and (c.get("fit_score") or 0) < min_fit:
            return False
        if q:
            needle = q.lower()
            hay = " ".join(
                str(c.get(f) or "") for f in ("name", "domain", "industry", "title")
            ).lower()
            if needle not in hay:
                return False
        for key, want in cf_filters.items():
            have = (c.get("custom_fields") or {}).get(key)
            if have is None or want.lower() not in str(have).lower():
                return False
        return True

    filtered = [c for c in candidates if _matches(c)]
    total = len(filtered)
    page = filtered[offset : offset + limit] if limit > 0 else filtered[offset:]

    stmt = ts.select(CustomFieldDef, CustomFieldDef.entity == entity).order_by(
        CustomFieldDef.label
    )
    columns = [
        ResultColumn(key=d.key, label=d.label, kind=d.kind)
        for d in (await ts.session.scalars(stmt)).all()
    ]
    return ResultsResponse(
        run_id=run.id,
        target=discovery.get("target"),
        total=total,
        counts=discovery.get("counts") or {},
        columns=columns,
        candidates=page,
    )
```

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_discovery_results.py -q`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add nexus/orchestration/schemas.py nexus/api/routers/orchestration.py \
        tests/test_discovery_results.py tests/conftest.py
git commit -m "feat(discovery): server-side filtered results endpoint with dynamic columns"
```

---

### Task 5.4: Custom-field definitions + CSV import of proprietary data

Reps bring their own columns (ARR, tier, account owner...) that NEXUS has no native schema
for. This task adds a `CustomFieldService` that manages the column catalog
(`CustomFieldDef`) and upserts values onto **existing** accounts/contacts from a CSV —
matched by a natural key (account `domain`, contact `email`). It never creates new entities;
unmatched rows are counted as skipped. Values land in the per-row `custom_fields` JSON, and
any mapped column without a `CustomFieldDef` yet gets one created automatically so the
discovery results table can render it. Spec §3.3.

**Files:**
- Create: `nexus/custom_fields/__init__.py` (empty package marker)
- Create: `nexus/custom_fields/service.py`
- Create: `nexus/api/routers/custom_fields.py`
- Modify: `nexus/api/routers/__init__.py`
- Test: `tests/test_custom_fields.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_custom_fields.py
"""Custom-field CRUD + CSV upsert onto existing accounts/contacts (domain/email match)."""
from __future__ import annotations

import json

import pytest

from nexus.models.account import Account, Contact
from nexus.workers.tasks import tenant_session
from tests.conftest import auth, signup, principal_from_token


async def _seed_accounts(token) -> None:
    p = principal_from_token(token)
    async with tenant_session(p.tenant_id) as ts:
        ts.add(Account(name="NorthBank", domain="northbank.com"))
        ts.add(Account(name="WestPay", domain="westpay.io"))
        await ts.session.flush()


@pytest.mark.asyncio
async def test_create_list_delete_field(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    r = await client.post(
        "/api/custom-fields",
        json={"entity": "account", "label": "ARR", "kind": "number"},
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    assert r.json()["key"] == "arr"

    r = await client.get("/api/custom-fields?entity=account", headers=auth(token))
    assert any(d["id"] == fid for d in r.json())

    r = await client.delete(f"/api/custom-fields/{fid}", headers=auth(token))
    assert r.status_code == 204
    r = await client.get("/api/custom-fields?entity=account", headers=auth(token))
    assert all(d["id"] != fid for d in r.json())


@pytest.mark.asyncio
async def test_csv_import_upserts_and_creates_defs(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    await _seed_accounts(token)
    csv_text = "website,annual_revenue,tier\nnorthbank.com,5000000,A\nunknown.com,1,Z\n"
    r = await client.post(
        "/api/custom-fields/import",
        data={
            "entity": "account",
            "match_column": "website",
            "mapping": json.dumps({"annual_revenue": "arr", "tier": "tier"}),
        },
        files={"file": ("data.csv", csv_text, "text/csv")},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] == 1          # northbank matched, unknown.com skipped
    assert body["updated"] == 1
    assert body["skipped"] == 1
    assert sorted(body["created_fields"]) == ["arr", "tier"]

    # The new column metadata is queryable, and the value landed on the account.
    r = await client.get("/api/custom-fields?entity=account", headers=auth(token))
    keys = {d["key"] for d in r.json()}
    assert {"arr", "tier"} <= keys

    p = principal_from_token(token)
    async with tenant_session(p.tenant_id) as ts:
        acc = await ts.first(Account, Account.domain == "northbank.com")
        assert acc.custom_fields["arr"] == "5000000"
        assert acc.custom_fields["tier"] == "A"


@pytest.mark.asyncio
async def test_csv_import_bad_match_column_400(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    r = await client.post(
        "/api/custom-fields/import",
        data={
            "entity": "account",
            "match_column": "nope",
            "mapping": json.dumps({"tier": "tier"}),
        },
        files={"file": ("data.csv", "website,tier\nx.com,A\n", "text/csv")},
        headers=auth(token),
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_custom_fields_requires_admin(client):
    # A rep (default role from signup is owner, so make a manager-less check via missing auth).
    r = await client.get("/api/custom-fields")
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_custom_fields.py -q`
Expected: FAIL — module `nexus.custom_fields` does not exist / 404.

- [ ] **Step 3: Create the package marker**

Create `nexus/custom_fields/__init__.py` (empty file).

- [ ] **Step 4: Implement the service**

Create `nexus/custom_fields/service.py`:

```python
"""Custom proprietary-data fields: a column catalog + CSV upsert onto Account/Contact.

Reps bring columns NEXUS has no native schema for. Values live in the per-row
``custom_fields`` JSON; each column's metadata lives in ``CustomFieldDef`` so tables can
render and filter it. Import matches rows by a natural key — account ``domain`` or contact
``email`` — and never creates new entities (unmatched rows are skipped)."""
from __future__ import annotations

import csv
import io
import re

from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact
from nexus.models.chat import (
    CF_KINDS,
    ENTITY_ACCOUNT,
    ENTITY_CONTACT,
    CustomFieldDef,
)

_KEY_RE = re.compile(r"[^a-z0-9_]+")


def _slug(label: str) -> str:
    return _KEY_RE.sub("_", (label or "").strip().lower()).strip("_")[:60] or "field"


class CustomFieldError(ValueError):
    """Bad input the router surfaces as 400."""


class CustomFieldService:
    async def list_defs(
        self, ts: TenantSession, entity: str | None = None
    ) -> list[CustomFieldDef]:
        where = () if entity is None else (CustomFieldDef.entity == entity,)
        return await ts.list(CustomFieldDef, *where)

    async def create_def(
        self, ts: TenantSession, *, entity: str, key: str | None, label: str, kind: str = "text"
    ) -> CustomFieldDef:
        entity = (entity or "").strip().lower()
        if entity not in (ENTITY_ACCOUNT, ENTITY_CONTACT):
            raise CustomFieldError(f"unknown entity {entity!r}")
        if kind not in CF_KINDS:
            raise CustomFieldError(f"unknown kind {kind!r}")
        slug = _slug(key or label)
        existing = await self._get_def(ts, entity, slug)
        if existing is not None:
            return existing
        d = CustomFieldDef(entity=entity, key=slug, label=label or slug, kind=kind)
        ts.add(d)
        await ts.flush()
        return d

    async def delete_def(self, ts: TenantSession, def_id: str) -> bool:
        d = await ts.get(CustomFieldDef, def_id)
        if d is None:
            return False
        await ts.delete(d)
        await ts.flush()
        return True

    async def _get_def(
        self, ts: TenantSession, entity: str, key: str
    ) -> CustomFieldDef | None:
        return await ts.first(
            CustomFieldDef, CustomFieldDef.entity == entity, CustomFieldDef.key == key
        )

    async def import_csv(
        self,
        ts: TenantSession,
        *,
        entity: str,
        content: bytes,
        match_column: str,
        mapping: dict[str, str],
    ) -> dict:
        entity = (entity or "").strip().lower()
        if entity not in (ENTITY_ACCOUNT, ENTITY_CONTACT):
            raise CustomFieldError(f"unknown entity {entity!r}")
        if not match_column:
            raise CustomFieldError("match_column is required")

        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        if match_column not in headers:
            raise CustomFieldError(f"match_column {match_column!r} not in CSV header")

        # {csv_column: field_key}, keeping only columns actually present in the file.
        cols = {c: _slug(k) for c, k in (mapping or {}).items() if c in headers}
        if not cols:
            raise CustomFieldError("no mapped columns found in CSV header")

        # Auto-create any missing CustomFieldDef (label defaults to the CSV column name).
        created_fields: list[str] = []
        for csv_col, key in cols.items():
            if await self._get_def(ts, entity, key) is None:
                ts.add(CustomFieldDef(entity=entity, key=key, label=csv_col, kind="text"))
                created_fields.append(key)
        await ts.flush()

        matched = updated = skipped = 0
        for row in reader:
            raw = (row.get(match_column) or "").strip().lower()
            if not raw:
                skipped += 1
                continue
            target = await self._match(ts, entity, raw)
            if target is None:
                skipped += 1
                continue
            matched += 1
            cf = dict(target.custom_fields or {})
            changed = False
            for csv_col, key in cols.items():
                val = (row.get(csv_col) or "").strip()
                if val == "":
                    continue
                if cf.get(key) != val:
                    cf[key] = val
                    changed = True
            if changed:
                target.custom_fields = cf
                flag_modified(target, "custom_fields")
                updated += 1
        await ts.flush()
        return {
            "matched": matched,
            "updated": updated,
            "created_fields": created_fields,
            "skipped": skipped,
        }

    async def _match(self, ts: TenantSession, entity: str, raw: str):
        if entity == ENTITY_ACCOUNT:
            return await ts.first(Account, func.lower(Account.domain) == raw)
        return await ts.first(Contact, func.lower(Contact.email) == raw)
```

- [ ] **Step 5: Implement the router**

Create `nexus/api/routers/custom_fields.py`:

```python
"""Custom-field definitions + CSV import of proprietary data.

GET/POST/DELETE ``/custom-fields`` manage the column catalog; POST ``/custom-fields/import``
upserts values onto existing accounts/contacts matched by domain/email. All tenant-scoped
and gated on ``manage_relevance`` (admin+) — proprietary data is an admin concern."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.custom_fields.service import CustomFieldError, CustomFieldService
from nexus.models.chat import CustomFieldDef

router = APIRouter(prefix="/custom-fields", tags=["custom-fields"])


class CustomFieldOut(BaseModel):
    id: str
    entity: str
    key: str
    label: str
    kind: str

    @classmethod
    def from_model(cls, d: CustomFieldDef) -> "CustomFieldOut":
        return cls(id=d.id, entity=d.entity, key=d.key, label=d.label, kind=d.kind)


class CreateFieldRequest(BaseModel):
    entity: str
    label: str
    key: str | None = None
    kind: str = "text"


class ImportResult(BaseModel):
    matched: int
    updated: int
    created_fields: list[str] = Field(default_factory=list)
    skipped: int


@router.get("", response_model=list[CustomFieldOut])
async def list_fields(
    entity: str | None = None,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> list[CustomFieldOut]:
    defs = await CustomFieldService().list_defs(ts, entity)
    return [CustomFieldOut.from_model(d) for d in defs]


@router.post("", response_model=CustomFieldOut, status_code=status.HTTP_201_CREATED)
async def create_field(
    body: CreateFieldRequest,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> CustomFieldOut:
    try:
        d = await CustomFieldService().create_def(
            ts, entity=body.entity, key=body.key, label=body.label, kind=body.kind
        )
    except CustomFieldError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return CustomFieldOut.from_model(d)


@router.delete("/{def_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_field(
    def_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> None:
    if not await CustomFieldService().delete_def(ts, def_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Field not found")


@router.post("/import", response_model=ImportResult)
async def import_csv(
    entity: str = Form(...),
    match_column: str = Form(...),
    mapping: str = Form(...),  # JSON object: {csv_column: field_key}
    file: UploadFile = File(...),
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> ImportResult:
    try:
        mapping_obj = json.loads(mapping)
        if not isinstance(mapping_obj, dict):
            raise ValueError("mapping must be a JSON object")
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid mapping: {exc}")
    content = await file.read()
    try:
        result = await CustomFieldService().import_csv(
            ts, entity=entity, content=content, match_column=match_column, mapping=mapping_obj
        )
    except CustomFieldError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return ImportResult(**result)
```

- [ ] **Step 6: Register the router**

In `nexus/api/routers/__init__.py`, add `custom_fields` to the import block and
`custom_fields.router` to `all_routers`:

```python
from nexus.api.routers import (
    accounts,
    agents,
    alerts,
    auth,
    chat,
    custom_fields,
    integrations,
    orchestration,
    relevance,
    signals,
    workflow,
    workspace,
)

all_routers = [
    auth.router,
    relevance.router,
    accounts.router,
    agents.router,
    workflow.router,
    alerts.router,
    integrations.router,
    workspace.router,
    signals.router,
    orchestration.router,
    chat.router,
    custom_fields.router,
]
```

- [ ] **Step 7: Run to verify it passes**

Run: `python -m pytest tests/test_custom_fields.py -q`
Expected: PASS (4 passed).

- [ ] **Step 8: Commit**

```bash
git add nexus/custom_fields/__init__.py nexus/custom_fields/service.py \
        nexus/api/routers/custom_fields.py nexus/api/routers/__init__.py \
        tests/test_custom_fields.py
git commit -m "feat(custom-fields): CRUD + CSV import upsert onto accounts/contacts"
```

---

## Phase 6 — Postgres migration + full-suite verification

Tests run on SQLite via `Base.metadata.create_all`, so no migration is needed to make them
pass. But production is Postgres and is driven by Alembic. The baseline `0001_initial` was a
metadata `create_all`; every change since must ship as an explicit revision so a live
database can reach `head`. This phase adds `0002` covering everything Phases 1–5 introduced,
then verifies the whole suite is green.

### Task 6.1: Alembic revision 0002 — chat tables, custom fields, run link, account.source

**Files:**
- Create: `migrations/versions/0002_conversational_orchestrator.py`

- [ ] **Step 1: Write the revision**

Create `migrations/versions/0002_conversational_orchestrator.py`. It creates the three new
tables and adds the four new columns. Column types mirror the models exactly (`IdMixin` →
`String(32)` PK; `TimestampMixin` → tz-aware `DateTime` with `server_default=now()`;
`TenantScoped` → indexed `String(32)` `tenant_id`).

```python
"""Conversational orchestrator: chat sessions/messages, custom-field defs, run link, source.

Revision ID: 0002_conversational_orchestrator
Revises: 0001_initial
Create Date: 2026-06-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_conversational_orchestrator"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _ts_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        *_ts_columns(),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=32),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("account_id", sa.String(length=32),
                  sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("parent_session_id", sa.String(length=32),
                  sa.ForeignKey("chat_sessions.id"), nullable=True),
        sa.Column("title", sa.String(length=160), nullable=False,
                  server_default="New conversation"),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="active"),
        sa.Column("target", sa.String(length=16), nullable=True),
        sa.Column("icp_state", sa.JSON(), nullable=True),
        sa.Column("missing_slots", sa.JSON(), nullable=True),
        sa.Column("context_summary", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_chat_sessions_tenant_id", "chat_sessions", ["tenant_id"])
    op.create_index("ix_chat_sessions_account_id", "chat_sessions", ["account_id"])
    op.create_index(
        "ix_chat_session_tenant_account", "chat_sessions", ["tenant_id", "account_id"])
    op.create_index(
        "ix_chat_session_tenant_status", "chat_sessions", ["tenant_id", "status"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(length=32), primary_key=True),
        *_ts_columns(),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32),
                  sa.ForeignKey("chat_sessions.id"), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=12), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False, server_default="text"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.UniqueConstraint("session_id", "seq", name="uq_chat_msg_seq"),
    )
    op.create_index("ix_chat_messages_tenant_id", "chat_messages", ["tenant_id"])
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])
    op.create_index("ix_chat_msg_session_seq", "chat_messages", ["session_id", "seq"])

    op.create_table(
        "custom_field_defs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        *_ts_columns(),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("entity", sa.String(length=12), nullable=False),
        sa.Column("key", sa.String(length=60), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False, server_default="text"),
        sa.UniqueConstraint("tenant_id", "entity", "key", name="uq_custom_field_key"),
    )
    op.create_index("ix_custom_field_defs_tenant_id", "custom_field_defs", ["tenant_id"])

    # New columns on existing tables.
    op.add_column("accounts", sa.Column("custom_fields", sa.JSON(), nullable=True))
    op.add_column("accounts", sa.Column("source", sa.String(length=40), nullable=True))
    op.add_column("contacts", sa.Column("custom_fields", sa.JSON(), nullable=True))
    op.add_column(
        "orchestration_runs",
        sa.Column("chat_session_id", sa.String(length=32),
                  sa.ForeignKey("chat_sessions.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orchestration_runs", "chat_session_id")
    op.drop_column("contacts", "custom_fields")
    op.drop_column("accounts", "source")
    op.drop_column("accounts", "custom_fields")
    op.drop_index("ix_custom_field_defs_tenant_id", table_name="custom_field_defs")
    op.drop_table("custom_field_defs")
    op.drop_index("ix_chat_msg_session_seq", table_name="chat_messages")
    op.drop_index("ix_chat_messages_session_id", table_name="chat_messages")
    op.drop_index("ix_chat_messages_tenant_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_session_tenant_status", table_name="chat_sessions")
    op.drop_index("ix_chat_session_tenant_account", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_account_id", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_tenant_id", table_name="chat_sessions")
    op.drop_table("chat_sessions")
```

> Note on column nullability: the models declare JSON columns with `default=dict`/`default=list`
> (a Python-side default), so the DB columns are nullable — the ORM fills them on insert.
> The migration matches that (`nullable=True`) rather than adding a server default, keeping it
> consistent with how `0001_initial` materialized the existing JSON columns.

- [ ] **Step 2: Verify the revision chain resolves to a single head**

Run: `python -m alembic heads`
Expected: exactly one head — `0002_conversational_orchestrator (head)`.

- [ ] **Step 3: Verify the migration applies and reverses on a scratch database**

This must run against a real Postgres URL (autogenerate/DDL doesn't fully exercise on SQLite).
If a local Postgres is available, point `NEXUS_DATABASE_URL` at a scratch DB and run:

```bash
NEXUS_DATABASE_URL=postgresql+asyncpg://localhost/nexus_scratch python -m alembic upgrade head
NEXUS_DATABASE_URL=postgresql+asyncpg://localhost/nexus_scratch python -m alembic downgrade base
NEXUS_DATABASE_URL=postgresql+asyncpg://localhost/nexus_scratch python -m alembic upgrade head
```

Expected: all three commands exit 0 (up → down → up clean). If no Postgres is available in
the environment, skip this step and rely on Step 2 plus the SQLite test suite; the explicit
DDL above is reviewable by inspection.

- [ ] **Step 4: Commit**

```bash
git add migrations/versions/0002_conversational_orchestrator.py
git commit -m "feat(db): migration 0002 — chat tables, custom fields, run link, account.source"
```

### Task 6.2: Full suite green

**Files:** none (verification only).

- [ ] **Step 1: Run the entire backend suite**

Run: `python -m pytest -q`
Expected: PASS — the 62-test baseline plus every test added in Phases 1–5
(`test_chat_models`, `test_chat_custom_fields_columns`, `test_orch_settings`,
`test_intake_*`, `test_context_envelope`, `test_tenant_switch`, `test_discovery`,
`test_chat_service`, `test_chat_api`, `test_discovery_results`, `test_custom_fields`).
Zero failures, zero errors.

- [ ] **Step 2: If anything fails, fix it before proceeding**

Triage in this order: (1) import/registration errors → a model not added to
`nexus/models/__init__.py` or a router not in `all_routers`; (2) `flag_modified` omissions →
a blackboard/`custom_fields`/`icp_state` mutation not persisting; (3) tenancy leaks → a query
that bypassed `ts.select`/`ts.first`. Re-run after each fix.

- [ ] **Step 3: Commit any fixes**

```bash
git add -A
git commit -m "test: full backend suite green for conversational orchestrator"
```

---

## Done

Phases 1–6 deliver the entire backend for the conversational orchestrator: schema (chat
sessions/messages, custom-field defs, account provenance + run link), the deterministic
intake controller and token-frugal context envelope, cross-workspace tenant switching, the
read-only discovery agent/tool/plan, the chat + results + custom-fields HTTP surface (with
SSE), and the Postgres migration. Every task is TDD with a green suite. The frontend that
consumes these endpoints is a separate plan (spec §8 / phase 6 of the spec).
