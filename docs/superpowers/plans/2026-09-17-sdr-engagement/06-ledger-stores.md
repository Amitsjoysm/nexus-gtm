# Phase 06: Training & Insights Ledger — The Three Stores Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** What phase 05 records now leaves the app: a worker ships the outbox to a sealed archive, builders turn the archive into pseudonymised training datasets and identified engagement insights, an operator manages all three stores from the Control plane, and a workspace opting out or a person asking to be forgotten is removed from every one of them with a report of what remains.

**Architecture:** Three Postgres databases (three Supabase projects, D27), each reached through `nexus/engagement/ledger/stores.py` with a role that owns one schema, and versioned by SQL files in the repo applied from the Ledger tab. `shipper.py` reads `ledger_outbox` across workspaces, resolves who each event was about, seals the pair and writes it to the **archive** — the only store it touches, so training and insights are always rebuildable. `builder.py` reads the archive from a watermark and writes both derived stores: `datasets.py` (pure) turns one event into pseudonymised rows and dataset examples, `insights.py` (pure) computes person and company profiles from the facts that remain. `deletion.py` implements both erasure paths.

**Tech Stack:** asyncpg (the three stores), Fernet + HMAC-SHA256 (`pseudonym.py`), regex scrubbing, async SQLAlchemy 2.0 (the outbox), FastAPI, React 18 + TypeScript strict.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §18.2–§18.6, §16, D25, D26, D27. **Depends on:** phase 02 (the four provider keys), phase 05 (`emit()`, consent, the outbox).

**Verified:** applied on top of phases 01–05 and run in the CI image. The offline suite for this phase passes, the whole suite passes (RUN_RESULT in Task 12), `ruff check nexus tests tests_live` is clean and `npm run typecheck` is clean. The store half is verified **against real Postgres 16** (`tests_integration/test_ledger_stores_pg.py`, 6 passed): schema application and idempotence, sealing and shipping, the label-preserving upsert on a replay, profile recomputation, workspace deletion and person erasure — and a store that genuinely refuses (a database that does not exist), which is how the backoff path is exercised without patching anything.

---

## What is true of every store operation here

1. **The archive is the source.** Training and insights are derived and rebuildable; nothing is written to them that cannot be produced again from the archive. That is what makes a scrubber fix replayable (§16).
2. **Everything is idempotent.** Every insert is `ON CONFLICT DO NOTHING` or a converging upsert, and every delete is by key. A batch that runs twice — after a crash, a retry, or a deliberate replay — converges.
3. **Nothing is deleted before it is acknowledged.** `shipped_archive_at` is set only after the archive insert commits; retention removes outbox rows seven days later.
4. **A failure is recorded on the row, not just in the log.** `attempts` and `last_error` drive a per-row backoff, so a store outage costs one failed batch per window instead of one per heartbeat tick.
5. **No connection string is ever in a response, a log line, an audit row or a job payload.**

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Create | `nexus/engagement/ledger/sql/archive/0001_initial.sql` | sealed events, person keys |
| Create | `nexus/engagement/ledger/sql/training/0001_initial.sql` | pseudonymised events, examples, build watermark |
| Create | `nexus/engagement/ledger/sql/insights/0001_initial.sql` | facts, person and company profiles |
| Create | `nexus/engagement/ledger/stores.py` | connect, store status, permission check |
| Create | `nexus/engagement/ledger/schema.py` | versioned SQL, applied versions, apply |
| Create | `nexus/engagement/ledger/pseudonym.py` | HMAC keys, splits, the archive's sealing key |
| Create | `nexus/engagement/ledger/scrub.py` | free-text scrubbing with stable placeholders |
| Create | `nexus/engagement/ledger/payloads.py` | the refs/payload contract phases 07–10 fill in |
| Create | `nexus/engagement/ledger/resolve.py` | who and what an event was about |
| Create | `nexus/engagement/ledger/shipper.py` | outbox → archive, backoff, retention, backlog |
| Create | `nexus/engagement/ledger/datasets.py` | one event → training rows (pure) |
| Create | `nexus/engagement/ledger/insights.py` | facts → person and company profiles (pure) |
| Create | `nexus/engagement/ledger/builder.py` | archive → training + insights, watermarked |
| Create | `nexus/engagement/ledger/deletion.py` | workspace deletion, person erasure, reports |
| Create | `nexus/api/routers/admin_ledger.py` | the Ledger tab's API |
| Modify | `nexus/api/routers/__init__.py` | register it |
| Modify | `nexus/api/routers/admin_health.py` | `ledger outbox` health row |
| Modify | `nexus/api/routers/engagement_settings.py` | opting out queues the deletion |
| Modify | `nexus/workers/tasks.py`, `nexus/workers/scheduler.py` | four jobs, two on the heartbeat |
| Modify | `nexus/people/store.py` | erasing a shared person reaches the ledger |
| Modify | `tests/test_continuous_automation.py`, `tests/test_crm_auto_sync.py` | two more drivers |
| Modify | `pyproject.toml` | the optional `export` extra (pyarrow) |
| Modify | `.github/workflows/ci.yml` | the live ledger secrets |
| Create | `scripts/export_training_dataset.py` | JSONL / Parquet export |
| Create | `docs/engagement/setup-supabase.md` | the owner's setup guide |
| Create | `frontend/src/pages/admin/LedgerTab.tsx` (+ `.module.css`) | the Ledger tab |
| Modify | `frontend/src/pages/AdminBillingPage.tsx`, `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts` | tab, client, types |
| Create | `tests/test_engagement_ledger_stores.py` | the offline half |
| Create | `tests_integration/test_ledger_stores_pg.py` | the store half, real Postgres |
| Create | `tests_live/engagement/test_ledger_live.py` | the three Supabase projects |

---

### Task 1: The three schemas

**Files:**
- Create: `nexus/engagement/ledger/sql/archive/0001_initial.sql`, `nexus/engagement/ledger/sql/training/0001_initial.sql`, `nexus/engagement/ledger/sql/insights/0001_initial.sql`, `nexus/engagement/ledger/stores.py`, `nexus/engagement/ledger/schema.py`
- Test: `tests/test_engagement_ledger_stores.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_engagement_ledger_stores.py` with the module docstring, the imports, `SECRET`/`NEXUS`/`SRC`/`SCRIPTS`/`NOW`, `_superadmin`, `_document` and these three tests from the final file (Task 12 Step 1): `test_every_store_has_versioned_sql_that_creates_its_schema`, `test_an_unconfigured_store_is_reported_not_crashed`, `test_a_store_dsn_pointing_at_the_metadata_service_is_refused`.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_ledger_stores.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.ledger.stores'`

- [ ] **Step 3: Implement**

`nexus/engagement/ledger/sql/archive/0001_initial.sql`:

```sql
-- nexus ledger ARCHIVE store, version 0001 (spec §18.2, §18.3).
-- Every envelope as written, sealed. The source every other store is rebuilt from.
-- Applied by Control plane -> Ledger -> Apply schema, connected as the store's own role, which owns
-- the nexus_ledger schema (docs/engagement/setup-supabase.md). Idempotent.

CREATE TABLE IF NOT EXISTS nexus_ledger.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nexus_ledger.events (
    event_id       text PRIMARY KEY,
    event_type     text NOT NULL,
    schema_version integer NOT NULL,
    occurred_at    timestamptz NOT NULL,
    tenant_id      text NOT NULL,
    -- Fernet-sealed JSON envelope. Clear columns above exist only to index, ship and delete by.
    sealed         text NOT NULL,
    -- HMAC person keys of everyone the event is about, so an erasure can find the rows.
    person_keys    text[] NOT NULL DEFAULT '{}',
    archived_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS events_archived_at ON nexus_ledger.events (archived_at, event_id);
CREATE INDEX IF NOT EXISTS events_tenant ON nexus_ledger.events (tenant_id);
CREATE INDEX IF NOT EXISTS events_person_keys ON nexus_ledger.events USING gin (person_keys);
```

`nexus/engagement/ledger/sql/training/0001_initial.sql`:

```sql
-- nexus ledger TRAINING store, version 0001 (spec §18.3, §18.4).
-- Pseudonymised events and dataset examples in standard shapes. Rebuildable from the archive.
-- No names, addresses, phone numbers, links or company names: identities are HMAC keys and free text
-- has been through the scrubber (scrubber_version on every row). Idempotent.

CREATE TABLE IF NOT EXISTS nexus_ledger.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nexus_ledger.events (
    event_id              text PRIMARY KEY,
    event_type            text NOT NULL,
    schema_version        integer NOT NULL,
    occurred_at           timestamptz NOT NULL,
    workspace_key         text NOT NULL,
    person_keys           text[] NOT NULL DEFAULT '{}',
    body                  jsonb NOT NULL,
    scrubber_version      integer NOT NULL,
    consent_terms_version text NOT NULL DEFAULT '',
    built_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nexus_ledger.examples (
    example_id            text PRIMARY KEY,
    dataset               text NOT NULL,
    created_at            timestamptz NOT NULL,
    source_event_ids      text[] NOT NULL,
    workspace_key         text NOT NULL,
    person_keys           text[] NOT NULL DEFAULT '{}',
    split                 text NOT NULL CHECK (split IN ('train', 'val', 'test')),
    schema_version        integer NOT NULL,
    scrubber_version      integer NOT NULL,
    consent_terms_version text NOT NULL DEFAULT '',
    quality               jsonb NOT NULL DEFAULT '{}',
    record                jsonb NOT NULL,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

-- The builders' watermark: the last archive row they have turned into training and insights rows.
CREATE TABLE IF NOT EXISTS nexus_ledger.build_state (
    name        text PRIMARY KEY,
    archived_at timestamptz NOT NULL,
    event_id    text NOT NULL,
    built_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS events_workspace ON nexus_ledger.events (workspace_key);
CREATE INDEX IF NOT EXISTS events_person_keys ON nexus_ledger.events USING gin (person_keys);
CREATE INDEX IF NOT EXISTS examples_dataset_split ON nexus_ledger.examples (dataset, split);
CREATE INDEX IF NOT EXISTS examples_workspace ON nexus_ledger.examples (workspace_key);
CREATE INDEX IF NOT EXISTS examples_person_keys ON nexus_ledger.examples USING gin (person_keys);
```

`nexus/engagement/ledger/sql/insights/0001_initial.sql`:

```sql
-- nexus ledger INSIGHTS store, version 0001 (spec §18.3, §18.5, D25, D26).
-- Identified engagement facts and person/company profiles the app reads back. Identity is kept (D25)
-- so SDRs can be told about real prospects; the display rules in the app (D26) decide what a viewing
-- workspace may see. Rebuildable from the archive. Idempotent.

CREATE TABLE IF NOT EXISTS nexus_ledger.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nexus_ledger.facts (
    event_id           text PRIMARY KEY,
    fact_type          text NOT NULL CHECK (fact_type IN ('send', 'reply', 'bounce', 'out_of_office')),
    person_email       text NOT NULL,
    person_key         text NOT NULL,
    company_domain     text NOT NULL DEFAULT '',
    workspace_key      text NOT NULL,
    occurred_at        timestamptz NOT NULL,
    local_hour         integer,
    local_weekday      integer,
    response_latency_s bigint,
    category           text NOT NULL DEFAULT '',
    attrs              jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS nexus_ledger.person_profiles (
    person_email       text PRIMARY KEY,
    person_key         text NOT NULL,
    company_domain     text NOT NULL DEFAULT '',
    best_weekday       integer,
    best_hour          integer,
    median_response_s  bigint,
    reply_propensity   real,
    last_reply_band    text NOT NULL DEFAULT '',
    ooo_periods        jsonb NOT NULL DEFAULT '[]',
    sends              integer NOT NULL DEFAULT 0,
    replies            integer NOT NULL DEFAULT 0,
    workspace_count    integer NOT NULL DEFAULT 0,
    workspace_keys     text[] NOT NULL DEFAULT '{}',
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nexus_ledger.company_profiles (
    company_domain     text PRIMARY KEY,
    best_weekday       integer,
    best_hour          integer,
    median_response_s  bigint,
    reply_propensity   real,
    sends              integer NOT NULL DEFAULT 0,
    replies            integer NOT NULL DEFAULT 0,
    workspace_count    integer NOT NULL DEFAULT 0,
    workspace_keys     text[] NOT NULL DEFAULT '{}',
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS facts_person ON nexus_ledger.facts (person_email, occurred_at);
CREATE INDEX IF NOT EXISTS facts_person_key ON nexus_ledger.facts (person_key);
CREATE INDEX IF NOT EXISTS facts_company ON nexus_ledger.facts (company_domain);
CREATE INDEX IF NOT EXISTS facts_workspace ON nexus_ledger.facts (workspace_key);
CREATE INDEX IF NOT EXISTS person_profiles_key ON nexus_ledger.person_profiles (person_key);
```

`nexus/engagement/ledger/stores.py`:

```python
"""Connections to the three ledger stores (spec §18.2, §18.3, D27).

Each store is its own Postgres — three Supabase projects — reached with a role that owns the
``nexus_ledger`` schema and nothing else (``docs/engagement/setup-supabase.md``). A connection is
opened per batch and closed after: shipping runs on the heartbeat, and a pool held between ticks
would hold connections Supabase counts against the project all day for a job that runs for a second.

Three details that are not style choices:

* **``statement_cache_size=0``.** Supabase's pooler can run in transaction mode, where the server
  connection a prepared statement was created on is not the one the next query lands on. asyncpg's
  cache then raises ``InvalidSQLStatementNameError`` under load and never in a quiet test.
* **The DSN goes through ``validate_dsn``**, the SSRF guard the source-database subsystem already
  owns. A connection string stored in the Control plane is operator input, and "connect to this and
  tell me what happened" is a port scanner otherwise.
* **The password never reaches a log or an audit row** — ``redact_dsn`` is what any reporting uses.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

STORES = ("archive", "training", "insights")


class StoreNotConfigured(RuntimeError):
    """No connection string is stored for this ledger store."""


class StoreUnavailable(RuntimeError):
    """The store is configured but refused or could not be reached."""


def _allow_private() -> bool:
    from nexus.core.config import get_settings

    settings = get_settings()
    return settings.env in ("local", "test") or bool(settings.source_db_allow_private)


@asynccontextmanager
async def connect(store: str):
    """One asyncpg connection to ``store``. Raises ``StoreNotConfigured`` when there is no DSN."""
    import asyncpg

    from nexus.engagement import config
    from nexus.sources.safety import validate_dsn

    if store not in STORES:
        raise ValueError(f"unknown ledger store {store!r}")
    dsn = await config.ledger_dsn(store)
    if not dsn:
        raise StoreNotConfigured(store)
    validate_dsn(dsn, allow_private=_allow_private())
    conn = await asyncpg.connect(
        dsn.replace("postgresql+asyncpg://", "postgresql://", 1),
        timeout=20, command_timeout=60, statement_cache_size=0,
    )
    try:
        yield conn
    finally:
        await conn.close()


async def status(store: str) -> dict:
    """What an operator needs to see on the Ledger tab: configured, reachable, owns its schema,
    which versions are applied and which are pending. Never raises, and never leaks the DSN."""
    from nexus.engagement.ledger import schema

    report = {
        "store": store, "configured": False, "reachable": False, "owns_schema": False,
        "applied": [], "pending": [version for version, _sql in schema.migrations(store)],
        "detail": "",
    }
    try:
        async with connect(store) as conn:
            report["configured"] = True
            report["reachable"] = True
            owner = await conn.fetchval(
                "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'nexus_ledger'")
            role = await conn.fetchval("SELECT current_user")
            report["owns_schema"] = bool(owner) and owner == role
            done = await schema.applied_versions(conn) if owner else set()
            report["applied"] = sorted(done)
            report["pending"] = [v for v, _sql in schema.migrations(store) if v not in done]
            if owner and owner != role:
                report["detail"] = (
                    f"schema nexus_ledger is owned by {owner}, not {role}; see "
                    "docs/engagement/setup-supabase.md")
            elif not owner:
                report["detail"] = "schema nexus_ledger does not exist yet — apply the schema"
    except StoreNotConfigured:
        report["detail"] = "not configured"
    except Exception as exc:
        # Configured, since we had a DSN to try: the useful distinction is reachable or not.
        report["configured"] = True
        report["detail"] = f"{type(exc).__name__}: could not connect or read the schema"
    return report
```

`nexus/engagement/ledger/schema.py`:

```python
"""Each store's schema, as versioned SQL files applied from the Control plane (spec §18.2).

The files live in ``nexus/engagement/ledger/sql/<store>/`` and are applied in name order inside one
transaction each, recorded in ``nexus_ledger.schema_migrations``. Never by hand: a store at a version
the code does not know is how a shipper silently writes into a table that no longer means what it
did. Every file is idempotent (``CREATE ... IF NOT EXISTS``), so a half-applied version can be
re-applied rather than repaired.

This deliberately does NOT use Alembic. Alembic owns the app's own database and its chain is
replayed onto an empty database by a test; these are three foreign databases with one table group
each, applied by an operator pressing a button, and giving them a second Alembic environment with
its own head would be a chain nobody replays.
"""
from __future__ import annotations

from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent / "sql"


def migrations(store: str) -> list[tuple[str, str]]:
    """``[(version, sql)]`` for a store, in name order. Empty for an unknown store."""
    folder = SQL_DIR / store
    if not folder.is_dir():
        return []
    return [(path.stem, path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.sql"))]


async def applied_versions(conn) -> set[str]:
    """What the store says it has. An absent table means nothing is applied yet, not an error."""
    if await conn.fetchval("SELECT to_regclass('nexus_ledger.schema_migrations')") is None:
        return set()
    rows = await conn.fetch("SELECT version FROM nexus_ledger.schema_migrations")
    return {row["version"] for row in rows}


async def apply_schema(store: str) -> list[str]:
    """Apply every pending version. Returns the versions applied by THIS call, so pressing the
    button twice reports an empty list rather than claiming work it did not do."""
    from nexus.engagement.ledger.stores import connect

    applied: list[str] = []
    async with connect(store) as conn:
        done = await applied_versions(conn)
        for version, sql in migrations(store):
            if version in done:
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO nexus_ledger.schema_migrations (version) VALUES ($1) "
                    "ON CONFLICT (version) DO NOTHING",
                    version,
                )
            applied.append(version)
    return applied
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_ledger_stores.py -n0 -q`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/ledger tests/test_engagement_ledger_stores.py
git commit -m "feat(ledger): three stores, each a role owning one versioned schema"
```

---

### Task 2: Keys, splits and the archive's sealing key

**Files:**
- Create: `nexus/engagement/ledger/pseudonym.py`
- Test: `tests/test_engagement_ledger_stores.py`

- [ ] **Step 1: Write the failing tests**

Add `test_a_person_is_the_same_key_everywhere_and_a_different_one_per_kind`, `test_a_workspace_lands_in_one_split_and_the_mix_is_roughly_80_10_10` and `test_the_archive_key_is_derived_from_the_same_secret_and_round_trips`.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_ledger_stores.py -n0 -q -k pseudonym or key or split`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.ledger.pseudonym'`

- [ ] **Step 3: Implement**

`nexus/engagement/ledger/pseudonym.py`:

```python
"""Pseudonymous keys, dataset splits and the archive's sealing key (spec §18.3, §18.4, D25).

One secret, ``ledger_pseudonym`` in Provider keys, derives everything here:

* **Keys** — ``HMAC-SHA256(secret, "<kind>:<value>")``. Deterministic, so the same person is the same
  key in every event and every store (joins and erasure work), and not reversible without the
  secret, which never leaves the app.
* **The archive's sealing key** — derived from the same secret with a different label, so an archive
  dump alone reveals nothing.
* **Splits** — ``train``/``val``/``test`` assigned by hashing the WORKSPACE key, so no workspace's
  data appears in two splits and an evaluation never scores a model on a customer it trained on.

Losing the secret breaks linkage and makes the archive unreadable (spec §16); it is backed up with the
database and rotation re-keys from the archive.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet

KINDS = ("person", "company", "workspace", "user", "account", "contact", "mailbox")


def _normal(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def key(secret: str, kind: str, value: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown key kind {kind!r}")
    if not secret:
        raise ValueError("the pseudonymisation secret is not configured")
    digest = hmac.new(secret.encode(), f"{kind}:{_normal(value)}".encode(), hashlib.sha256)
    return digest.hexdigest()[:32]


def person_key(secret: str, email: str) -> str:
    return key(secret, "person", email)


def workspace_key(secret: str, tenant_id: str) -> str:
    return key(secret, "workspace", tenant_id)


def company_key(secret: str, domain: str) -> str:
    return key(secret, "company", domain)


def split_for(workspace: str) -> str:
    """80 / 10 / 10 by workspace. Deterministic, and independent of the secret's rotation because it
    hashes the key the rows already carry."""
    bucket = int(hashlib.sha256(workspace.encode()).hexdigest()[:8], 16) % 100
    if bucket < 80:
        return "train"
    return "val" if bucket < 90 else "test"


def archive_fernet(secret: str) -> Fernet:
    if not secret:
        raise ValueError("the pseudonymisation secret is not configured")
    raw = hashlib.sha256(f"nexus-ledger-archive:{secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_ledger_stores.py -n0 -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/ledger/pseudonym.py tests/test_engagement_ledger_stores.py
git commit -m "feat(ledger): one secret behind every key, split and the archive's seal"
```

---

### Task 3: The scrubber

**Files:**
- Create: `nexus/engagement/ledger/scrub.py`
- Test: `tests/test_engagement_ledger_stores.py`

- [ ] **Step 1: Write the failing tests**

Add `test_known_people_companies_addresses_and_numbers_become_stable_placeholders`, `test_an_iso_date_survives_but_a_stranger_s_address_does_not` and `test_the_scrubber_walks_nested_values_and_leaves_non_text_alone`.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_ledger_stores.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.ledger.scrub'`

- [ ] **Step 3: Implement**

`nexus/engagement/ledger/scrub.py`:

```python
"""Remove personal data from free text before it reaches the training store (spec §18.3, §16).

Three passes, in this order:

1. **Addresses and links.** Every email address and link — the ones the event's records know first,
   then anything that looks like one — becomes a placeholder. These go first because a company domain
   replaced earlier would split "priya@acme.io" and "www.acme.io/careers" into halves no pattern can
   recognise.
2. **Known people and companies.** Every name and company the event is about, taken from the records
   it references (contact, account, mailbox, the SDR), becomes a consistent placeholder for the whole
   example: "Jane" in the email and "Jane Buyer" in the signature are the same ``[PERSON_1]``, which
   keeps the text learnable.
3. **Phone numbers.** Eight to fifteen digits, whatever the punctuation.

Names the records do not know and no pattern can see (a colleague named in passing) are the known
limit; ``SCRUBBER_VERSION`` is stored on every row so an improved scrubber can re-scrub from the
archive.

Matching rules: full names and company names are matched case-insensitively; a single name part
("Mark", "Will") only with the capitalisation it was given, so ordinary words survive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SCRUBBER_VERSION = 1

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<![\w/])\+?\(?\d[\d\s().-]{6,}\d(?![\w/])")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_BOUNDARY = r"\b"


@dataclass(slots=True)
class Known:
    people: list[str] = field(default_factory=list)       # full names
    companies: list[str] = field(default_factory=list)    # names and domains
    emails: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _word(literal: str, flags: int) -> re.Pattern:
    return re.compile(_BOUNDARY + re.escape(literal) + _BOUNDARY, flags)


class Scrubber:
    """One per example, so placeholders are consistent across its texts."""

    def __init__(self, known: Known):
        self._placeholders: dict[tuple[str, str], str] = {}
        self._counts: dict[str, int] = {}

        literals: list[tuple[re.Pattern, str, str]] = []
        for email in known.emails:
            if email:
                literals.append((re.compile(re.escape(email), re.IGNORECASE), "EMAIL",
                                 email.lower()))
        for url in known.urls:
            if url:
                literals.append((re.compile(re.escape(url), re.IGNORECASE), "URL", url.lower()))
        # Known addresses and links first, so they number before strangers found by pattern.
        self._literals = sorted(literals, key=lambda r: -len(r[0].pattern))

        names: list[tuple[re.Pattern, str, str]] = []
        people: set[tuple[str, bool, str]] = set()
        for full in known.people:
            name = " ".join((full or "").split())
            if not name:
                continue
            identity = name.lower()
            people.add((name, True, identity))
            for part in name.split(" "):
                # A first or last name on its own shares the full name's placeholder, and matches
                # only with the capitalisation it was given, so "will" and "mark" survive as words.
                if len(part) >= 3 and part[0].isupper():
                    people.add((part, False, identity))
        for name, insensitive, identity in people:
            names.append((_word(name, re.IGNORECASE if insensitive else 0), "PERSON", identity))
        for company in {c for c in known.companies if c}:
            names.append((_word(company, re.IGNORECASE), "COMPANY", company.lower()))
        # Longest literal first, so "Jane Buyer" wins over "Jane" and a domain over its label.
        self._names = sorted(names, key=lambda r: -len(r[0].pattern))

    def _placeholder(self, kind: str, identity: str) -> str:
        slot = (kind, identity)
        if slot not in self._placeholders:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._placeholders[slot] = f"[{kind}_{self._counts[kind]}]"
        return self._placeholders[slot]

    def text(self, value: str | None) -> str:
        text = value or ""
        for pattern, kind, identity in self._literals:
            text = pattern.sub(lambda _m, k=kind, i=identity: self._placeholder(k, i), text)
        text = EMAIL_RE.sub(lambda m: self._placeholder("EMAIL", m.group(0).lower()), text)
        text = URL_RE.sub(lambda m: self._placeholder("URL", m.group(0).lower()), text)
        for pattern, kind, identity in self._names:
            text = pattern.sub(lambda _m, k=kind, i=identity: self._placeholder(k, i), text)

        def _phone(match: re.Match) -> str:
            digits = _digits(match.group(0))
            # An ISO date has eight digits too, and a resolved "call me on 2026-10-05" is a label.
            if _ISO_DATE.fullmatch(match.group(0)):
                return match.group(0)
            if len(digits) < 8 or len(digits) > 15:
                return match.group(0)
            return self._placeholder("PHONE", digits)

        return PHONE_RE.sub(_phone, text)

    def value(self, obj):
        """Scrub every string inside a JSON-like value."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {k: self.value(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.value(v) for v in obj]
        return obj
```

Three details that were each found by running it:

- **Order matters.** Known addresses and links go first: a company domain replaced earlier splits `priya@acme.io` into halves no pattern can recognise.
- **A single name part is matched case-SENSITIVELY.** "Mark", "Will" and "Bill" are ordinary words; replacing them case-insensitively mangles the text a model would learn from.
- **An ISO date is not a phone number.** Eight digits is also `2026-10-05`, and a resolved date is a label the `cls_reply` dataset is built on.

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_ledger_stores.py -n0 -q`
Expected: `9 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/ledger/scrub.py tests/test_engagement_ledger_stores.py
git commit -m "feat(ledger): scrub free text to stable placeholders, versioned so it can be replayed"
```

---

### Task 4: What an event was about

**Files:**
- Create: `nexus/engagement/ledger/payloads.py`, `nexus/engagement/ledger/resolve.py`
- Test: covered by Task 5's shipping tests and the integration suite

- [ ] **Step 1: Implement the contract**

`nexus/engagement/ledger/payloads.py` — the shapes phases 07–10 fill in, in one place so a builder written now can read an event first emitted in phase 09:

```python
"""What each engagement event's ``refs`` and ``payload`` carry — the contract between emitters and
the dataset builders (spec §18.1, §18.4, §18.5).

Emitters in phases 07-10 build these dicts; ``datasets.build`` reads them. A field an emitter omits
reads as absent, never as an error: an example missing its context is skipped with a quality flag
rather than breaking the build for every other event. Keeping the shapes in one module is what lets a
builder written in phase 06 read an event first emitted in phase 09.
"""
from __future__ import annotations

from typing import TypedDict


class EngagementRefs(TypedDict, total=False):
    account_id: str
    contact_id: str
    campaign_id: str
    enrollment_id: str
    thread_id: str
    message_id: str
    mailbox_id: str
    #: On reply events: the outbound message the reply answers (from In-Reply-To / thread matching).
    answered_message_id: str
    classification_id: str


class DraftCreated(TypedDict, total=False):        # draft.created
    step_index: int
    kind: str                                       # step | reengage | response
    context_pack: str                               # the full prompt context the model saw
    subject: str
    body: str
    quality_problems: list[str]
    prompt_version: str


class DraftEdited(TypedDict, total=False):         # draft.edited
    context_pack: str
    ai_subject: str
    ai_body: str
    subject: str
    body: str
    edit_distance: int


class MessageSent(TypedDict, total=False):         # message.sent
    kind: str
    step_index: int
    subject: str
    body: str
    context_pack: str
    sent_local_hour: int
    sent_local_weekday: int
    contact_timezone: str
    mailbox_provider: str
    days_since_last_touch: int
    persona: dict                                   # {"title", "seniority"}
    account: dict                                   # {"industry", "employee_count", "country"}
    signal_types: list[str]
    personalisation_facts: list[str]


class ReplyReceived(TypedDict, total=False):       # reply.received
    inbound_kind: str                               # human | auto_reply | bounce | other_auto
    local_hour: int
    local_weekday: int
    response_latency_s: int
    body: str
    thread: list[dict]                              # [{"direction", "at", "body"}], oldest first


class ReplyClassified(TypedDict, total=False):     # reply.classified
    category: str
    confidence: float
    date_phrase: str
    resolved_date: str                              # ISO date
    label_source: str                               # ai | deterministic
    body: str
    thread: list[dict]
    prompt_version: str


class ReplyCorrected(TypedDict, total=False):      # reply.corrected
    ai_category: str
    sdr_category: str
    resolved_date: str


class ResponseSent(TypedDict, total=False):        # response.sent
    subject: str
    body: str
    ai_body: str
    conversation: list[dict]


POSITIVE_CATEGORIES = frozenset({"interested", "question", "referral"})
```

- [ ] **Step 2: Implement the resolver**

`nexus/engagement/ledger/resolve.py`:

```python
"""Who and what a ledger event is about, read from the records its refs name (spec §18.3, §18.5).

Resolved once, at shipping, and sealed into the archive beside the envelope. Three things need it:
the person keys an erasure searches by, the known names the scrubber replaces, and the person and
company an insights fact is about.

Runs on the platform session, because the shipper reads across workspaces — but every record is
checked against the event's OWN tenant before a field is taken from it, so a ref that names another
workspace's row contributes nothing. That check is the whole safety story of this module: this is
the one place in the ledger where tenant-scoped records are read without a tenant binding.

Never raises. A record that cannot be read resolves to empty fields, and an event with no resolved
contact simply produces no insights fact and no person key — never a failed batch.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Resolved:
    tenant_id: str = ""
    consent_terms_version: str = ""
    contact_email: str = ""
    contact_name: str = ""
    contact_title: str = ""
    contact_seniority: str = ""
    account_name: str = ""
    account_domain: str = ""
    account_industry: str = ""
    account_employee_count: int | None = None
    account_country: str = ""
    sdr_name: str = ""
    sdr_email: str = ""
    other_names: list[str] = field(default_factory=list)

    def person_emails(self) -> list[str]:
        """Whose erasure must find this event. The SDR is our customer's employee, not a
        prospect — their address is still scrubbed out of training text, but an erasure request
        is about the person we contacted."""
        return [e for e in (self.contact_email,) if e]

    def known(self):
        """The names, companies and addresses the scrubber replaces with stable placeholders."""
        from nexus.engagement.ledger.scrub import Known

        return Known(
            people=[n for n in (self.contact_name, self.sdr_name, *self.other_names) if n],
            companies=[c for c in (self.account_name, self.account_domain) if c],
            emails=[e for e in (self.contact_email, self.sdr_email) if e],
        )

    def as_dict(self) -> dict:
        from dataclasses import asdict

        data = asdict(self)
        data["person_emails"] = self.person_emails()
        return data


async def resolve(session, envelope: dict) -> Resolved:
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection
    from nexus.models.identity import User

    tenant_id = str(envelope.get("tenant_id") or "")
    refs = envelope.get("refs") or {}
    out = Resolved(tenant_id=tenant_id)
    if not tenant_id:
        return out

    def owned(row) -> bool:
        return row is not None and getattr(row, "tenant_id", None) == tenant_id

    try:
        contact = await session.get(Contact, refs["contact_id"]) if refs.get("contact_id") else None
        if owned(contact):
            out.contact_email = (contact.email or "").strip().lower()
            out.contact_name = contact.full_name or ""
            out.contact_title = contact.title or ""
            out.contact_seniority = contact.seniority or ""
        account_id = refs.get("account_id") or (
            contact.account_id if owned(contact) else None)
        account = await session.get(Account, account_id) if account_id else None
        if owned(account):
            out.account_name = account.name or ""
            out.account_domain = (account.domain or "").strip().lower()
            out.account_industry = account.industry or ""
            out.account_employee_count = account.employee_count
            out.account_country = account.country or ""
        mailbox = (await session.get(MailboxConnection, refs["mailbox_id"])
                   if refs.get("mailbox_id") else None)
        user_id = (envelope.get("actor") or {}).get("user_id")
        if owned(mailbox):
            out.sdr_email = mailbox.email or ""
            user_id = user_id or mailbox.owner_user_id
        user = await session.get(User, user_id) if user_id else None
        if user is not None:
            out.sdr_name = user.full_name or ""
            out.sdr_email = out.sdr_email or (user.email or "")
        out.consent_terms_version = await _terms_version(session, tenant_id)
    except Exception:
        return out
    return out


async def _terms_version(session, tenant_id: str) -> str:
    """The terms the workspace agreed to when this was collected, stamped on every training row."""
    from sqlalchemy import select

    from nexus.models.ledger import TrainingConsent

    row = (await session.execute(
        select(TrainingConsent.terms_version)
        .where(TrainingConsent.tenant_id == tenant_id)
        .order_by(TrainingConsent.decided_at.desc())
        .limit(1)
    )).scalars().first()
    return row or ""
```

> The tenant check inside `resolve` is the safety story of this module: it is the one place in the ledger where tenant-scoped records are read without a tenant binding, because the shipper is cross-workspace by definition.

- [ ] **Step 3: Check it imports and the guard holds**

Run: `pytest tests/test_rls_binding_guard.py -n0 -q`
Expected: pass — `resolve()` takes a platform session and builds no `TenantSession`, so the guard has nothing to complain about.

- [ ] **Step 4: Commit**

```bash
git add nexus/engagement/ledger/payloads.py nexus/engagement/ledger/resolve.py
git commit -m "feat(ledger): the event payload contract and the resolver that names who an event is about"
```

---

### Task 5: Shipping to the archive

**Files:**
- Create: `nexus/engagement/ledger/shipper.py`
- Test: `tests/test_engagement_ledger_stores.py`

- [ ] **Step 1: Write the failing tests**

Add `test_a_failed_row_waits_longer_each_time_up_to_an_hour`, `test_an_archive_row_is_sealed_carries_its_person_keys_and_opens_again`, `test_shipping_without_a_secret_records_nothing_and_says_so`, `test_the_backlog_reports_what_is_waiting_and_how_old_it_is` and `test_retention_only_removes_rows_the_archive_has_acknowledged`.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_ledger_stores.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.ledger.shipper'`

- [ ] **Step 3: Implement**

`nexus/engagement/ledger/shipper.py`:

```python
"""Outbox → archive (spec §18.2).

The archive is written FIRST and is the only store the shipper touches; training and insights are
built from it, so both are rebuildable and neither can drift from what was actually recorded.

What the shipper adds to each envelope before sealing it is the ``resolved`` block — who and what the
event was about, read from the app database at shipping time. Resolving here rather than at build
time is what makes the archive self-contained: a contact deleted next week does not take the meaning
of last week's events with it.

Failure posture, and it is deliberately not the job-retry machinery alone:

* Each row carries ``attempts`` and ``last_error``, and a row that failed waits ``2^attempts``
  minutes (capped at an hour) before it is tried again. So a store outage costs one failed batch per
  backoff window rather than one per heartbeat tick, and the jobs that ride in between find nothing
  due and return quietly instead of dead-lettering every minute.
* The handler still RAISES when a batch fails, so `workers/durability.py` retries and finally
  dead-letters — the evidence an operator needs, once per window instead of sixty times.
* Nothing is ever deleted before it is acknowledged: ``shipped_archive_at`` is set only after the
  insert commits, and retention removes rows seven days after that.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger("nexus.engagement.ledger.shipper")

#: Rows per batch. A ledger event is at most 256 KB (`envelope.MAX_PAYLOAD_BYTES`), so a full batch
#: is bounded at ~125 MB in the worst case and a few MB in practice.
BATCH = 250
#: Batches per run. The heartbeat comes round again; an unbounded loop would starve every other job.
MAX_BATCHES = 8
#: How long a shipped row stays in the outbox. Long enough to re-ship after a store is restored from
#: a backup, short enough that the table stays small (spec §18.2).
RETENTION_DAYS = 7
_MAX_BACKOFF_MINUTES = 60


def retry_delay_minutes(attempts: int) -> int:
    """``0, 2, 4, 8, ... 60``. Pure, so the schedule is testable without a store."""
    if attempts <= 0:
        return 0
    return min(2 ** attempts, _MAX_BACKOFF_MINUTES)


def is_due(attempts: int, updated_at: datetime, now: datetime) -> bool:
    return updated_at + timedelta(minutes=retry_delay_minutes(attempts)) <= now


def archive_document(envelope: dict, resolved: dict) -> dict:
    """What is sealed: the envelope as written, plus who it was about at the time."""
    return {"envelope": envelope, "resolved": resolved}


def archive_row(secret: str, envelope: dict, resolved: dict) -> tuple:
    """One row for ``nexus_ledger.events``. Pure given the secret."""
    from nexus.engagement.ledger.pseudonym import archive_fernet, person_key

    document = archive_document(envelope, resolved)
    sealed = archive_fernet(secret).encrypt(
        json.dumps(document, default=str).encode("utf-8")).decode("ascii")
    emails = [e for e in (resolved.get("person_emails") or []) if e]
    keys = sorted({person_key(secret, email) for email in emails})
    return (
        envelope["event_id"],
        envelope["event_type"],
        int(envelope.get("schema_version") or 1),
        as_datetime(envelope["occurred_at"]),
        str(envelope.get("tenant_id") or ""),
        sealed,
        keys,
    )


def open_document(secret: str, sealed: str) -> dict:
    """The inverse, for the builders and the export script."""
    from nexus.engagement.ledger.pseudonym import archive_fernet

    return json.loads(archive_fernet(secret).decrypt(sealed.encode("ascii")).decode("utf-8"))


def as_datetime(value) -> datetime:
    """An envelope's ISO timestamp as a real datetime. asyncpg binds by inferred type before any
    cast in the SQL, so a string here is a DataError, not a coercion."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


async def _claim(session, now: datetime, limit: int) -> list:
    """The oldest unshipped rows whose backoff has elapsed, across every workspace.

    Reads through the platform sessionmaker: under the RLS-bound app role a cross-tenant read
    returns ZERO ROWS rather than raising, which would read as "nothing to ship" forever.
    """
    from sqlalchemy import select

    from nexus.models.ledger import LedgerOutbox

    rows = (await session.execute(
        select(LedgerOutbox)
        .where(LedgerOutbox.shipped_archive_at.is_(None))
        .order_by(LedgerOutbox.occurred_at, LedgerOutbox.created_at)
        .limit(limit * 3)
    )).scalars().all()
    return [row for row in rows if is_due(row.attempts, row.updated_at, now)][:limit]


async def ship_batch(limit: int = BATCH) -> dict:
    """Ship one batch. Returns a summary; raises ``StoreUnavailable`` when the archive refused."""
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.engagement import config
    from nexus.engagement.ledger import resolve as resolver
    from nexus.engagement.ledger.stores import StoreUnavailable, connect

    secret = await config.pseudonym_secret()
    if not secret:
        return {"shipped": 0, "skipped": "the pseudonymisation secret is not configured"}

    now = utcnow()
    sessionmaker = get_platform_sessionmaker()
    async with sessionmaker() as session:
        rows = await _claim(session, now, limit)
        if not rows:
            return {"shipped": 0}
        prepared = []
        for row in rows:
            envelope = row.payload or {}
            resolved = await resolver.resolve(session, envelope)
            prepared.append((row, archive_row(secret, envelope, resolved.as_dict())))
        try:
            async with connect("archive") as conn:
                await conn.executemany(
                    "INSERT INTO nexus_ledger.events "
                    "(event_id, event_type, schema_version, occurred_at, tenant_id, sealed, "
                    " person_keys) VALUES ($1, $2, $3, $4, $5, $6, $7) "
                    "ON CONFLICT (event_id) DO NOTHING",
                    [values for _row, values in prepared],
                )
        except Exception as exc:
            for row, _values in prepared:
                row.attempts += 1
                row.last_error = f"{type(exc).__name__}: {exc}"[:500]
            await session.commit()
            logger.warning("ledger archive batch of %s failed", len(prepared), exc_info=True)
            raise StoreUnavailable(f"archive: {type(exc).__name__}") from exc
        for row, _values in prepared:
            row.shipped_archive_at = now
            row.last_error = None
        await session.commit()
    return {"shipped": len(prepared)}


async def ship(max_batches: int = MAX_BATCHES) -> dict:
    total = 0
    for _ in range(max_batches):
        result = await ship_batch()
        if result.get("skipped"):
            return result
        shipped = result["shipped"]
        total += shipped
        if shipped < BATCH:
            break
    return {"shipped": total}


async def purge_shipped() -> int:
    """Delete rows archived more than ``RETENTION_DAYS`` ago. Bounded, indexed, idempotent."""
    from sqlalchemy import delete

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.ledger import LedgerOutbox

    cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
    async with get_platform_sessionmaker()() as session:
        result = await session.execute(
            delete(LedgerOutbox)
            .where(LedgerOutbox.shipped_archive_at.is_not(None))
            .where(LedgerOutbox.shipped_archive_at < cutoff)
        )
        await session.commit()
    return int(result.rowcount or 0)


async def backlog() -> dict:
    """What the Ledger tab and the health row report: how much is waiting, and how old it is."""
    from sqlalchemy import func, select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.ledger import LedgerOutbox

    async with get_platform_sessionmaker()() as session:
        row = (await session.execute(
            select(func.count(LedgerOutbox.id), func.min(LedgerOutbox.occurred_at),
                   func.max(LedgerOutbox.attempts))
            .where(LedgerOutbox.shipped_archive_at.is_(None))
        )).one()
        waiting, oldest, attempts = int(row[0] or 0), row[1], int(row[2] or 0)
        errored = (await session.execute(
            select(func.count(LedgerOutbox.id))
            .where(LedgerOutbox.shipped_archive_at.is_(None))
            .where(LedgerOutbox.attempts > 0)
        )).scalar_one()
    age_s = int((utcnow() - oldest).total_seconds()) if oldest is not None else 0
    return {"waiting": waiting, "oldest_age_s": age_s, "retrying": int(errored or 0),
            "max_attempts": attempts}
```

> `as_datetime` exists because asyncpg binds by **inferred type before any cast in the SQL**: an ISO string passed for a `timestamptz` column is a `DataError`, not a coercion. That was found by the integration suite, not by reading.

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_ledger_stores.py -n0 -q`
Expected: `14 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/ledger/shipper.py tests/test_engagement_ledger_stores.py
git commit -m "feat(ledger): ship the outbox to a sealed archive, with per-row backoff and retention"
```

---

### Task 6: The dataset builders and the profile maths

**Files:**
- Create: `nexus/engagement/ledger/datasets.py`, `nexus/engagement/ledger/insights.py`
- Test: `tests/test_engagement_ledger_stores.py`

- [ ] **Step 1: Write the failing tests**

Add the ten dataset and profile tests: `test_a_sent_message_makes_a_training_example_a_feature_row_and_a_send_fact`, `test_the_pseudonymised_event_keeps_no_tenant_no_ids_and_no_names`, `test_a_reply_labels_the_message_it_answers_and_records_a_reply_fact`, `test_a_classification_becomes_a_labelled_example_and_marks_the_send_positive`, `test_an_sdr_correction_replaces_the_label_and_says_who_set_it`, `test_an_sdr_edit_becomes_a_preference_pair_and_an_unedited_draft_does_not`, `test_a_meeting_labels_every_example_the_outcome_is_attributed_to`, `test_an_ai_call_is_kept_for_distillation_with_its_model_and_cost`, `test_an_event_about_nobody_still_becomes_a_training_event_but_no_fact`, `test_a_profile_is_the_shape_of_the_replies_that_remain` and `test_the_speed_band_is_the_only_pattern_below_the_threshold`.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_ledger_stores.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.ledger.datasets'`

- [ ] **Step 3: Implement**

`nexus/engagement/ledger/datasets.py`:

```python
"""One archived event → pseudonymised training rows and identified insights facts (spec §18.4, §18.5).

**Pure.** ``build(document, secret)`` takes what the archive holds and returns plain dataclasses;
`builder.py` is the only part that touches a store. That split is what makes the datasets testable
without three Postgres servers, and what lets a scrubber fix be replayed over the archive.

The shapes are standard so any model family can train on them without reshaping:

| dataset | anchor event | record |
|---|---|---|
| ``sft_outreach_email`` | ``message.sent`` | chat messages: context pack → the email as sent |
| ``pref_outreach_email`` | ``draft.edited`` | ``prompt`` / ``chosen`` (the SDR's) / ``rejected`` (the AI's) |
| ``cls_reply`` | ``reply.classified`` | reply + thread → category, resolved date, label source |
| ``sft_reply_response`` | ``response.sent`` | conversation → the SDR's final response |
| ``tab_engagement`` | ``message.sent`` | features → replied, positive, response latency |
| ``ai_calls`` | ``ai.call`` | prompt version, model, inputs, outputs, tokens, latency |

**Labels arrive later than the example they belong to**, which is the whole difficulty: a reply is a
different event, hours or days after the send. So a builder emits two kinds of thing — an ``Example``
(upsert, keyed by a hash of the dataset and its anchor) and a ``Patch`` (a merge into one object of
an existing example, keyed the same way). A patch for an example that does not exist is dropped: the
archive is ordered by arrival, and a reply cannot be archived before the message it answers.

An example is never overwritten by a rebuild: the upsert merges the stored labels OVER the fresh
ones, so replaying the archive cannot un-learn a label a later event set.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from nexus.engagement.ledger.payloads import POSITIVE_CATEGORIES
from nexus.engagement.ledger.pseudonym import company_key, person_key, split_for, workspace_key
from nexus.engagement.ledger.scrub import SCRUBBER_VERSION, Scrubber

SCHEMA_VERSION = 1

SYSTEM_OUTREACH = ("You are an SDR writing a first-touch email. Use only the facts in the context; "
                   "never invent a customer, a metric or a case study.")
SYSTEM_RESPONSE = "You are an SDR replying to a prospect's answer, in the SDR's own voice."

#: Which refs name a person, a company or a workspace actor, and so become HMAC keys in training.
_REF_KINDS = {"contact_id": "contact", "account_id": "account", "mailbox_id": "mailbox",
              "user_id": "user", "person_id": "person", "company_id": "company"}


@dataclass(slots=True)
class Example:
    dataset: str
    example_id: str
    anchor: str
    workspace_key: str
    person_keys: list[str]
    created_at: str
    record: dict
    consent_terms_version: str = ""
    quality: dict = field(default_factory=dict)
    source_event_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Patch:
    """A merge into ``record -> <path>`` of the example anchored on ``anchor``."""
    dataset: str
    example_id: str
    path: str
    values: dict
    source_event_id: str


@dataclass(slots=True)
class Fact:
    event_id: str
    fact_type: str
    person_email: str
    person_key: str
    company_domain: str
    workspace_key: str
    occurred_at: str
    local_hour: int | None = None
    local_weekday: int | None = None
    response_latency_s: int | None = None
    category: str = ""
    attrs: dict = field(default_factory=dict)


@dataclass(slots=True)
class FactCategory:
    """A classification arriving after the reply fact it describes."""
    message_id: str
    category: str


@dataclass(slots=True)
class Built:
    event: dict | None = None
    examples: list[Example] = field(default_factory=list)
    patches: list[Patch] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    fact_categories: list[FactCategory] = field(default_factory=list)


def example_id(dataset: str, anchor: str) -> str:
    """Stable, and not a raw application id: the training store holds no identifiers of ours."""
    return hashlib.sha256(f"{dataset}:{anchor}".encode()).hexdigest()[:32]


def pseudonymise(envelope: dict, secret: str, scrubber: Scrubber) -> dict:
    """The envelope with identities replaced by keys and free text scrubbed."""
    refs = {}
    for name, value in (envelope.get("refs") or {}).items():
        kind = _REF_KINDS.get(name)
        refs[name] = pseudonym_key(secret, kind, str(value)) if kind and value else value
    actor = dict(envelope.get("actor") or {})
    if actor.get("user_id"):
        actor["user_id"] = pseudonym_key(secret, "user", actor["user_id"])
    return {
        "event_id": envelope.get("event_id", ""),
        "event_type": envelope.get("event_type", ""),
        "schema_version": envelope.get("schema_version", SCHEMA_VERSION),
        "occurred_at": envelope.get("occurred_at", ""),
        "actor": actor,
        "refs": refs,
        "chain": envelope.get("chain") or {},
        "context": envelope.get("context") or {},
        "payload": scrubber.value(envelope.get("payload") or {}),
    }


def pseudonym_key(secret: str, kind: str, value: str) -> str:
    from nexus.engagement.ledger.pseudonym import key

    return key(secret, kind, value)


def _thread(scrubber: Scrubber, entries) -> list[dict]:
    out = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        out.append({"direction": entry.get("direction", ""), "at": entry.get("at", ""),
                    "body": scrubber.text(entry.get("body", ""))})
    return out


def build(document: dict, secret: str) -> Built:
    """One archived document (``{"envelope", "resolved"}``) → everything it contributes."""
    envelope = document.get("envelope") or {}
    resolved = document.get("resolved") or {}
    tenant_id = str(envelope.get("tenant_id") or "")
    if not tenant_id:
        return Built()

    scrubber = _scrubber(resolved)
    wkey = workspace_key(secret, tenant_id)
    emails = [e for e in (resolved.get("person_emails") or []) if e]
    pkeys = sorted({person_key(secret, email) for email in emails})
    built = Built(event={
        "event_id": envelope.get("event_id", ""),
        "event_type": envelope.get("event_type", ""),
        "schema_version": int(envelope.get("schema_version") or SCHEMA_VERSION),
        "occurred_at": envelope.get("occurred_at", ""),
        "workspace_key": wkey,
        "person_keys": pkeys,
        "body": pseudonymise(envelope, secret, scrubber),
        "scrubber_version": SCRUBBER_VERSION,
        "consent_terms_version": resolved.get("consent_terms_version") or "",
    })

    event_type = envelope.get("event_type", "")
    payload = envelope.get("payload") or {}
    refs = envelope.get("refs") or {}
    event_id = envelope.get("event_id", "")
    occurred_at = envelope.get("occurred_at", "")
    common = {"workspace_key": wkey, "person_keys": pkeys, "created_at": occurred_at,
              "source_event_ids": [event_id],
              "consent_terms_version": resolved.get("consent_terms_version") or ""}

    if event_type == "message.sent":
        _message_sent(built, scrubber, payload, refs, resolved, common, event_id, occurred_at,
                      secret, wkey, emails, pkeys)
    elif event_type == "draft.edited":
        anchor = refs.get("message_id") or event_id
        chosen = _subject_body(scrubber, payload.get("subject"), payload.get("body"))
        rejected = _subject_body(scrubber, payload.get("ai_subject"), payload.get("ai_body"))
        if chosen and rejected and chosen != rejected:
            built.examples.append(Example(
                dataset="pref_outreach_email", example_id=example_id("pref_outreach_email", anchor),
                anchor=anchor, record={"prompt": scrubber.text(payload.get("context_pack", "")),
                                       "chosen": chosen, "rejected": rejected},
                quality={"missing_context": not payload.get("context_pack"),
                         "edit_distance": payload.get("edit_distance")},
                **common))
    elif event_type == "reply.received":
        _reply_received(built, scrubber, payload, refs, resolved, event_id, occurred_at, wkey,
                        emails, secret)
    elif event_type == "reply.classified":
        _reply_classified(built, scrubber, payload, refs, common, event_id)
    elif event_type == "reply.corrected":
        _reply_corrected(built, payload, refs, event_id)
    elif event_type == "message.bounced":
        _simple_fact(built, "bounce", refs, resolved, event_id, occurred_at, wkey, emails, secret)
    elif event_type == "response.sent":
        anchor = refs.get("message_id") or event_id
        built.examples.append(Example(
            dataset="sft_reply_response", example_id=example_id("sft_reply_response", anchor),
            anchor=anchor,
            record={"messages": [{"role": "system", "content": SYSTEM_RESPONSE},
                                 *_conversation(scrubber, payload.get("conversation")),
                                 {"role": "assistant",
                                  "content": scrubber.text(payload.get("body", ""))}],
                    "labels": {"meeting": False}},
            quality={"ai_draft_kept": payload.get("body") == payload.get("ai_body")},
            **common))
    elif event_type == "outcome.recorded":
        _outcome(built, payload, refs, event_id)
    elif event_type == "ai.call":
        built.examples.append(Example(
            dataset="ai_calls", example_id=example_id("ai_calls", event_id), anchor=event_id,
            record={"agent": payload.get("agent", ""),
                    "model": (envelope.get("context") or {}).get("model", ""),
                    "prompt_version": (envelope.get("context") or {}).get("prompt_version", ""),
                    "status": payload.get("status", ""),
                    "inputs": scrubber.value(payload.get("inputs") or {}),
                    "output": scrubber.value(payload.get("output") or ""),
                    "tokens": payload.get("tokens"), "latency_ms": payload.get("latency_ms")},
            quality={"failed": payload.get("status") not in ("ok", "success", None)},
            **common))
    return built


def _scrubber(resolved: dict) -> Scrubber:
    from nexus.engagement.ledger.scrub import Known

    return Scrubber(Known(
        people=[n for n in (resolved.get("contact_name"), resolved.get("sdr_name"),
                            *(resolved.get("other_names") or [])) if n],
        companies=[c for c in (resolved.get("account_name"), resolved.get("account_domain")) if c],
        emails=[e for e in (resolved.get("contact_email"), resolved.get("sdr_email")) if e],
    ))


def _subject_body(scrubber: Scrubber, subject, body) -> str:
    subject, body = (subject or "").strip(), (body or "").strip()
    if not body:
        return ""
    head = f"Subject: {scrubber.text(subject)}\n\n" if subject else ""
    return head + scrubber.text(body)


def _conversation(scrubber: Scrubber, entries) -> list[dict]:
    out = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        role = "user" if entry.get("direction") == "in" else "assistant"
        out.append({"role": role, "content": scrubber.text(entry.get("body", ""))})
    return out


def _message_sent(built, scrubber, payload, refs, resolved, common, event_id, occurred_at, secret,
                  wkey, emails, pkeys) -> None:
    anchor = refs.get("message_id") or event_id
    body = _subject_body(scrubber, payload.get("subject"), payload.get("body"))
    if body:
        built.examples.append(Example(
            dataset="sft_outreach_email", example_id=example_id("sft_outreach_email", anchor),
            anchor=anchor,
            record={"messages": [{"role": "system", "content": SYSTEM_OUTREACH},
                                 {"role": "user",
                                  "content": scrubber.text(payload.get("context_pack", ""))},
                                 {"role": "assistant", "content": body}],
                    "labels": {"replied": False, "positive": False, "meeting": False},
                    "meta": {"kind": payload.get("kind", ""),
                             "step_index": payload.get("step_index")}},
            quality={"missing_context": not payload.get("context_pack")}, **common))
    built.examples.append(Example(
        dataset="tab_engagement", example_id=example_id("tab_engagement", anchor), anchor=anchor,
        record={"features": {
            "local_hour": payload.get("sent_local_hour"),
            "local_weekday": payload.get("sent_local_weekday"),
            "step_index": payload.get("step_index"),
            "kind": payload.get("kind", ""),
            "mailbox_provider": payload.get("mailbox_provider", ""),
            "days_since_last_touch": payload.get("days_since_last_touch"),
            "title": (payload.get("persona") or {}).get("title", ""),
            "seniority": (payload.get("persona") or {}).get("seniority", ""),
            "industry": (payload.get("account") or {}).get("industry", ""),
            "employee_count": (payload.get("account") or {}).get("employee_count"),
            "country": (payload.get("account") or {}).get("country", ""),
            "signal_types": payload.get("signal_types") or [],
            # The facts themselves are free text about a named person; how MANY were used is the
            # feature, and it carries no identity.
            "personalisation_facts": len(payload.get("personalisation_facts") or []),
        }, "labels": {"replied": False, "positive": False, "response_latency_s": None}},
        **common))
    if emails:
        built.facts.append(Fact(
            event_id=event_id, fact_type="send", person_email=emails[0],
            person_key=person_key(secret, emails[0]),
            company_domain=resolved.get("account_domain") or "", workspace_key=wkey,
            occurred_at=occurred_at, local_hour=payload.get("sent_local_hour"),
            local_weekday=payload.get("sent_local_weekday"),
            attrs={"message_id": anchor, "step_index": payload.get("step_index")}))


def _reply_received(built, scrubber, payload, refs, resolved, event_id, occurred_at, wkey, emails,
                    secret) -> None:
    answered = refs.get("answered_message_id")
    kind = payload.get("inbound_kind", "human")
    latency = payload.get("response_latency_s")
    if kind == "human" and answered:
        for dataset in ("sft_outreach_email", "tab_engagement"):
            built.patches.append(Patch(
                dataset=dataset, example_id=example_id(dataset, answered), path="labels",
                values={"replied": True, "response_latency_s": latency},
                source_event_id=event_id))
    if not emails:
        return
    fact_type = {"human": "reply", "bounce": "bounce", "auto_reply": "out_of_office"}.get(kind, "")
    if not fact_type:
        return
    attrs = {"message_id": refs.get("message_id") or event_id}
    if fact_type == "out_of_office" and payload.get("ooo_until"):
        attrs["until"] = payload["ooo_until"]
    built.facts.append(Fact(
        event_id=event_id, fact_type=fact_type, person_email=emails[0],
        person_key=person_key(secret, emails[0]),
        company_domain=resolved.get("account_domain") or "", workspace_key=wkey,
        occurred_at=occurred_at, local_hour=payload.get("local_hour"),
        local_weekday=payload.get("local_weekday"),
        response_latency_s=latency if fact_type == "reply" else None, attrs=attrs))


def _reply_classified(built, scrubber, payload, refs, common, event_id) -> None:
    anchor = refs.get("message_id") or event_id
    built.examples.append(Example(
        dataset="cls_reply", example_id=example_id("cls_reply", anchor), anchor=anchor,
        record={"input": {"reply": scrubber.text(payload.get("body", "")),
                          "thread": _thread(scrubber, payload.get("thread"))},
                "output": {"category": payload.get("category", ""),
                           "resolved_date": payload.get("resolved_date", ""),
                           "label_source": payload.get("label_source", "ai")}},
        quality={"confidence": payload.get("confidence")}, **common))
    answered = refs.get("answered_message_id")
    if answered:
        positive = payload.get("category") in POSITIVE_CATEGORIES
        for dataset in ("sft_outreach_email", "tab_engagement"):
            built.patches.append(Patch(
                dataset=dataset, example_id=example_id(dataset, answered), path="labels",
                values={"positive": positive}, source_event_id=event_id))
    built.fact_categories.append(FactCategory(message_id=anchor,
                                              category=payload.get("category", "")))


def _reply_corrected(built, payload, refs, event_id) -> None:
    anchor = refs.get("message_id") or event_id
    sdr = payload.get("sdr_category", "")
    source = "sdr_confirmed" if sdr and sdr == payload.get("ai_category") else "sdr_corrected"
    built.patches.append(Patch(
        dataset="cls_reply", example_id=example_id("cls_reply", anchor), path="output",
        values={"category": sdr, "resolved_date": payload.get("resolved_date", ""),
                "label_source": source},
        source_event_id=event_id))
    answered = refs.get("answered_message_id")
    if answered:
        for dataset in ("sft_outreach_email", "tab_engagement"):
            built.patches.append(Patch(
                dataset=dataset, example_id=example_id(dataset, answered), path="labels",
                values={"positive": sdr in POSITIVE_CATEGORIES}, source_event_id=event_id))
    built.fact_categories.append(FactCategory(message_id=anchor, category=sdr))


def _outcome(built, payload, refs, event_id) -> None:
    """A meeting is the label every outreach dataset is really after."""
    if payload.get("stage") not in ("meeting", "meeting_booked", "qualified", "won"):
        return
    anchor = (payload.get("meta") or {}).get("message_id") or refs.get("message_id")
    if not anchor:
        return
    for dataset in ("sft_outreach_email", "tab_engagement", "sft_reply_response"):
        built.patches.append(Patch(
            dataset=dataset, example_id=example_id(dataset, anchor), path="labels",
            values={"meeting": True}, source_event_id=event_id))


def _simple_fact(built, fact_type, refs, resolved, event_id, occurred_at, wkey, emails,
                 secret) -> None:
    if not emails:
        return
    built.facts.append(Fact(
        event_id=event_id, fact_type=fact_type, person_email=emails[0],
        person_key=person_key(secret, emails[0]),
        company_domain=resolved.get("account_domain") or "", workspace_key=wkey,
        occurred_at=occurred_at, attrs={"message_id": refs.get("message_id") or event_id}))


def split_of(workspace: str) -> str:
    return split_for(workspace)


def company_of(secret: str, domain: str) -> str:
    return company_key(secret, domain)
```

`nexus/engagement/ledger/insights.py`:

```python
"""Person and company engagement profiles, computed from identified facts (spec §18.5).

Pure: a list of fact dicts in, one profile dict out. The builder recomputes a profile from ALL of a
person's remaining facts whenever one of their facts changes, rather than incrementing counters, so
a workspace deletion or an erasure that removes facts leaves a profile that is exactly what the
remaining evidence says — never a count that still includes data somebody asked us to delete.
"""
from __future__ import annotations

from collections import Counter
from statistics import median

HOUR_S = 3600
DAY_S = 24 * HOUR_S

#: Response-speed bands of the last reply, the only pattern shown below the workspace threshold (D26).
BANDS = (("within_hour", HOUR_S), ("same_day", DAY_S), ("within_week", 7 * DAY_S))


def speed_band(latency_s: int | None) -> str:
    if latency_s is None or latency_s < 0:
        return ""
    for band, ceiling in BANDS:
        if latency_s <= ceiling:
            return band
    return "longer"


def _mode(values: list[int]) -> int | None:
    """Most frequent value; the smallest wins a tie, so the answer does not depend on fact order."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    counts = Counter(present)
    best = max(counts.values())
    return min(v for v, n in counts.items() if n == best)


def _aggregate(facts: list[dict]) -> dict:
    sends = [f for f in facts if f["fact_type"] == "send"]
    replies = sorted((f for f in facts if f["fact_type"] == "reply"), key=lambda f: f["occurred_at"])
    latencies = [f["response_latency_s"] for f in replies if f.get("response_latency_s") is not None]
    workspaces = sorted({f["workspace_key"] for f in facts if f.get("workspace_key")})
    return {
        "best_weekday": _mode([f.get("local_weekday") for f in replies]),
        "best_hour": _mode([f.get("local_hour") for f in replies]),
        "median_response_s": int(median(latencies)) if latencies else None,
        "reply_propensity": round(len(replies) / len(sends), 4) if sends else None,
        "sends": len(sends),
        "replies": len(replies),
        "workspace_count": len(workspaces),
        "workspace_keys": workspaces,
        "_last_reply": replies[-1] if replies else None,
    }


def person_profile(person_email: str, facts: list[dict]) -> dict | None:
    """``None`` when no facts remain: the profile is deleted rather than left describing nobody."""
    if not facts:
        return None
    agg = _aggregate(facts)
    last = agg.pop("_last_reply")
    ooo = sorted(
        ({"from": f["occurred_at"].date().isoformat() if hasattr(f["occurred_at"], "date")
          else str(f["occurred_at"])[:10],
          "until": (f.get("attrs") or {}).get("until", "")}
         for f in facts if f["fact_type"] == "out_of_office"),
        key=lambda p: p["from"],
    )[-5:]
    newest = max(facts, key=lambda f: f["occurred_at"])
    return {
        "person_email": person_email,
        "person_key": newest["person_key"],
        "company_domain": newest.get("company_domain") or "",
        **agg,
        "last_reply_band": speed_band(last.get("response_latency_s")) if last else "",
        "ooo_periods": ooo,
    }


def company_profile(company_domain: str, facts: list[dict]) -> dict | None:
    if not facts or not company_domain:
        return None
    agg = _aggregate(facts)
    agg.pop("_last_reply")
    return {"company_domain": company_domain, **agg}
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_ledger_stores.py -n0 -q`
Expected: `25 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/ledger/datasets.py nexus/engagement/ledger/insights.py tests/test_engagement_ledger_stores.py
git commit -m "feat(ledger): pure builders for the six datasets and the person/company profiles"
```

---

### Task 7: Building both derived stores

**Files:**
- Create: `nexus/engagement/ledger/builder.py`
- Test: `tests_integration/test_ledger_stores_pg.py` (Task 11)

- [ ] **Step 1: Implement**

`nexus/engagement/ledger/builder.py`:

```python
"""Archive → training and insights, incrementally (spec §18.2, §18.4, §18.5).

Reads the archive in the order rows were written, from the watermark in
``nexus_ledger.build_state`` (kept in the training store, because that is where the examples are),
and writes both derived stores from the same batch. Everything it writes is an upsert keyed on an id
derived from the event, so re-running a batch — after a crash, or deliberately to replay a scrubber
fix — converges instead of duplicating.

The watermark advances only after both stores have taken the batch. Re-running therefore costs a
repeated upsert, never a gap; a gap is the one failure that would be invisible, because a missing
example looks exactly like a workspace that did not send that day.

Profiles are RECOMPUTED from the facts that remain for each person touched, never incremented. An
erasure or a workspace deletion removes facts, and a counter would keep describing data we no longer
hold.
"""
from __future__ import annotations

import json
import logging

from nexus.engagement.ledger import datasets, insights
from nexus.engagement.ledger.shipper import as_datetime
from nexus.engagement.ledger.stores import connect

logger = logging.getLogger("nexus.engagement.ledger.builder")

WATERMARK = "datasets"
BATCH = 500
MAX_BATCHES = 10


async def build_once(limit: int = BATCH) -> dict:
    """One batch. Returns what it wrote, or what stopped it."""
    from nexus.engagement import config

    secret = await config.pseudonym_secret()
    if not secret:
        return {"built": 0, "skipped": "the pseudonymisation secret is not configured"}

    async with connect("training") as training:
        mark = await training.fetchrow(
            "SELECT archived_at, event_id FROM nexus_ledger.build_state WHERE name = $1", WATERMARK)
    since_at = mark["archived_at"] if mark else None
    since_id = mark["event_id"] if mark else ""

    async with connect("archive") as archive:
        if since_at is None:
            rows = await archive.fetch(
                "SELECT event_id, archived_at, sealed FROM nexus_ledger.events "
                "ORDER BY archived_at, event_id LIMIT $1", limit)
        else:
            rows = await archive.fetch(
                "SELECT event_id, archived_at, sealed FROM nexus_ledger.events "
                "WHERE (archived_at, event_id) > ($1, $2) ORDER BY archived_at, event_id LIMIT $3",
                since_at, since_id, limit)
    if not rows:
        return {"built": 0}

    from nexus.engagement.ledger.shipper import open_document

    events, examples, patches, facts, categories = [], [], [], [], []
    for row in rows:
        try:
            document = open_document(secret, row["sealed"])
        except Exception:
            # An unreadable row is almost always a rotated secret. Skipping keeps the rest of the
            # batch moving; the watermark still advances, and the archive row is the evidence.
            logger.warning("ledger archive row %s could not be opened", row["event_id"])
            continue
        built = datasets.build(document, secret)
        if built.event:
            events.append(built.event)
        examples.extend(built.examples)
        patches.extend(built.patches)
        facts.extend(built.facts)
        categories.extend(built.fact_categories)

    await _write_training(events, examples, patches)
    touched = await _write_insights(facts, categories)
    await _advance(rows[-1]["archived_at"], rows[-1]["event_id"])
    return {"built": len(rows), "examples": len(examples), "patches": len(patches),
            "facts": len(facts), "profiles": touched}


async def build(max_batches: int = MAX_BATCHES) -> dict:
    total = {"built": 0, "examples": 0, "facts": 0, "profiles": 0}
    for _ in range(max_batches):
        result = await build_once()
        if result.get("skipped"):
            return result
        for key in total:
            total[key] += result.get(key, 0)
        if result["built"] < BATCH:
            break
    return total


async def _advance(archived_at, event_id: str) -> None:
    async with connect("training") as training:
        await training.execute(
            "INSERT INTO nexus_ledger.build_state (name, archived_at, event_id, built_at) "
            "VALUES ($1, $2, $3, now()) "
            "ON CONFLICT (name) DO UPDATE SET archived_at = EXCLUDED.archived_at, "
            "event_id = EXCLUDED.event_id, built_at = now()",
            WATERMARK, archived_at, event_id)


async def last_built_at():
    async with connect("training") as training:
        return await training.fetchval(
            "SELECT built_at FROM nexus_ledger.build_state WHERE name = $1", WATERMARK)


async def _write_training(events: list[dict], examples: list, patches: list) -> None:
    if not (events or examples or patches):
        return
    async with connect("training") as training:
        async with training.transaction():
            if events:
                await training.executemany(
                    "INSERT INTO nexus_ledger.events (event_id, event_type, schema_version, "
                    " occurred_at, workspace_key, person_keys, body, scrubber_version, "
                    " consent_terms_version) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9) "
                    "ON CONFLICT (event_id) DO UPDATE SET body = EXCLUDED.body, "
                    "scrubber_version = EXCLUDED.scrubber_version",
                    [(e["event_id"], e["event_type"], e["schema_version"],
                      as_datetime(e["occurred_at"]),
                      e["workspace_key"], e["person_keys"], json.dumps(e["body"]),
                      e["scrubber_version"], e["consent_terms_version"]) for e in events])
            if examples:
                await training.executemany(
                    "INSERT INTO nexus_ledger.examples (example_id, dataset, created_at, "
                    " source_event_ids, workspace_key, person_keys, split, schema_version, "
                    " scrubber_version, consent_terms_version, quality, record) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, "
                    " $12::jsonb) "
                    "ON CONFLICT (example_id) DO UPDATE SET "
                    # The fresh record, with any labels or corrections already learned put BACK over
                    # it, so replaying the archive (for a scrubber fix) cannot un-learn a label a
                    # later event set. `jsonb_strip_nulls` drops the keys the stored row never had.
                    "  record = EXCLUDED.record || jsonb_strip_nulls(jsonb_build_object("
                    "    'labels', nexus_ledger.examples.record->'labels', "
                    "    'output', nexus_ledger.examples.record->'output')), "
                    "  quality = EXCLUDED.quality, scrubber_version = EXCLUDED.scrubber_version, "
                    "  source_event_ids = ARRAY(SELECT DISTINCT unnest("
                    "     nexus_ledger.examples.source_event_ids || EXCLUDED.source_event_ids)), "
                    "  updated_at = now()",
                    [(x.example_id, x.dataset, as_datetime(x.created_at), x.source_event_ids,
                      x.workspace_key,
                      x.person_keys, datasets.split_of(x.workspace_key), datasets.SCHEMA_VERSION,
                      datasets.SCRUBBER_VERSION, x.consent_terms_version, json.dumps(x.quality),
                      json.dumps(x.record)) for x in examples])
            for patch in patches:
                await training.execute(
                    "UPDATE nexus_ledger.examples SET record = jsonb_set(record, $2::text[], "
                    "  coalesce(record #> $2::text[], '{}'::jsonb) || $3::jsonb, true), "
                    "  source_event_ids = ARRAY(SELECT DISTINCT unnest(source_event_ids || $4)), "
                    "  updated_at = now() "
                    "WHERE example_id = $1",
                    patch.example_id, [patch.path], json.dumps(patch.values),
                    [patch.source_event_id])


async def _write_insights(facts: list, categories: list) -> int:
    """Facts, late-arriving categories, then a recomputed profile for everyone touched."""
    if not (facts or categories):
        return 0
    async with connect("insights") as store:
        async with store.transaction():
            if facts:
                await store.executemany(
                    "INSERT INTO nexus_ledger.facts (event_id, fact_type, person_email, person_key,"
                    " company_domain, workspace_key, occurred_at, local_hour, local_weekday, "
                    " response_latency_s, category, attrs) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb) "
                    "ON CONFLICT (event_id) DO NOTHING",
                    [(f.event_id, f.fact_type, f.person_email, f.person_key, f.company_domain,
                      f.workspace_key, as_datetime(f.occurred_at), f.local_hour,
                      f.local_weekday,
                      f.response_latency_s, f.category, json.dumps(f.attrs)) for f in facts])
            for category in categories:
                await store.execute(
                    "UPDATE nexus_ledger.facts SET category = $2 "
                    "WHERE fact_type = 'reply' AND attrs->>'message_id' = $1",
                    category.message_id, category.category)
        people = sorted({f.person_email for f in facts if f.person_email})
        domains = sorted({f.company_domain for f in facts if f.company_domain})
        for email in people:
            await refresh_person(store, email)
        for domain in domains:
            await refresh_company(store, domain)
    return len(people)


async def refresh_person(store, person_email: str) -> None:
    rows = await store.fetch(
        "SELECT fact_type, person_key, company_domain, workspace_key, occurred_at, local_hour, "
        "       local_weekday, response_latency_s, attrs "
        "FROM nexus_ledger.facts WHERE person_email = $1", person_email)
    profile = insights.person_profile(person_email, [dict(row) for row in rows])
    if profile is None:
        await store.execute("DELETE FROM nexus_ledger.person_profiles WHERE person_email = $1",
                            person_email)
        return
    await store.execute(
        "INSERT INTO nexus_ledger.person_profiles (person_email, person_key, company_domain, "
        " best_weekday, best_hour, median_response_s, reply_propensity, last_reply_band, "
        " ooo_periods, sends, replies, workspace_count, workspace_keys, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, now()) "
        "ON CONFLICT (person_email) DO UPDATE SET person_key = EXCLUDED.person_key, "
        " company_domain = EXCLUDED.company_domain, best_weekday = EXCLUDED.best_weekday, "
        " best_hour = EXCLUDED.best_hour, median_response_s = EXCLUDED.median_response_s, "
        " reply_propensity = EXCLUDED.reply_propensity, last_reply_band = EXCLUDED.last_reply_band, "
        " ooo_periods = EXCLUDED.ooo_periods, sends = EXCLUDED.sends, replies = EXCLUDED.replies, "
        " workspace_count = EXCLUDED.workspace_count, workspace_keys = EXCLUDED.workspace_keys, "
        " updated_at = now()",
        profile["person_email"], profile["person_key"], profile["company_domain"],
        profile["best_weekday"], profile["best_hour"], profile["median_response_s"],
        profile["reply_propensity"], profile["last_reply_band"], json.dumps(profile["ooo_periods"]),
        profile["sends"], profile["replies"], profile["workspace_count"],
        profile["workspace_keys"])


async def refresh_company(store, company_domain: str) -> None:
    rows = await store.fetch(
        "SELECT fact_type, person_key, company_domain, workspace_key, occurred_at, local_hour, "
        "       local_weekday, response_latency_s, attrs "
        "FROM nexus_ledger.facts WHERE company_domain = $1", company_domain)
    profile = insights.company_profile(company_domain, [dict(row) for row in rows])
    if profile is None:
        await store.execute("DELETE FROM nexus_ledger.company_profiles WHERE company_domain = $1",
                            company_domain)
        return
    await store.execute(
        "INSERT INTO nexus_ledger.company_profiles (company_domain, best_weekday, best_hour, "
        " median_response_s, reply_propensity, sends, replies, workspace_count, workspace_keys, "
        " updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now()) "
        "ON CONFLICT (company_domain) DO UPDATE SET best_weekday = EXCLUDED.best_weekday, "
        " best_hour = EXCLUDED.best_hour, median_response_s = EXCLUDED.median_response_s, "
        " reply_propensity = EXCLUDED.reply_propensity, sends = EXCLUDED.sends, "
        " replies = EXCLUDED.replies, workspace_count = EXCLUDED.workspace_count, "
        " workspace_keys = EXCLUDED.workspace_keys, updated_at = now()",
        profile["company_domain"], profile["best_weekday"], profile["best_hour"],
        profile["median_response_s"], profile["reply_propensity"], profile["sends"],
        profile["replies"], profile["workspace_count"], profile["workspace_keys"])
```

Two things in that SQL are load-bearing and neither is obvious:

- **The examples upsert puts the STORED `labels` and `output` back over the fresh record.** Rebuilding from the archive re-emits an example with blank labels; without this, replaying to apply a scrubber fix would un-learn every reply, positive and meeting label a later event had set. Pinned by the replay assertion in the integration suite.
- **A patch for an example that does not exist is dropped.** The archive is read in arrival order, and a reply is archived after the message it answers, so this cannot happen in normal order — and an `UPDATE` that matches no row is the right behaviour if it ever does, rather than inventing an example with no content.

- [ ] **Step 2: Check it imports cleanly**

Run: `python -c "import nexus.engagement.ledger.builder"`
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add nexus/engagement/ledger/builder.py
git commit -m "feat(ledger): build training and insights from the archive, watermarked and replayable"
```

---

### Task 8: Deletion and erasure

**Files:**
- Create: `nexus/engagement/ledger/deletion.py`
- Test: `tests_integration/test_ledger_stores_pg.py` (Task 11)

- [ ] **Step 1: Implement**

`nexus/engagement/ledger/deletion.py`:

```python
"""Deleting a workspace's contribution, and erasing one person, from all three stores (spec §18.6).

Two requests, one shape: delete everywhere, then COUNT WHAT REMAINS and report it. A deletion that
reports what it did is a claim; a deletion that reports what is left is evidence, and the expected
answer is zero. The report is written to the audit log by the caller.

**Erasure deletes the archive rows, rather than blanking fields in them.** The archive holds the
email that was sent — the person's name is in the greeting and their address in the body — so
removing "identity fields" would leave the person's data in the text. The row is the unit that is
about them, and the training and insights rows are rebuilt from the archive, so removing it there is
what makes the deletion hold after any later rebuild.

Ordering matters in one place: the person's still-unshipped events are shipped FIRST, so an event
recorded a minute before the request cannot arrive in the archive a minute after the erasure ran.
"""
from __future__ import annotations

import logging

from nexus.engagement.ledger.stores import STORES, StoreNotConfigured, connect

logger = logging.getLogger("nexus.engagement.ledger.deletion")


async def delete_workspace(tenant_id: str) -> dict:
    """Remove everything this workspace contributed. Idempotent."""
    from nexus.engagement import config
    from nexus.engagement.ledger.pseudonym import workspace_key

    report = {"tenant_id": tenant_id, "deleted": {}, "remaining": {}, "unconfigured": []}
    secret = await config.pseudonym_secret()
    if not secret:
        report["error"] = "the pseudonymisation secret is not configured"
        return report
    wkey = workspace_key(secret, tenant_id)

    await _outbox_delete(tenant_id, report)

    for store in STORES:
        try:
            async with connect(store) as conn:
                if store == "archive":
                    deleted = await conn.execute(
                        "DELETE FROM nexus_ledger.events WHERE tenant_id = $1", tenant_id)
                    remaining = await conn.fetchval(
                        "SELECT count(*) FROM nexus_ledger.events WHERE tenant_id = $1", tenant_id)
                elif store == "training":
                    deleted = await conn.execute(
                        "DELETE FROM nexus_ledger.events WHERE workspace_key = $1", wkey)
                    deleted += " " + await conn.execute(
                        "DELETE FROM nexus_ledger.examples WHERE workspace_key = $1", wkey)
                    remaining = await conn.fetchval(
                        "SELECT (SELECT count(*) FROM nexus_ledger.events WHERE workspace_key = $1)"
                        " + (SELECT count(*) FROM nexus_ledger.examples WHERE workspace_key = $1)",
                        wkey)
                else:
                    people, domains = await _insights_scope(conn, "workspace_key = $1", wkey)
                    deleted = await conn.execute(
                        "DELETE FROM nexus_ledger.facts WHERE workspace_key = $1", wkey)
                    await _reprofile(conn, people, domains)
                    remaining = await conn.fetchval(
                        "SELECT count(*) FROM nexus_ledger.facts WHERE workspace_key = $1", wkey)
                report["deleted"][store] = _rows(deleted)
                report["remaining"][store] = int(remaining or 0)
        except StoreNotConfigured:
            report["unconfigured"].append(store)
        except Exception as exc:
            report["remaining"][store] = -1
            report.setdefault("errors", {})[store] = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("ledger deletion failed for %s", store, exc_info=True)
    return report


async def erase_person(person_key: str) -> dict:
    """Remove one person from every store, by their deterministic key."""
    report = {"person_key": person_key, "deleted": {}, "remaining": {}, "unconfigured": []}
    for store in STORES:
        try:
            async with connect(store) as conn:
                if store in ("archive", "training"):
                    deleted = await conn.execute(
                        "DELETE FROM nexus_ledger.events WHERE person_keys @> ARRAY[$1]::text[]",
                        person_key)
                    remaining = await conn.fetchval(
                        "SELECT count(*) FROM nexus_ledger.events "
                        "WHERE person_keys @> ARRAY[$1]::text[]", person_key)
                    if store == "training":
                        deleted += " " + await conn.execute(
                            "DELETE FROM nexus_ledger.examples "
                            "WHERE person_keys @> ARRAY[$1]::text[]", person_key)
                        remaining += await conn.fetchval(
                            "SELECT count(*) FROM nexus_ledger.examples "
                            "WHERE person_keys @> ARRAY[$1]::text[]", person_key)
                else:
                    _people, domains = await _insights_scope(conn, "person_key = $1", person_key)
                    deleted = await conn.execute(
                        "DELETE FROM nexus_ledger.facts WHERE person_key = $1", person_key)
                    await conn.execute(
                        "DELETE FROM nexus_ledger.person_profiles WHERE person_key = $1",
                        person_key)
                    # The company they worked at still has a profile; it must stop counting them.
                    await _reprofile(conn, [], domains)
                    remaining = await conn.fetchval(
                        "SELECT (SELECT count(*) FROM nexus_ledger.facts WHERE person_key = $1) "
                        "+ (SELECT count(*) FROM nexus_ledger.person_profiles "
                        "   WHERE person_key = $1)", person_key)
                report["deleted"][store] = _rows(deleted)
                report["remaining"][store] = int(remaining or 0)
        except StoreNotConfigured:
            report["unconfigured"].append(store)
        except Exception as exc:
            report["remaining"][store] = -1
            report.setdefault("errors", {})[store] = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("ledger erasure failed for %s", store, exc_info=True)
    return report


async def _insights_scope(conn, where: str, value: str) -> tuple[list[str], list[str]]:
    """Whose profiles this delete will invalidate, read BEFORE the rows go."""
    rows = await conn.fetch(
        f"SELECT DISTINCT person_email, company_domain FROM nexus_ledger.facts WHERE {where}", value)
    return ([r["person_email"] for r in rows if r["person_email"]],
            [r["company_domain"] for r in rows if r["company_domain"]])


async def _reprofile(conn, people: list[str], domains: list[str]) -> None:
    from nexus.engagement.ledger.builder import refresh_company, refresh_person

    for email in sorted(set(people)):
        await refresh_person(conn, email)
    for domain in sorted(set(domains)):
        await refresh_company(conn, domain)


async def _outbox_delete(tenant_id: str, report: dict) -> None:
    """Anything not yet shipped goes too — otherwise the next tick re-creates what we just deleted."""
    from sqlalchemy import delete

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.ledger import LedgerOutbox

    async with get_platform_sessionmaker()() as session:
        result = await session.execute(
            delete(LedgerOutbox).where(LedgerOutbox.tenant_id == tenant_id))
        await session.commit()
    report["deleted"]["outbox"] = int(result.rowcount or 0)
    report["remaining"]["outbox"] = 0


def _rows(status) -> int:
    """asyncpg returns ``"DELETE 12"``; several statements are joined with a space."""
    total = 0
    for part in str(status).split():
        if part.isdigit():
            total += int(part)
    return total
```

> **Erasure deletes archive rows rather than blanking fields in them.** The archive holds the email that was sent: the person's name is in the greeting and their address in the body, so removing "identity fields" would leave their data in the text. The row is the unit that is about them, and because training and insights are rebuilt from the archive, removing it there is what makes the deletion hold after any later rebuild.

- [ ] **Step 2: Check it imports cleanly**

Run: `python -c "import nexus.engagement.ledger.deletion"`
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add nexus/engagement/ledger/deletion.py
git commit -m "feat(ledger): delete a workspace's contribution, erase a person, report what remains"
```

---

### Task 9: The four jobs

**Files:**
- Modify: `nexus/workers/tasks.py`, `nexus/workers/scheduler.py`, `nexus/api/routers/engagement_settings.py`, `nexus/people/store.py`, `tests/test_continuous_automation.py`, `tests/test_crm_auto_sync.py`
- Test: `tests/test_engagement_ledger_stores.py`

- [ ] **Step 1: Write the failing tests**

Add `test_the_four_ledger_jobs_are_registered_and_do_nothing_without_a_store`, `test_the_scheduler_drives_shipping_and_building_whatever_automation_says`, `test_switching_training_off_queues_the_deletion_of_what_was_already_shipped` and `test_erasing_a_shared_person_reaches_the_ledger`.

In `tests/test_continuous_automation.py`, the scheduler now enqueues two more drivers: `assert count == 13` → `assert count == 15`, `assert count == 9` → `assert count == 11`, and add to both expected sets:

```python
        # The ledger ships and builds on the heartbeat too: a workspace that opted in is
        # contributing whether or not it switched automation on.
        "ship_ledger",
        "build_ledger_datasets",
```

The same two names go into both expected sets in `tests/test_crm_auto_sync.py`, which assert the whole set as well.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_ledger_stores.py tests/test_continuous_automation.py -n0 -q`
Expected: FAIL — `ship_ledger` is not in `HANDLERS`, and `13 != 15`.

- [ ] **Step 3: Implement**

In `nexus/workers/tasks.py`, add the four handlers immediately above `enqueue_refresh_mailbox_tokens`:

```python
async def handle_ship_ledger(payload: dict) -> dict:
    """Ship consented events to the archive, then drop what has been archived for a week.

    Raises when a store refused, so the batch retries and finally dead-letters with its evidence.
    Rows carry their own backoff, so the ticks in between find nothing due and return quietly rather
    than dead-lettering once a minute for the length of an outage (spec §18.2)."""
    from nexus.engagement.ledger import shipper

    result = await shipper.ship()
    if not result.get("skipped"):
        result["purged"] = await shipper.purge_shipped()
    return result


async def handle_build_ledger_datasets(payload: dict) -> dict:
    """Turn newly archived events into training examples and insights facts, hourly.

    Self-limiting on the watermark's own `built_at`, so the heartbeat can enqueue it every tick: the
    worker is not the only process that could run this, and an hour kept in a module variable would
    be wrong in the second replica."""
    from datetime import timedelta

    from nexus.core.db import utcnow
    from nexus.engagement.ledger import builder
    from nexus.engagement.ledger.stores import StoreNotConfigured

    try:
        last = await builder.last_built_at()
    except StoreNotConfigured:
        return {"skipped": "the training store is not configured"}
    except Exception:
        last = None
    if last is not None and utcnow() - last < timedelta(hours=1):
        return {"skipped": "built less than an hour ago"}
    return await builder.build()


async def handle_ledger_delete_workspace(payload: dict) -> dict:
    """A workspace switched training off: remove what it already contributed (spec §18.6)."""
    from nexus.engagement.ledger import deletion

    tenant_id = payload.get("tenant_id") or ""
    if not tenant_id:
        return {"error": "no tenant"}
    report = await deletion.delete_workspace(tenant_id)
    async with tenant_session(tenant_id) as ts:
        from nexus.core.audit import record_audit

        await record_audit(ts, "ledger.workspace_deleted", target_type="tenant",
                           target_id=tenant_id, meta=report)
    return report


async def handle_ledger_erase_person(payload: dict) -> dict:
    """Erase one person everywhere, by key. Ships the outbox first so an event recorded seconds
    before the request cannot land in the archive seconds after the erasure."""
    from nexus.engagement.ledger import deletion, shipper

    person_key = payload.get("person_key") or ""
    if not person_key:
        return {"error": "no person key"}
    try:
        await shipper.ship()
    except Exception:
        logger.warning("could not flush the outbox before an erasure", exc_info=True)
    return await deletion.erase_person(person_key)


async def enqueue_ship_ledger(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="ship_ledger", payload={}))


async def enqueue_build_ledger_datasets(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="build_ledger_datasets", payload={}))


async def enqueue_ledger_delete_workspace(tenant_id: str, *,
                                          queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="ledger_delete_workspace", payload={"tenant_id": tenant_id}))


async def enqueue_ledger_erase_person(person_key: str, *,
                                      queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="ledger_erase_person", payload={"person_key": person_key}))
```

and register all four at the end of `HANDLERS`:

```python
    "ship_ledger": handle_ship_ledger,
    "build_ledger_datasets": handle_build_ledger_datasets,
    "ledger_delete_workspace": handle_ledger_delete_workspace,
    "ledger_erase_person": handle_ledger_erase_person,
```

In `nexus/workers/scheduler.py`, import `enqueue_ship_ledger` and `enqueue_build_ledger_datasets` beside the other enqueue functions, and add them after the mailbox-refresh call:

```python
            # The ledger collects for workspaces that opted in, which is not an automation
            # opt-in either; both handlers self-filter (nothing due, nothing consented,
            # no store configured) and cost one indexed query when there is nothing to do.
            await enqueue_ship_ledger(queue=queue)
            await enqueue_build_ledger_datasets(queue=queue)
            count += 2
```

In `nexus/api/routers/engagement_settings.py`, switching consent off now also removes what was already shipped — add to the `if body.status == "off":` branch, after the outbox delete:

```python
        # What has already been shipped is removed by the deletion job, which writes its
        # verification report (rows remaining, expected zero) to the audit log (spec §18.6).
        from nexus.workers.tasks import enqueue_ledger_delete_workspace

        await enqueue_ledger_delete_workspace(ts.tenant_id)
```

In `nexus/people/store.py`, `forget_person` reads the address before the row goes, and queues the ledger erasure after the flush:

```python
    await session.execute(delete(PersonIdentity).where(PersonIdentity.person_id == person_id))
    email = unseal_text(person.email_encrypted or "", key=_enc_key())
    await session.delete(person)
    await session.flush()
    await _erase_from_ledger(email)
    logger.info("erased shared person record %s", person_id)
    return True


async def _erase_from_ledger(email: str) -> None:
    """The same request reaches the ledger stores (spec §18.6). Never raises: the person's
    record here is already gone, and failing the erasure would leave it.

    The job carries the person KEY, never the address: a dead-lettered job logs its payload."""
    if not email:
        return
    try:
        from nexus.engagement import config
        from nexus.engagement.ledger.pseudonym import person_key
        from nexus.workers.tasks import enqueue_ledger_erase_person

        secret = await config.pseudonym_secret()
        if secret:
            await enqueue_ledger_erase_person(person_key(secret, email))
    except Exception:
        logger.warning("could not queue the ledger erasure", exc_info=True)
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_ledger_stores.py tests/test_continuous_automation.py tests/test_crm_auto_sync.py tests/test_job_durability.py tests/test_people_store.py -n0 -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add nexus/workers nexus/api/routers/engagement_settings.py nexus/people/store.py tests/
git commit -m "feat(ledger): ship and build on the heartbeat; opting out and erasure reach the stores"
```

---

### Task 10: The Ledger tab

**Files:**
- Create: `nexus/api/routers/admin_ledger.py`, `frontend/src/pages/admin/LedgerTab.tsx` (+ `.module.css`)
- Modify: `nexus/api/routers/__init__.py`, `nexus/api/routers/admin_health.py`, `frontend/src/pages/AdminBillingPage.tsx`, `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`
- Test: `tests/test_engagement_ledger_stores.py`

- [ ] **Step 1: Write the failing tests**

Add `test_the_ledger_tab_is_platform_only_and_never_shows_a_connection_string`, `test_applying_a_schema_to_an_unconfigured_store_is_a_409`, `test_erasing_a_person_queues_the_key_and_needs_the_secret` and `test_the_ledger_tab_shows_state_and_the_two_actions_and_no_dsn`.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_ledger_stores.py -n0 -q`
Expected: FAIL — `/api/admin/ledger` is 404 for everyone, including a platform admin.

- [ ] **Step 3: Implement the API**

`nexus/api/routers/admin_ledger.py`:

```python
"""The Ledger tab: is each store configured, reachable and at the right schema version (spec §18.2).

Gated on ``providers.manage``, the same permission as the connection strings themselves: applying a
schema and erasing a person are both acts on somebody else's database, and the people who hold those
credentials are the people who do them. No DSN is in any response — a store is reported by name,
state and version.

Erasure is here rather than on a tenant route on purpose: it deletes a person from every workspace's
contribution at once, which is a platform act, and the request names the person by address while the
job that does the work carries only their key.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.permissions import PROVIDERS_MANAGE

router = APIRouter(prefix="/admin/ledger", tags=["admin-ledger"])


class StoreStatusOut(BaseModel):
    store: str
    configured: bool
    reachable: bool
    owns_schema: bool
    applied: list[str]
    pending: list[str]
    detail: str


class LedgerStatusOut(BaseModel):
    capture_enabled: bool
    pseudonym_secret_configured: bool
    stores: list[StoreStatusOut]
    outbox: dict
    last_built_at: str | None
    consented_workspaces: int
    opted_out_workspaces: int
    undecided_workspaces: int


class ApplySchemaOut(BaseModel):
    store: str
    applied: list[str]
    status: StoreStatusOut


class ErasePersonIn(BaseModel):
    model_config = {"extra": "forbid"}

    email: EmailStr


class ErasePersonOut(BaseModel):
    queued: bool
    person_key: str


@router.get("", response_model=LedgerStatusOut)
async def ledger_status(
    _: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> LedgerStatusOut:
    from nexus.core.config import get_settings
    from nexus.engagement import config
    from nexus.engagement.ledger import builder, shipper, stores

    secret = await config.pseudonym_secret()
    try:
        built_at = await builder.last_built_at() if secret else None
    except Exception:
        built_at = None
    return LedgerStatusOut(
        capture_enabled=get_settings().ledger_capture_enabled,
        pseudonym_secret_configured=bool(secret),
        stores=[StoreStatusOut(**await stores.status(store)) for store in stores.STORES],
        outbox=await shipper.backlog(),
        last_built_at=built_at.isoformat() if built_at else None,
        **await _consent_counts(),
    )


@router.post("/{store}/schema", response_model=ApplySchemaOut)
async def apply_store_schema(
    store: str,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> ApplySchemaOut:
    from nexus.engagement.ledger import schema, stores

    if store not in stores.STORES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown ledger store")
    try:
        applied = await schema.apply_schema(store)
    except stores.StoreNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This store has no connection string yet") from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"The store refused the schema: {type(exc).__name__}") from exc
    await _audit(principal, "ledger.schema_applied", store, {"applied": applied})
    return ApplySchemaOut(store=store, applied=applied,
                          status=StoreStatusOut(**await stores.status(store)))


@router.post("/erase-person", response_model=ErasePersonOut)
async def erase_person(
    body: ErasePersonIn,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> ErasePersonOut:
    from nexus.engagement import config
    from nexus.engagement.ledger.pseudonym import person_key
    from nexus.workers.tasks import enqueue_ledger_erase_person

    secret = await config.pseudonym_secret()
    if not secret:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "The pseudonymisation secret is not configured, so nothing can be "
                            "found by person key")
    key = person_key(secret, str(body.email))
    await enqueue_ledger_erase_person(key)
    # The address is never written to the audit row: the key is what the stores hold, and an audit
    # log of "who asked us to forget whom" would be the record they asked us to remove.
    await _audit(principal, "ledger.person_erased", key, {"queued": True})
    return ErasePersonOut(queued=True, person_key=key)


async def _consent_counts() -> dict:
    """How many workspaces contribute. Cross-tenant, so it runs on the platform sessionmaker."""
    from sqlalchemy import func, select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.identity import Tenant
    from nexus.models.ledger import TrainingConsent

    async with get_platform_sessionmaker()() as session:
        newest = (
            select(TrainingConsent.tenant_id,
                   func.max(TrainingConsent.decided_at).label("decided_at"))
            .group_by(TrainingConsent.tenant_id)
            .subquery()
        )
        rows = (await session.execute(
            select(TrainingConsent.status, func.count())
            .join(newest, (TrainingConsent.tenant_id == newest.c.tenant_id)
                  & (TrainingConsent.decided_at == newest.c.decided_at))
            .group_by(TrainingConsent.status)
        )).all()
        decided = {status_value: int(count) for status_value, count in rows}
        tenants = (await session.execute(select(func.count(Tenant.id)))).scalar_one()
    on, off = decided.get("on", 0), decided.get("off", 0)
    return {"consented_workspaces": on, "opted_out_workspaces": off,
            "undecided_workspaces": max(int(tenants) - on - off, 0)}


async def _audit(principal: Principal, action: str, target: str, after: dict) -> None:
    from nexus.billing.audit import record_admin_action
    from nexus.core.db import get_platform_sessionmaker

    async with get_platform_sessionmaker()() as session:
        await record_admin_action(session, actor=principal.user_id, action=action,
                                  target=target, after=after)
        await session.commit()
```

In `nexus/api/routers/__init__.py`, import `admin_ledger` beside `admin_engagement` and register `admin_ledger.router` beside `admin_engagement.router`.

In `nexus/api/routers/admin_health.py`, add the probe above `_probe_ledger_stores` and register it after that row in `_PROBES`:

```python
async def _probe_ledger_outbox() -> tuple[str, str]:
    """How much is waiting to be shipped, and how old the oldest is.

    A backlog that keeps growing is how a store outage shows up before anybody reads a dataset — the
    shipper itself is quiet by design, because it backs each row off rather than dead-lettering
    every tick."""
    from nexus.engagement.ledger import shipper

    backlog = await shipper.backlog()
    hours = backlog["oldest_age_s"] // 3600
    if backlog["waiting"] == 0:
        return OK, "nothing waiting"
    detail = f"{backlog['waiting']} events waiting, oldest {hours}h"
    if backlog["retrying"]:
        detail += f", {backlog['retrying']} retrying"
    return (ERROR if (hours >= 6 or backlog["retrying"]) else OK), detail
```

```python
    ("ledger outbox", _probe_ledger_outbox),
```

- [ ] **Step 4: Implement the client and the tab**

In `frontend/src/lib/types.ts`, append:

```ts
/** GET /admin/ledger — one ledger store's state (spec §18.2). */
export interface LedgerStoreStatus {
  store: string;
  configured: boolean;
  reachable: boolean;
  owns_schema: boolean;
  applied: string[];
  pending: string[];
  detail: string;
}

export interface LedgerStatus {
  capture_enabled: boolean;
  pseudonym_secret_configured: boolean;
  stores: LedgerStoreStatus[];
  outbox: {
    waiting: number;
    oldest_age_s: number;
    retrying: number;
    max_attempts: number;
  };
  last_built_at: string | null;
  consented_workspaces: number;
  opted_out_workspaces: number;
  undecided_workspaces: number;
}
```

In `frontend/src/lib/api.ts`, import `LedgerStatus` and `LedgerStoreStatus` beside `TrainingConsentState`, and add above the consent section:

```ts
  // ---- admin: the training & insights ledger ----
  ledgerStatus(signal?: AbortSignal) {
    return this.request<LedgerStatus>("/admin/ledger", { signal });
  }
  applyLedgerSchema(store: string) {
    return this.request<{ store: string; applied: string[]; status: LedgerStoreStatus }>(
      `/admin/ledger/${store}/schema`, { method: "POST" });
  }
  eraseLedgerPerson(email: string) {
    return this.request<{ queued: boolean; person_key: string }>("/admin/ledger/erase-person", {
      method: "POST", body: { email },
    });
  }
```

`frontend/src/pages/admin/LedgerTab.tsx`:

```tsx
import { type FormEvent, useState } from "react";

import { useApiClient } from "@/app/AuthContext";
import { DataState } from "@/components/DataState";
import { Badge, Button, CardHeader, Skeleton } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import type { LedgerStatus, LedgerStoreStatus } from "@/lib/types";

import styles from "./LedgerTab.module.css";

/**
 * The training & insights ledger: what each store is doing, how much is waiting, and the two
 * actions an operator has — apply a store's schema, and erase a person everywhere.
 *
 * No connection string is ever rendered. They are entered in Provider keys, sealed there, and this
 * screen reports only state: configured, reachable, owns its schema, which versions are applied.
 */
export function LedgerTab() {
  const api = useApiClient();
  const toast = useToast();
  const [nonce, setNonce] = useState(0);
  const status = useApi<LedgerStatus>((signal) => api.ledgerStatus(signal), [nonce]);
  const [busy, setBusy] = useState("");
  const [email, setEmail] = useState("");

  async function applySchema(store: string) {
    setBusy(store);
    try {
      const result = await api.applyLedgerSchema(store);
      toast.success(
        result.applied.length
          ? `${store}: applied ${result.applied.join(", ")}`
          : `${store}: already up to date`,
      );
      setNonce((n) => n + 1);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : `Could not apply the ${store} schema`);
    } finally {
      setBusy("");
    }
  }

  async function erase(event: FormEvent) {
    event.preventDefault();
    if (!email.trim()) return;
    setBusy("erase");
    try {
      const result = await api.eraseLedgerPerson(email.trim());
      toast.success(`Queued: every store will be cleared of ${result.person_key.slice(0, 8)}…`);
      setEmail("");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not queue the erasure");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className={styles.stack}>
      <CardHeader
        title="Training &amp; insights ledger"
        subtitle="What workspaces that opted in have contributed, where it is stored, and the two operator actions: apply a store's schema, and erase a person from every store."
      />
      <DataState
        state={status}
        errorTitle="Couldn't load the ledger status"
        skeleton={<Skeleton width="100%" height={320} />}
      >
        {(s) => (
          <>
            <section className={styles.section} aria-labelledby="ledger-collection">
              <h3 id="ledger-collection" className={styles.heading}>Collection</h3>
              <dl className={styles.facts}>
                <div>
                  <dt>Capture</dt>
                  <dd>
                    <Badge tone={s.capture_enabled ? "success" : "warning"} dot>
                      {s.capture_enabled ? "On" : "Off"}
                    </Badge>{" "}
                    {s.capture_enabled ? "" : "Runtime settings → Mailboxes & engagement"}
                  </dd>
                </div>
                <div>
                  <dt>Pseudonymisation secret</dt>
                  <dd>
                    <Badge tone={s.pseudonym_secret_configured ? "success" : "warning"} dot>
                      {s.pseudonym_secret_configured ? "Stored" : "Missing"}
                    </Badge>
                  </dd>
                </div>
                <div>
                  <dt>Workspaces</dt>
                  <dd>
                    {s.consented_workspaces} contributing · {s.opted_out_workspaces} off ·{" "}
                    {s.undecided_workspaces} not asked
                  </dd>
                </div>
                <div>
                  <dt>Waiting to ship</dt>
                  <dd>
                    {s.outbox.waiting === 0
                      ? "Nothing waiting"
                      : `${s.outbox.waiting} events, oldest ${Math.floor(
                          s.outbox.oldest_age_s / 3600,
                        )}h${s.outbox.retrying ? `, ${s.outbox.retrying} retrying` : ""}`}
                  </dd>
                </div>
                <div>
                  <dt>Datasets last built</dt>
                  <dd>
                    {s.last_built_at ? new Date(s.last_built_at).toLocaleString() : "Never"}
                  </dd>
                </div>
              </dl>
            </section>

            <section className={styles.section} aria-labelledby="ledger-stores">
              <h3 id="ledger-stores" className={styles.heading}>Stores</h3>
              <ul className={styles.stores}>
                {s.stores.map((store) => (
                  <StoreCard
                    key={store.store}
                    store={store}
                    busy={busy === store.store}
                    onApply={() => void applySchema(store.store)}
                  />
                ))}
              </ul>
            </section>

            <section className={styles.section} aria-labelledby="ledger-erase">
              <h3 id="ledger-erase" className={styles.heading}>Erase a person</h3>
              <p className={styles.hint}>
                Removes every event, training row, fact and profile about this address from all
                three stores, including what has not been shipped yet. The address itself is never
                stored in the job or the audit row — only the key derived from it.
              </p>
              <form className={styles.row} onSubmit={erase}>
                <input
                  className={styles.input}
                  type="email"
                  value={email}
                  placeholder="person@example.com"
                  aria-label="Email address to erase"
                  onChange={(e) => setEmail(e.target.value)}
                />
                <Button type="submit" variant="danger" disabled={busy === "erase" || !email.trim()}>
                  {busy === "erase" ? "Queueing…" : "Erase everywhere"}
                </Button>
              </form>
            </section>
          </>
        )}
      </DataState>
    </div>
  );
}

function StoreCard({
  store,
  busy,
  onApply,
}: {
  store: LedgerStoreStatus;
  busy: boolean;
  onApply: () => void;
}) {
  const state = !store.configured
    ? "Not configured"
    : !store.reachable
      ? "Unreachable"
      : store.pending.length
        ? `${store.pending.length} version(s) pending`
        : "Up to date";
  const tone = !store.configured || !store.reachable
    ? "warning"
    : store.pending.length
      ? "neutral"
      : "success";
  return (
    <li className={styles.store}>
      <div>
        <strong>{store.store}</strong>{" "}
        <Badge tone={tone} dot>
          {state}
        </Badge>
        {store.detail && <p className={styles.detail}>{store.detail}</p>}
        <p className={styles.detail}>
          Applied: {store.applied.length ? store.applied.join(", ") : "none"}
          {store.owns_schema ? " · owns nexus_ledger" : ""}
        </p>
      </div>
      <Button
        variant="secondary"
        disabled={busy || !store.configured || !store.pending.length}
        onClick={onApply}
      >
        {busy ? "Applying…" : "Apply schema"}
      </Button>
    </li>
  );
}
```

`frontend/src/pages/admin/LedgerTab.module.css`:

```css
.stack {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}
.section {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.heading {
  margin: 0;
  font-size: var(--text-base);
  font-weight: 600;
}
.facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
  gap: var(--space-3);
  margin: 0;
}
.facts dt {
  font-size: var(--text-sm);
  color: var(--text-muted);
}
.facts dd {
  margin: 0;
  font-weight: 500;
}
.stores {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}
.store {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}
.state {
  margin-left: var(--space-2);
  font-size: var(--text-sm);
  color: var(--text-muted);
}
.detail {
  margin: var(--space-1) 0 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
}
.hint {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
  max-width: 60ch;
}
.row {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  flex-wrap: wrap;
}
.input {
  flex: 1 1 18rem;
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text);
}
.note {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
}
```

In `frontend/src/pages/AdminBillingPage.tsx`: import `LedgerTab` beside `EngagementSetupTab`, add the tab after the Mailbox apps one —

```tsx
    ...(can(PROVIDERS_MANAGE) ? [{ value: "ledger", label: "Ledger" }] : []),
```

— and render it beside the others:

```tsx
          {tab === "ledger" && can(PROVIDERS_MANAGE) && <LedgerTab />}
```

- [ ] **Step 5: Run to see them pass**

Run: `pytest tests/test_engagement_ledger_stores.py tests/test_admin_health.py tests/test_admin_routes_are_not_discoverable.py -n0 -q`
Then: `cd frontend && npm run typecheck`
Expected: tests pass; typecheck clean.

- [ ] **Step 6: Commit**

```bash
git add nexus/api/routers frontend/src tests/test_engagement_ledger_stores.py
git commit -m "feat(ledger): a Ledger tab — store state, apply schema, erase a person"
```

---

### Task 11: Proving it against real Postgres

**Files:**
- Create: `tests_integration/test_ledger_stores_pg.py`, `tests_live/engagement/test_ledger_live.py`, `scripts/export_training_dataset.py`, `docs/engagement/setup-supabase.md`
- Modify: `pyproject.toml`, `.github/workflows/ci.yml`

- [ ] **Step 1: Write the integration suite**

`tests_integration/test_ledger_stores_pg.py` — the name matters: `tests/` and `tests_integration/` are both non-packages, so two files called `test_engagement_ledger_stores.py` collide at collection.

```python
"""The ledger stores against REAL Postgres: schema, shipping, building, deletion (spec §18.2-§18.6).

The offline suite proves the pure halves. Everything here is the part a fake cannot prove: that the
versioned SQL applies and is idempotent, that the upserts converge when a batch is replayed, that
`jsonb` label merging keeps what a later event learned, that `text[]` person keys can be searched,
and that a deletion actually leaves zero rows.

Skipped unless `NEXUS_TEST_POSTGRES_URL` is set, exactly like the other integration tests. The CI
leg provides a superuser connection; this module creates its own role and four databases inside it —
three stores plus an app database — and drops them afterwards, so it leaves the server as it found
it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import asyncpg
import pytest
import pytest_asyncio

from tests_integration.conftest import PG_URL, requires_pg

pytestmark = [pytest.mark.asyncio, requires_pg]

SECRET = "an-integration-pseudonymisation-secret-0123456789abcdef"
ROLE = "nexus_ledger_test"
PASSWORD = "ledger-test-password"
DATABASES = {"archive": "nexus_ledger_archive_test", "training": "nexus_ledger_training_test",
             "insights": "nexus_ledger_insights_test"}
APP_DB = "nexus_ledger_app_test"
TENANT = "t-integration"
NOW = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)


def _admin_dsn() -> str:
    return PG_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


def _store_dsn(database: str) -> str:
    parsed = urlparse(_admin_dsn())
    return f"postgresql://{ROLE}:{PASSWORD}@{parsed.hostname}:{parsed.port or 5432}/{database}"


async def _admin(database: str = ""):
    dsn = _admin_dsn()
    if database:
        dsn = dsn.rsplit("/", 1)[0] + "/" + database
    return await asyncpg.connect(dsn, timeout=20)


@pytest_asyncio.fixture
async def stores(monkeypatch):
    """A role that owns `nexus_ledger` in three databases, plus an app database, torn down after."""
    from nexus.core import db as core_db
    from nexus.core.config import get_settings
    from nexus.providers import resolver

    admin = await _admin()
    await admin.execute(f"DROP ROLE IF EXISTS {ROLE}")
    for database in (*DATABASES.values(), APP_DB):
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    await admin.execute(f"CREATE ROLE {ROLE} LOGIN PASSWORD '{PASSWORD}'")
    for database in (*DATABASES.values(), APP_DB):
        await admin.execute(f'CREATE DATABASE "{database}" OWNER {ROLE}')
    await admin.close()

    for database in DATABASES.values():
        conn = await _admin(database)
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS nexus_ledger AUTHORIZATION {ROLE}")
        await conn.close()

    settings = get_settings()
    monkeypatch.setattr(settings, "ledger_pseudonym_secret", SECRET)
    monkeypatch.setattr(settings, "source_db_allow_private", True)
    for store, database in DATABASES.items():
        monkeypatch.setattr(settings, f"ledger_{store}_dsn", _store_dsn(database))
    # The app database moves with us, so the shipper's outbox reads hit real Postgres too.
    monkeypatch.setattr(settings, "database_url",
                        _store_dsn(APP_DB).replace("postgresql://", "postgresql+asyncpg://", 1))
    monkeypatch.setattr(settings, "db_owner_url", "")
    resolver.invalidate()
    await core_db.dispose_db()
    monkeypatch.setattr(core_db, "_platform_engine", None, raising=False)
    monkeypatch.setattr(core_db, "_platform_sessionmaker", None, raising=False)

    # The whole app schema, not just the ledger tables: the provider-key resolver reads
    # `provider_keys` on every secret lookup, and a missing table there is not the failure this
    # module is testing.
    await core_db.init_db()

    yield

    await core_db.dispose_db()
    admin = await _admin()
    for database in (*DATABASES.values(), APP_DB):
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    await admin.execute(f"DROP ROLE IF EXISTS {ROLE}")
    await admin.close()
    resolver.invalidate()


async def _apply_all() -> None:
    from nexus.engagement.ledger import schema

    for store in DATABASES:
        await schema.apply_schema(store)


async def _outbox(event_type: str, payload: dict, refs: dict, event_id: str) -> None:
    from nexus.core.db import get_platform_sessionmaker
    from nexus.engagement.ledger import envelope
    from nexus.models.ledger import LedgerOutbox

    body = envelope.build(event_id=event_id, event_type=event_type, tenant_id=TENANT,
                          occurred_at=NOW, refs=refs, payload=payload)
    async with get_platform_sessionmaker()() as session:
        session.add(LedgerOutbox(tenant_id=TENANT, event_id=event_id, event_type=event_type,
                                 schema_version=1, occurred_at=NOW, payload=body))
        await session.commit()


async def _seed_records() -> None:
    """A tenant, an account and a contact in the app database, so the shipper resolves a real
    person — which is what turns a sent message into an insights fact."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.account import Account, Contact
    from nexus.models.identity import Tenant

    async with get_platform_sessionmaker()() as session:
        session.add(Tenant(id=TENANT, name="Integration", slug="integration"))
        session.add(Account(id="a-1", tenant_id=TENANT, name="Acme Robotics", domain="acme.io"))
        session.add(Contact(id="c-1", tenant_id=TENANT, account_id="a-1", full_name="Jane Buyer",
                            email="jane.buyer@acme.io", title="VP Engineering"))
        await session.commit()


async def _count(store: str, sql: str, *args) -> int:
    from nexus.engagement.ledger.stores import connect

    async with connect(store) as conn:
        return int(await conn.fetchval(sql, *args) or 0)


async def test_the_schema_applies_once_and_is_safe_to_apply_again(stores):
    from nexus.engagement.ledger import schema, stores as store_module

    for store in DATABASES:
        applied = await schema.apply_schema(store)
        assert applied == ["0001_initial"], store
        assert await schema.apply_schema(store) == [], f"{store} re-applied a version"
        status = await store_module.status(store)
        assert status["reachable"] and status["owns_schema"], status
        assert status["applied"] == ["0001_initial"] and status["pending"] == []


async def test_a_batch_ships_seals_and_can_be_shipped_again_without_duplicating(stores):
    from nexus.engagement.ledger import shipper

    await _apply_all()
    await _outbox("message.sent", {"subject": "Hi", "body": "Hi Jane Buyer at Acme"},
                  {"message_id": "m-1", "contact_id": "c-1"}, "ev-1")
    result = await shipper.ship()
    assert result["shipped"] == 1

    async with __import__("nexus.engagement.ledger.stores", fromlist=["x"]).connect(
            "archive") as conn:
        rows = await conn.fetch("SELECT event_id, tenant_id, sealed FROM nexus_ledger.events")
    assert [row["event_id"] for row in rows] == ["ev-1"]
    assert "Jane" not in rows[0]["sealed"]
    opened = shipper.open_document(SECRET, rows[0]["sealed"])
    assert opened["envelope"]["payload"]["body"] == "Hi Jane Buyer at Acme"

    # Shipping again must not duplicate: the row is marked, and the insert ignores a repeat.
    assert (await shipper.ship())["shipped"] == 0
    assert await _count("archive", "SELECT count(*) FROM nexus_ledger.events") == 1


async def test_building_produces_examples_facts_and_a_profile_and_a_replay_keeps_the_labels(stores):
    from nexus.engagement.ledger import builder, datasets, shipper

    await _apply_all()
    await _seed_records()
    await _outbox("message.sent",
                  {"subject": "Quick question", "body": "Hi Jane", "context_pack": "Acme ...",
                   "sent_local_hour": 9, "sent_local_weekday": 1, "step_index": 0},
                  {"message_id": "m-1", "contact_id": "c-1"}, "ev-send")
    await _outbox("reply.received",
                  {"inbound_kind": "human", "response_latency_s": 3600, "local_hour": 10,
                   "local_weekday": 1, "body": "Interesting"},
                  {"message_id": "m-2", "answered_message_id": "m-1",
                   "contact_id": "c-1"}, "ev-reply")
    await _outbox("reply.classified",
                  {"category": "interested", "confidence": 0.9, "body": "Interesting"},
                  {"message_id": "m-2", "answered_message_id": "m-1",
                   "contact_id": "c-1"}, "ev-class")
    await shipper.ship()

    built = await builder.build()
    assert built["built"] == 3

    example_id = datasets.example_id("sft_outreach_email", "m-1")
    async with __import__("nexus.engagement.ledger.stores", fromlist=["x"]).connect(
            "training") as conn:
        example = await conn.fetchrow(
            "SELECT split, record, consent_terms_version FROM nexus_ledger.examples "
            "WHERE example_id = $1", example_id)
        events = await conn.fetchval("SELECT count(*) FROM nexus_ledger.events")
    import json

    record = json.loads(example["record"])
    assert record["labels"] == {"replied": True, "positive": True, "meeting": False,
                                "response_latency_s": 3600}
    assert example["split"] in ("train", "val", "test")
    assert events == 3

    async with __import__("nexus.engagement.ledger.stores", fromlist=["x"]).connect(
            "insights") as conn:
        facts = await conn.fetch("SELECT fact_type, category FROM nexus_ledger.facts "
                                 "ORDER BY fact_type")
        profile = await conn.fetchrow("SELECT * FROM nexus_ledger.person_profiles")
    assert [(f["fact_type"], f["category"]) for f in facts] == [("reply", "interested"),
                                                                ("send", "")]
    assert profile["sends"] == 1 and profile["replies"] == 1
    assert profile["last_reply_band"] == "within_hour"
    assert profile["workspace_count"] == 1

    # Replaying the whole archive (what a scrubber fix does) must not un-learn the reply label.
    async with __import__("nexus.engagement.ledger.stores", fromlist=["x"]).connect(
            "training") as conn:
        await conn.execute("DELETE FROM nexus_ledger.build_state")
    await builder.build()
    async with __import__("nexus.engagement.ledger.stores", fromlist=["x"]).connect(
            "training") as conn:
        again = json.loads(await conn.fetchval(
            "SELECT record FROM nexus_ledger.examples WHERE example_id = $1", example_id))
    assert again["labels"]["replied"] is True and again["labels"]["positive"] is True


async def test_a_workspace_that_opts_out_is_removed_from_every_store(stores):
    from nexus.engagement.ledger import builder, deletion, shipper

    await _apply_all()
    await _outbox("message.sent", {"subject": "Hi", "body": "Hi"}, {"message_id": "m-1"}, "ev-1")
    await shipper.ship()
    await builder.build()

    report = await deletion.delete_workspace(TENANT)
    assert report["remaining"] == {"outbox": 0, "archive": 0, "training": 0, "insights": 0}, report
    assert await _count("archive", "SELECT count(*) FROM nexus_ledger.events") == 0
    assert await _count("training", "SELECT count(*) FROM nexus_ledger.examples") == 0
    # Idempotent: asking twice reports zero remaining, not an error.
    assert (await deletion.delete_workspace(TENANT))["remaining"]["archive"] == 0


async def test_erasing_a_person_takes_their_rows_and_leaves_the_company_profile_honest(stores):
    from nexus.engagement.ledger import builder, deletion, shipper
    from nexus.engagement.ledger.pseudonym import person_key
    from nexus.engagement.ledger.stores import connect

    await _apply_all()
    await _outbox("message.sent", {"subject": "Hi", "body": "Hi"},
                  {"message_id": "m-1", "contact_id": "c-1"}, "ev-1")
    await shipper.ship()
    # The shipper resolves against the app database, which holds no contacts here, so the fact is
    # written by hand with the same key the erasure will search for.
    key = person_key(SECRET, "jane.buyer@acme.io")
    async with connect("insights") as conn:
        await conn.execute(
            "INSERT INTO nexus_ledger.facts (event_id, fact_type, person_email, person_key, "
            " company_domain, workspace_key, occurred_at) "
            "VALUES ('f-1', 'send', 'jane.buyer@acme.io', $1, 'acme.io', 'w-1', now())", key)
        await conn.execute(
            "INSERT INTO nexus_ledger.facts (event_id, fact_type, person_email, person_key, "
            " company_domain, workspace_key, occurred_at) "
            "VALUES ('f-2', 'send', 'other@acme.io', 'other-key', 'acme.io', 'w-1', now())")
    async with connect("insights") as conn:
        await builder.refresh_person(conn, "jane.buyer@acme.io")
        await builder.refresh_company(conn, "acme.io")
        assert await conn.fetchval(
            "SELECT sends FROM nexus_ledger.company_profiles WHERE company_domain = 'acme.io'") == 2

    report = await deletion.erase_person(key)
    assert report["remaining"]["insights"] == 0, report
    async with connect("insights") as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM nexus_ledger.facts WHERE person_key = $1", key) == 0
        # The colleague's fact stays, and the company profile now counts only what is left.
        assert await conn.fetchval(
            "SELECT sends FROM nexus_ledger.company_profiles WHERE company_domain = 'acme.io'") == 1


async def test_a_store_that_refuses_leaves_the_rows_to_retry_with_backoff(stores):
    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker
    from nexus.engagement.ledger import shipper
    from nexus.engagement.ledger.stores import StoreUnavailable
    from nexus.models.ledger import LedgerOutbox
    from nexus.providers import resolver
    from sqlalchemy import select

    await _apply_all()
    await _outbox("message.sent", {"subject": "Hi", "body": "Hi"}, {"message_id": "m-1"}, "ev-1")
    # Point the archive at a database that does not exist: a real refusal, not a patched function.
    get_settings().ledger_archive_dsn = _store_dsn("nexus_ledger_absent_test")
    resolver.invalidate()
    with pytest.raises(StoreUnavailable):
        await shipper.ship_batch()

    async with get_platform_sessionmaker()() as session:
        row = (await session.execute(select(LedgerOutbox))).scalars().one()
    assert row.shipped_archive_at is None and row.attempts == 1 and row.last_error
    assert shipper.is_due(row.attempts, row.updated_at, row.updated_at + timedelta(minutes=1)) \
        is False
```

- [ ] **Step 2: Run it against a real Postgres**

```bash
docker run -d --name nexus-ledger-pg -e POSTGRES_PASSWORD=pw -p 55433:5432 postgres:16-alpine
NEXUS_TEST_POSTGRES_URL=postgresql+asyncpg://postgres:pw@localhost:55433/postgres \
  pytest tests_integration/test_ledger_stores_pg.py -n0 -q
```

Expected: `6 passed`. (`docker rm -f nexus-ledger-pg` afterwards.)

- [ ] **Step 3: Write the live suite and the export script**

`tests_live/engagement/test_ledger_live.py`:

```python
"""The three ledger stores, against the REAL Supabase projects (spec §18.2, §18.3, D21).

What only a live run can prove: that the connection strings in Provider keys reach a database, that
the role there owns `nexus_ledger` and can apply the schema, that Supabase's pooler tolerates the way
we connect (`statement_cache_size=0`), and that a round trip — insert, read back, delete — works
through it. The offline and integration suites prove everything else.

Skipped, loudly and by name, unless the DSNs are in the environment:

    NEXUS_LEDGER_ARCHIVE_DSN, NEXUS_LEDGER_TRAINING_DSN, NEXUS_LEDGER_INSIGHTS_DSN,
    NEXUS_LEDGER_PSEUDONYM_SECRET

These are the same values the Control plane stores; the environment is the documented fallback, so a
live run needs no database. **Nothing here writes a real event**: the round trip uses one synthetic
row with an id that names it, and deletes it again.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests_live.engagement.conftest import require_env

pytestmark = [pytest.mark.asyncio]

STORES = ("archive", "training", "insights")
PROBE_ID = "live-probe-0000000000000000"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """Point the app at the live stores, or skip naming exactly what is missing."""
    from nexus.core.config import get_settings
    from nexus.providers import resolver

    values = require_env("NEXUS_LEDGER_ARCHIVE_DSN", "NEXUS_LEDGER_TRAINING_DSN",
                         "NEXUS_LEDGER_INSIGHTS_DSN", "NEXUS_LEDGER_PSEUDONYM_SECRET")
    settings = get_settings()
    monkeypatch.setattr(settings, "ledger_archive_dsn", values["NEXUS_LEDGER_ARCHIVE_DSN"])
    monkeypatch.setattr(settings, "ledger_training_dsn", values["NEXUS_LEDGER_TRAINING_DSN"])
    monkeypatch.setattr(settings, "ledger_insights_dsn", values["NEXUS_LEDGER_INSIGHTS_DSN"])
    monkeypatch.setattr(settings, "ledger_pseudonym_secret",
                        values["NEXUS_LEDGER_PSEUDONYM_SECRET"])
    resolver.invalidate()
    return values


async def test_every_store_is_reachable_and_owned_by_its_own_role():
    """The role must own `nexus_ledger` — that is what lets the Control plane apply the schema, and
    `check_store_dsn` refuses a superuser, so a project's `postgres` user will fail here."""
    from nexus.engagement.ledger import stores

    for store in STORES:
        report = await stores.status(store)
        assert report["reachable"], f"{store}: {report['detail']}"
        assert report["owns_schema"], f"{store}: {report['detail']}"


async def test_the_schema_is_applied_and_applying_it_again_changes_nothing():
    from nexus.engagement.ledger import schema
    from nexus.engagement.ledger.stores import connect

    for store in STORES:
        await schema.apply_schema(store)
        assert await schema.apply_schema(store) == [], f"{store} re-applied a version"
        async with connect(store) as conn:
            applied = await schema.applied_versions(conn)
        assert applied == {version for version, _sql in schema.migrations(store)}


async def test_a_sealed_event_round_trips_through_the_pooler(_configured):
    """One synthetic row, read back through a second connection, then deleted.

    The second connection is the point: Supabase's pooler may hand it a different server
    connection, which is why the app disables asyncpg's statement cache."""
    from nexus.engagement.ledger import envelope, schema, shipper
    from nexus.engagement.ledger.stores import connect

    await schema.apply_schema("archive")
    secret = _configured["NEXUS_LEDGER_PSEUDONYM_SECRET"]
    body = envelope.build(event_id=PROBE_ID, event_type="audit.action", tenant_id="live-probe",
                          occurred_at=NOW, payload={"action": "ledger.live_probe"})
    row = shipper.archive_row(secret, body, {"person_emails": []})
    try:
        async with connect("archive") as conn:
            await conn.execute(
                "INSERT INTO nexus_ledger.events "
                "(event_id, event_type, schema_version, occurred_at, tenant_id, sealed, "
                " person_keys) VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "ON CONFLICT (event_id) DO NOTHING", *row)
        async with connect("archive") as conn:
            sealed = await conn.fetchval(
                "SELECT sealed FROM nexus_ledger.events WHERE event_id = $1", PROBE_ID)
        assert sealed, "the probe row was not readable on a second connection"
        opened = shipper.open_document(secret, sealed)
        assert opened["envelope"]["payload"]["action"] == "ledger.live_probe"
    finally:
        async with connect("archive") as conn:
            await conn.execute("DELETE FROM nexus_ledger.events WHERE event_id = $1", PROBE_ID)
        async with connect("archive") as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM nexus_ledger.events WHERE event_id = $1", PROBE_ID) == 0
```

`scripts/export_training_dataset.py`:

```python
#!/usr/bin/env python
"""Export one training dataset to JSONL or Parquet (spec §18.4).

    python scripts/export_training_dataset.py --dataset sft_outreach_email --split train
    python scripts/export_training_dataset.py --dataset cls_reply --format parquet --out data/

Reads the TRAINING store only, which holds no names, addresses, phone numbers, links or company
names — identities are HMAC keys and free text has been through the scrubber. Every row carries the
``scrubber_version`` and ``consent_terms_version`` it was built under, so an export can be filtered
if either changes.

JSONL is written in the chat format fine-tuning APIs take (``{"messages": [...]}``) for the SFT
datasets, and one JSON object per row for the others. Parquet needs the optional ``export`` extra
(``pip install -e ".[export]"``), because pyarrow is 40 MB and no server needs it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

DATASETS = ("sft_outreach_email", "pref_outreach_email", "cls_reply", "sft_reply_response",
            "tab_engagement", "ai_calls")
SPLITS = ("train", "val", "test")


async def rows(dataset: str, split: str, limit: int, min_scrubber: int) -> list[dict]:
    from nexus.engagement.ledger.stores import connect

    query = ["SELECT example_id, dataset, created_at, split, workspace_key, schema_version, "
             "       scrubber_version, consent_terms_version, quality, record "
             "FROM nexus_ledger.examples WHERE dataset = $1 AND scrubber_version >= $2"]
    params: list = [dataset, min_scrubber]
    if split:
        query.append("AND split = $3")
        params.append(split)
    query.append("ORDER BY created_at, example_id")
    if limit:
        query.append(f"LIMIT {int(limit)}")
    async with connect("training") as conn:
        found = await conn.fetch(" ".join(query), *params)
    return [dict(row) for row in found]


def to_record(row: dict) -> dict:
    record = row["record"]
    if isinstance(record, str):
        record = json.loads(record)
    quality = row["quality"]
    if isinstance(quality, str):
        quality = json.loads(quality)
    out = dict(record)
    out["_meta"] = {
        "example_id": row["example_id"], "split": row["split"],
        "workspace_key": row["workspace_key"], "created_at": str(row["created_at"]),
        "schema_version": row["schema_version"], "scrubber_version": row["scrubber_version"],
        "consent_terms_version": row["consent_terms_version"], "quality": quality,
    }
    return out


def write_jsonl(path: pathlib.Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_parquet(path: pathlib.Path, records: list[dict]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:  # pragma: no cover - depends on an optional extra
        raise SystemExit('Parquet needs the optional extra: pip install -e ".[export]"') from None

    # Records are nested and ragged, so the columns are the JSON of each top-level key. A schema
    # guessed per file would differ between exports of the same dataset, which is worse than one
    # honest string column.
    columns = sorted({key for record in records for key in record})
    table = pa.table({
        column: [json.dumps(record.get(column), ensure_ascii=False) if isinstance(
            record.get(column), (dict, list)) else record.get(column) for record in records]
        for column in columns
    })
    pq.write_table(table, path)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True, choices=DATASETS)
    parser.add_argument("--split", default="", choices=("", *SPLITS),
                        help="train, val or test; omit for every split")
    parser.add_argument("--format", default="jsonl", choices=("jsonl", "parquet"))
    parser.add_argument("--out", default="exports", help="directory to write into")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-scrubber", type=int, default=1,
                        help="skip rows built by an older scrubber than this")
    args = parser.parse_args()

    found = await rows(args.dataset, args.split, args.limit, args.min_scrubber)
    if not found:
        print("no rows matched")
        return 1
    records = [to_record(row) for row in found]
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{args.dataset}{'-' + args.split if args.split else ''}.{args.format}"
    path = out_dir / name
    if args.format == "jsonl":
        write_jsonl(path, records)
    else:
        write_parquet(path, records)
    versions = sorted({r["_meta"]["consent_terms_version"] for r in records})
    print(f"wrote {len(records)} rows to {path}")
    print(f"consent terms in this export: {', '.join(v or '(none recorded)' for v in versions)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

In `pyproject.toml`, add the optional extra after `migrate`:

```toml
# Parquet for `scripts/export_training_dataset.py`. Optional because pyarrow is ~40 MB and no
# server needs it — JSONL export works without it.
export = ["pyarrow>=16.0"]
```

In `.github/workflows/ci.yml`, add the four store secrets to the `live-engagement` job's `env`:

```yaml
      # The three ledger stores. Absent, the ledger live tests skip naming these.
      NEXUS_LEDGER_ARCHIVE_DSN: ${{ secrets.NEXUS_LEDGER_ARCHIVE_DSN }}
      NEXUS_LEDGER_TRAINING_DSN: ${{ secrets.NEXUS_LEDGER_TRAINING_DSN }}
      NEXUS_LEDGER_INSIGHTS_DSN: ${{ secrets.NEXUS_LEDGER_INSIGHTS_DSN }}
      NEXUS_LEDGER_PSEUDONYM_SECRET: ${{ secrets.NEXUS_LEDGER_PSEUDONYM_SECRET }}
```

- [ ] **Step 4: Write the owner's guide**

`docs/engagement/setup-supabase.md`:

````markdown
# The three ledger stores (Supabase)

The training & insights ledger writes to **three separate Postgres databases** (spec §18.3, D27):

| Store | Holds | Who reads it |
|---|---|---|
| `archive` | every event as written, sealed | the rebuild jobs only |
| `training` | pseudonymised events and dataset tables | data scientists, through the export script |
| `insights` | identified engagement facts and profiles | the app, for the badges in §18.5 |

They are separate so a training extract can be handed to someone without also handing them the
archive, and so an insights outage cannot stop collection. This guide provisions them on Supabase.

Everything here is done **once, by the owner**. Claude never handles these credentials: the
connection strings are typed into the Control plane (Provider keys), where they are sealed, and they
appear in no API response, log line or audit row.

---

## 1. Three projects

Create three Supabase projects (or use the three already created for this):

| Store | Project ref |
|---|---|
| archive | `grnxfnagoxhdiqyscyfi` |
| training | `maqwelpushxsjdntkklz` |
| insights | `lrmzazesarmqtitpoyge` |

Any region is fine; put them all in the one closest to the app so shipping is not a transatlantic
round trip per batch.

## 2. A role per store, owning one schema and nothing else

In each project, open **SQL Editor** and run this, with your own password:

```sql
-- One role per store. It owns the ledger schema and has no rights anywhere else in the database.
create role nexus_ledger login password 'A-LONG-RANDOM-PASSWORD';
create schema if not exists nexus_ledger authorization nexus_ledger;

-- Nothing else in this database is reachable from this role.
revoke all on schema public from nexus_ledger;
revoke all on all tables in schema public from nexus_ledger;
alter role nexus_ledger set search_path = nexus_ledger;
```

Why a dedicated role rather than the project's `postgres` user:

- The app's own `check_store_dsn` **refuses** a superuser, a `BYPASSRLS` role, and the names
  `postgres` and `supabase_admin`. A connection string that can do anything is one leaked secret away
  from being able to do anything.
- The schema is `authorization nexus_ledger`, so the role **owns** it — that is what lets the Control
  plane apply the versioned schema, and it is what the Ledger tab reports as *owns nexus_ledger*.

Use a different password per project. Generate them with a password manager; they are typed once.

## 3. The connection string

Supabase gives two kinds of connection string. Use the **session pooler** one:

```
postgresql://nexus_ledger.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

- The username is `nexus_ledger.<project-ref>` — the pooler needs the project ref in the user name.
- Port **5432** is the session pooler; 6543 is the transaction pooler. Either works (the app sets
  `statement_cache_size=0` precisely because the pooler may run in transaction mode), but session
  mode is what the schema step needs.
- Direct connections (`db.<ref>.supabase.co`) are IPv6-only on new projects and will fail from an
  IPv4-only host.

## 4. Store the three strings and the pseudonymisation secret

In the app: **Control plane → Provider keys**, and add one key for each id:

| Provider key id | Value |
|---|---|
| `ledger_archive` | the archive project's connection string |
| `ledger_training` | the training project's connection string |
| `ledger_insights` | the insights project's connection string |
| `ledger_pseudonym` | a random secret, at least 32 characters, at least 16 distinct |

The pseudonymisation secret is what turns a person into a stable key and what seals the archive.
**Back it up with the database.** Losing it makes the archive unreadable and breaks every join
between stores — and, because erasure finds rows by that key, it breaks erasure too (spec §16).
Generate it with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Press **Test** on each key. The store keys probe by connecting and checking the role is not a
superuser; the secret is checked for length and variety.

## 5. Apply the schema

**Control plane → Ledger**. Each store shows *not configured*, *unreachable*, *N versions pending*
or *up to date*. Press **Apply schema** on each; it runs the versioned SQL in the repo
(`nexus/engagement/ledger/sql/<store>/`), records what it applied, and is safe to press twice.

Never run those files by hand in the SQL editor: the version table is what tells the shipper the
store is at a version the code understands.

## 6. Check it is collecting

Give it a few minutes with at least one consented workspace using the product, then:

- **Control plane → Platform health**: `ledger stores` is ok and `ledger outbox` says either nothing
  waiting or a small number with a recent oldest age.
- **Control plane → Ledger**: *Workspaces contributing* is not zero, and *Datasets last built* fills
  in within the hour.

A growing outbox with `retrying` above zero means a store is refusing writes — the detail line on the
store card says which and why.

## 7. Costs and retention

- The archive grows fastest: one row per event, sealed. On Supabase's free tier, watch the 500 MB
  database limit; a busy workspace is roughly 1–2 MB a day.
- The app's own `ledger_outbox` keeps rows for **7 days** after they are archived, then deletes them.
- Nothing in these stores is deleted by age. Deletion happens on request: a workspace switching
  training off, or a person erasure — both remove rows from all three stores and report what remains.
````

- [ ] **Step 5: Run the live suite (it should skip, by name)**

Run: `pytest tests_live/engagement -n0 -q -rs`
Expected: every ledger test SKIPS naming `NEXUS_LEDGER_ARCHIVE_DSN, NEXUS_LEDGER_TRAINING_DSN, NEXUS_LEDGER_INSIGHTS_DSN, NEXUS_LEDGER_PSEUDONYM_SECRET`. It passes for real once the owner has followed the guide.

- [ ] **Step 6: Commit**

```bash
git add tests_integration tests_live scripts/export_training_dataset.py docs/engagement/setup-supabase.md pyproject.toml .github/workflows/ci.yml
git commit -m "test(ledger): prove the stores against real Postgres, and against Supabase when keyed"
```

---

### Task 12: The whole test file, and the whole suite

**Files:**
- Test: `tests/test_engagement_ledger_stores.py`

- [ ] **Step 1: Write the complete test file**

`tests/test_engagement_ledger_stores.py` in full — every earlier task took its tests from here:

```python
"""The ledger's three stores: keys, scrubbing, datasets, profiles, shipping, deletion (§18.2-§18.6).

Everything here runs offline. The pure half (keys, scrubber, dataset builders, profile maths, the
shipping schedule) is tested directly; the store I/O is tested against real Postgres in
`tests_integration/test_engagement_ledger_stores.py`, because a fake asyncpg would only prove the
fake agrees with itself.
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from nexus.core.config import get_settings
from tests.conftest import (
    assert_staff_surface_hidden,
    auth,
    make_tenant,
    signup,
    tenant_session,
)

SECRET = "a-test-pseudonymisation-secret-with-enough-variety-0123456789"
NEXUS = pathlib.Path(__file__).resolve().parents[1] / "nexus"
SRC = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"
SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"

NOW = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)


async def _superadmin(client, monkeypatch, *, slug: str, email: str) -> str:
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


def _document(event_type: str, payload: dict, refs: dict | None = None, **resolved) -> dict:
    """An archive document: the envelope as emitted, plus who it was about."""
    from nexus.engagement.ledger import envelope

    body = envelope.build(
        event_id=f"ev-{event_type}", event_type=event_type, tenant_id="t-1", occurred_at=NOW,
        refs=refs or {}, payload=payload,
    )
    base = {"tenant_id": "t-1", "consent_terms_version": "2026-09-17",
            "contact_email": "jane.buyer@acme.io", "contact_name": "Jane Buyer",
            "account_name": "Acme Robotics", "account_domain": "acme.io",
            "sdr_name": "Sam Rep", "sdr_email": "sam@seller.com",
            "person_emails": ["jane.buyer@acme.io"], "other_names": []}
    base.update(resolved)
    return {"envelope": body, "resolved": base}


# ---- keys and splits ------------------------------------------------------------------------------

def test_a_person_is_the_same_key_everywhere_and_a_different_one_per_kind():
    from nexus.engagement.ledger import pseudonym

    first = pseudonym.person_key(SECRET, "Jane.Buyer@Acme.io ")
    assert first == pseudonym.person_key(SECRET, "jane.buyer@acme.io")   # normalised
    assert first != pseudonym.person_key("another-secret-entirely-xxxxxxxxxx", "jane.buyer@acme.io")
    assert first != pseudonym.company_key(SECRET, "jane.buyer@acme.io")  # kind is in the input
    assert len(first) == 32
    with pytest.raises(ValueError):
        pseudonym.key(SECRET, "not-a-kind", "x")
    with pytest.raises(ValueError):
        pseudonym.person_key("", "jane@acme.io")


def test_a_workspace_lands_in_one_split_and_the_mix_is_roughly_80_10_10():
    from collections import Counter

    from nexus.engagement.ledger import pseudonym

    keys = [pseudonym.workspace_key(SECRET, f"tenant-{i}") for i in range(600)]
    splits = [pseudonym.split_for(key) for key in keys]
    assert splits == [pseudonym.split_for(key) for key in keys]      # deterministic
    counts = Counter(splits)
    assert 0.70 < counts["train"] / len(keys) < 0.90
    assert counts["val"] and counts["test"]


def test_the_archive_key_is_derived_from_the_same_secret_and_round_trips():
    from nexus.engagement.ledger import pseudonym

    sealed = pseudonym.archive_fernet(SECRET).encrypt(b'{"hello": "world"}')
    assert b"hello" not in sealed
    assert pseudonym.archive_fernet(SECRET).decrypt(sealed) == b'{"hello": "world"}'


# ---- the scrubber ---------------------------------------------------------------------------------

def test_known_people_companies_addresses_and_numbers_become_stable_placeholders():
    from nexus.engagement.ledger.scrub import Known, Scrubber

    scrubber = Scrubber(Known(people=["Jane Buyer", "Sam Rep"],
                              companies=["Acme Robotics", "acme.io"],
                              emails=["jane.buyer@acme.io"]))
    first = scrubber.text(
        "Hi Jane, Sam here from Acme Robotics — reply to jane.buyer@acme.io or call "
        "+1 (415) 555-0134. See https://acme.io/pricing.")
    assert "Jane" not in first and "Sam" not in first and "Acme" not in first
    assert "jane.buyer@acme.io" not in first and "415" not in first and "acme.io" not in first
    assert "[PERSON_" in first and "[COMPANY_" in first and "[PHONE_" in first
    # The same person is the same placeholder in the next text of the same example.
    second = scrubber.text("Jane Buyer replied.")
    assert second.split()[0] == first.split()[1].rstrip(",")


def test_an_iso_date_survives_but_a_stranger_s_address_does_not():
    from nexus.engagement.ledger.scrub import Known, Scrubber

    scrubber = Scrubber(Known())
    assert scrubber.text("Let's meet on 2026-10-05.") == "Let's meet on 2026-10-05."
    assert "@" not in scrubber.text("Try priya@othercorp.com instead")


def test_the_scrubber_walks_nested_values_and_leaves_non_text_alone():
    from nexus.engagement.ledger.scrub import Known, Scrubber

    scrubber = Scrubber(Known(people=["Jane Buyer"]))
    out = scrubber.value({"thread": [{"body": "Jane Buyer said yes", "at": "2026-09-18"}],
                          "count": 3, "ok": True})
    assert "Jane" not in out["thread"][0]["body"]
    assert out["count"] == 3 and out["ok"] is True


# ---- dataset builders -----------------------------------------------------------------------------

def test_a_sent_message_makes_a_training_example_a_feature_row_and_a_send_fact():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "message.sent",
        {"subject": "Quick question, Jane", "body": "Hi Jane, saw Acme Robotics is hiring.",
         "context_pack": "Account: Acme Robotics ...", "sent_local_hour": 9,
         "sent_local_weekday": 1, "kind": "step", "step_index": 0,
         "persona": {"title": "VP Engineering", "seniority": "vp"},
         "account": {"industry": "robotics", "employee_count": 200, "country": "US"},
         "personalisation_facts": ["hiring 12 engineers"], "mailbox_provider": "google"},
        refs={"message_id": "m-1", "contact_id": "c-1", "account_id": "a-1"},
    ), SECRET)

    kinds = {example.dataset for example in built.examples}
    assert kinds == {"sft_outreach_email", "tab_engagement"}
    sft = next(x for x in built.examples if x.dataset == "sft_outreach_email")
    assistant = sft.record["messages"][-1]["content"]
    assert "Jane" not in assistant and "Acme" not in assistant
    assert sft.record["labels"] == {"replied": False, "positive": False, "meeting": False}
    assert sft.consent_terms_version == "2026-09-17"
    tab = next(x for x in built.examples if x.dataset == "tab_engagement")
    # The facts themselves are free text about a named person; the count is the feature.
    assert tab.record["features"]["personalisation_facts"] == 1
    assert tab.record["features"]["local_hour"] == 9
    assert [f.fact_type for f in built.facts] == ["send"]
    assert built.facts[0].person_email == "jane.buyer@acme.io"
    assert built.facts[0].attrs["message_id"] == "m-1"


def test_the_pseudonymised_event_keeps_no_tenant_no_ids_and_no_names():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "message.sent", {"subject": "Hi", "body": "Hi Jane Buyer"},
        refs={"message_id": "m-1", "contact_id": "c-1", "account_id": "a-1"}), SECRET)
    event = built.event
    assert "tenant_id" not in event["body"]
    assert event["workspace_key"] != "t-1"
    assert event["body"]["refs"]["contact_id"] != "c-1"
    assert event["body"]["refs"]["message_id"] == "m-1"    # internal ids are not identities
    assert "Jane" not in json.dumps(event["body"])
    assert event["person_keys"] == [
        __import__("nexus.engagement.ledger.pseudonym", fromlist=["x"]).person_key(
            SECRET, "jane.buyer@acme.io")]


def test_a_reply_labels_the_message_it_answers_and_records_a_reply_fact():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "reply.received",
        {"inbound_kind": "human", "response_latency_s": 5400, "local_hour": 11,
         "local_weekday": 2, "body": "Sounds interesting"},
        refs={"message_id": "m-2", "answered_message_id": "m-1"}), SECRET)

    patched = {(p.dataset, p.values.get("replied")) for p in built.patches}
    assert patched == {("sft_outreach_email", True), ("tab_engagement", True)}
    assert all(p.example_id == datasets.example_id(p.dataset, "m-1") for p in built.patches)
    assert built.patches[0].values["response_latency_s"] == 5400
    assert [f.fact_type for f in built.facts] == ["reply"]
    assert built.facts[0].response_latency_s == 5400


def test_a_classification_becomes_a_labelled_example_and_marks_the_send_positive():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "reply.classified",
        {"category": "interested", "confidence": 0.91, "body": "Yes, let's talk",
         "thread": [{"direction": "out", "at": "2026-09-17", "body": "Hi Jane"}],
         "label_source": "ai"},
        refs={"message_id": "m-2", "answered_message_id": "m-1"}), SECRET)

    example = next(x for x in built.examples if x.dataset == "cls_reply")
    assert example.record["output"]["category"] == "interested"
    assert example.record["output"]["label_source"] == "ai"
    assert "Jane" not in json.dumps(example.record)
    assert all(p.values["positive"] is True for p in built.patches)
    assert built.fact_categories[0].category == "interested"


def test_an_sdr_correction_replaces_the_label_and_says_who_set_it():
    from nexus.engagement.ledger import datasets

    corrected = datasets.build(_document(
        "reply.corrected", {"ai_category": "interested", "sdr_category": "not_now",
                            "resolved_date": "2026-11-01"},
        refs={"message_id": "m-2", "answered_message_id": "m-1"}), SECRET)
    patch = next(p for p in corrected.patches if p.dataset == "cls_reply")
    assert patch.path == "output"
    assert patch.values == {"category": "not_now", "resolved_date": "2026-11-01",
                            "label_source": "sdr_corrected"}
    assert {p.values.get("positive") for p in corrected.patches if p.dataset != "cls_reply"} == {False}

    confirmed = datasets.build(_document(
        "reply.corrected", {"ai_category": "interested", "sdr_category": "interested"},
        refs={"message_id": "m-2"}), SECRET)
    assert confirmed.patches[0].values["label_source"] == "sdr_confirmed"


def test_an_sdr_edit_becomes_a_preference_pair_and_an_unedited_draft_does_not():
    from nexus.engagement.ledger import datasets

    edited = datasets.build(_document(
        "draft.edited",
        {"context_pack": "Account: Acme ...", "ai_subject": "Hi", "ai_body": "AI wrote this",
         "subject": "Hi", "body": "The SDR rewrote this", "edit_distance": 14},
        refs={"message_id": "m-1"}), SECRET)
    pair = next(x for x in edited.examples if x.dataset == "pref_outreach_email")
    assert "SDR rewrote" in pair.record["chosen"] and "AI wrote" in pair.record["rejected"]

    untouched = datasets.build(_document(
        "draft.edited", {"context_pack": "c", "ai_subject": "Hi", "ai_body": "Same",
                         "subject": "Hi", "body": "Same"}, refs={"message_id": "m-9"}), SECRET)
    assert untouched.examples == []


def test_a_meeting_labels_every_example_the_outcome_is_attributed_to():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "outcome.recorded", {"stage": "meeting", "meta": {"message_id": "m-1"}},
        refs={"contact_id": "c-1"}), SECRET)
    assert {p.dataset for p in built.patches} == {
        "sft_outreach_email", "tab_engagement", "sft_reply_response"}
    assert all(p.values == {"meeting": True} for p in built.patches)

    ignored = datasets.build(_document("outcome.recorded", {"stage": "replied"}), SECRET)
    assert ignored.patches == []


def test_an_ai_call_is_kept_for_distillation_with_its_model_and_cost():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "ai.call", {"agent": "messaging", "status": "ok", "inputs": {"account": "Acme Robotics"},
                    "output": "Hi Jane", "tokens": 812, "latency_ms": 2400}), SECRET)
    example = next(x for x in built.examples if x.dataset == "ai_calls")
    assert example.record["tokens"] == 812
    assert "Jane" not in json.dumps(example.record) and "Acme" not in json.dumps(example.record)


def test_an_event_about_nobody_still_becomes_a_training_event_but_no_fact():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "message.sent", {"subject": "s", "body": "b"}, person_emails=[], contact_email=""), SECRET)
    assert built.event is not None
    assert built.facts == []
    assert built.event["person_keys"] == []


# ---- profiles -------------------------------------------------------------------------------------

def _fact(fact_type: str, *, hour=None, weekday=None, latency=None, at=NOW, workspace="w1",
          attrs=None) -> dict:
    return {"fact_type": fact_type, "person_key": "pk", "company_domain": "acme.io",
            "workspace_key": workspace, "occurred_at": at, "local_hour": hour,
            "local_weekday": weekday, "response_latency_s": latency, "attrs": attrs or {}}


def test_a_profile_is_the_shape_of_the_replies_that_remain():
    from nexus.engagement.ledger import insights

    facts = [
        _fact("send"), _fact("send"), _fact("send"), _fact("send"),
        _fact("reply", hour=9, weekday=1, latency=3000, at=NOW - timedelta(days=3)),
        _fact("reply", hour=9, weekday=1, latency=9000, at=NOW - timedelta(days=2),
              workspace="w2"),
        _fact("out_of_office", at=NOW - timedelta(days=1), attrs={"until": "2026-09-25"}),
    ]
    profile = insights.person_profile("jane.buyer@acme.io", facts)
    assert profile["best_hour"] == 9 and profile["best_weekday"] == 1
    assert profile["median_response_s"] == 6000
    assert profile["reply_propensity"] == 0.5
    assert profile["sends"] == 4 and profile["replies"] == 2
    assert profile["workspace_count"] == 2
    assert profile["last_reply_band"] == "same_day"
    assert profile["ooo_periods"] == [{"from": "2026-09-17", "until": "2026-09-25"}]
    assert insights.person_profile("jane.buyer@acme.io", []) is None


def test_the_speed_band_is_the_only_pattern_below_the_threshold():
    from nexus.engagement.ledger import insights

    assert insights.speed_band(600) == "within_hour"
    assert insights.speed_band(40_000) == "same_day"
    assert insights.speed_band(400_000) == "within_week"
    assert insights.speed_band(4_000_000) == "longer"
    assert insights.speed_band(None) == ""


# ---- shipping -------------------------------------------------------------------------------------

def test_a_failed_row_waits_longer_each_time_up_to_an_hour():
    from nexus.engagement.ledger import shipper

    assert [shipper.retry_delay_minutes(n) for n in (0, 1, 2, 3, 10)] == [0, 2, 4, 8, 60]
    assert shipper.is_due(0, NOW, NOW) is True
    assert shipper.is_due(3, NOW - timedelta(minutes=5), NOW) is False
    assert shipper.is_due(3, NOW - timedelta(minutes=9), NOW) is True


def test_an_archive_row_is_sealed_carries_its_person_keys_and_opens_again():
    from nexus.engagement.ledger import envelope, shipper
    from nexus.engagement.ledger.pseudonym import person_key

    body = envelope.build(event_id="ev-1", event_type="message.sent", tenant_id="t-1",
                          occurred_at=NOW, payload={"body": "Hi Jane"})
    resolved = {"contact_email": "jane.buyer@acme.io", "person_emails": ["jane.buyer@acme.io"]}
    row = shipper.archive_row(SECRET, body, resolved)
    event_id, event_type, schema_version, occurred_at, tenant_id, sealed, keys = row
    assert (event_id, event_type, schema_version, tenant_id) == ("ev-1", "message.sent", 1, "t-1")
    assert occurred_at == NOW
    assert "Jane" not in sealed and "jane.buyer" not in sealed
    assert keys == [person_key(SECRET, "jane.buyer@acme.io")]
    opened = shipper.open_document(SECRET, sealed)
    assert opened["envelope"]["payload"]["body"] == "Hi Jane"
    assert opened["resolved"]["contact_email"] == "jane.buyer@acme.io"


async def test_shipping_without_a_secret_records_nothing_and_says_so(monkeypatch):
    from nexus.engagement.ledger import shipper
    from nexus.providers import resolver

    monkeypatch.setattr(get_settings(), "ledger_pseudonym_secret", "")
    resolver.invalidate()
    result = await shipper.ship()
    assert result["shipped"] == 0 and "secret" in result["skipped"]


async def test_the_backlog_reports_what_is_waiting_and_how_old_it_is():
    from nexus.engagement.ledger import consent
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.ledger.shipper import backlog

    tid = await make_tenant(slug="ledgerq", name="Ledger Q")
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
        await emit(ts, "account.scored", refs={"account_id": "a-1"}, payload={"composite": 80})
    waiting = await backlog()
    assert waiting["waiting"] >= 1 and waiting["retrying"] == 0


async def test_retention_only_removes_rows_the_archive_has_acknowledged():
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.engagement.ledger import consent, shipper
    from nexus.engagement.ledger.emit import emit
    from nexus.models.ledger import LedgerOutbox

    tid = await make_tenant(slug="ledgerret", name="Ledger Ret")
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
        await emit(ts, "account.scored", refs={"account_id": "a-1"}, payload={})
        await emit(ts, "account.scored", refs={"account_id": "a-2"}, payload={})
    async with get_platform_sessionmaker()() as session:
        rows = (await session.execute(
            select(LedgerOutbox).where(LedgerOutbox.tenant_id == tid))).scalars().all()
        rows[0].shipped_archive_at = utcnow() - timedelta(days=shipper.RETENTION_DAYS + 1)
        await session.commit()

    assert await shipper.purge_shipped() == 1
    async with get_platform_sessionmaker()() as session:
        left = (await session.execute(
            select(LedgerOutbox).where(LedgerOutbox.tenant_id == tid))).scalars().all()
    assert len(left) == 1 and left[0].shipped_archive_at is None


# ---- stores and schema ----------------------------------------------------------------------------

def test_every_store_has_versioned_sql_that_creates_its_schema():
    from nexus.engagement.ledger import schema, stores

    for store in stores.STORES:
        versions = schema.migrations(store)
        assert versions, f"{store} has no SQL"
        names = [version for version, _sql in versions]
        assert names == sorted(names)
        first = versions[0][1]
        assert "nexus_ledger.schema_migrations" in first
        assert "IF NOT EXISTS" in first, "applying a version twice must be safe"


async def test_an_unconfigured_store_is_reported_not_crashed(monkeypatch):
    from nexus.engagement.ledger import stores
    from nexus.providers import resolver

    for field in ("ledger_archive_dsn", "ledger_training_dsn", "ledger_insights_dsn"):
        monkeypatch.setattr(get_settings(), field, "")
    resolver.invalidate()
    report = await stores.status("archive")
    assert report["configured"] is False and report["detail"] == "not configured"
    assert report["pending"] == ["0001_initial"]
    with pytest.raises(ValueError):
        async with stores.connect("nope"):
            pass


async def test_a_store_dsn_pointing_at_the_metadata_service_is_refused(monkeypatch):
    """The SSRF guard the source-database subsystem owns, applied to operator-typed DSNs."""
    from nexus.engagement.ledger import stores
    from nexus.providers import resolver
    from nexus.sources.safety import SourceRejected

    monkeypatch.setattr(get_settings(), "env", "prod")
    monkeypatch.setattr(get_settings(), "source_db_allow_private", False)
    monkeypatch.setattr(get_settings(), "ledger_archive_dsn",
                        "postgresql://u:p@169.254.169.254:5432/postgres")
    resolver.invalidate()
    with pytest.raises(SourceRejected):
        async with stores.connect("archive"):
            pass


# ---- the jobs -------------------------------------------------------------------------------------

async def test_the_four_ledger_jobs_are_registered_and_do_nothing_without_a_store(monkeypatch):
    from nexus.providers import resolver
    from nexus.workers.tasks import HANDLERS

    for name in ("ship_ledger", "build_ledger_datasets", "ledger_delete_workspace",
                 "ledger_erase_person"):
        assert name in HANDLERS
    monkeypatch.setattr(get_settings(), "ledger_pseudonym_secret", "")
    resolver.invalidate()
    assert "skipped" in await HANDLERS["ship_ledger"]({})
    assert await HANDLERS["ledger_delete_workspace"]({}) == {"error": "no tenant"}
    assert await HANDLERS["ledger_erase_person"]({}) == {"error": "no person key"}


async def test_the_scheduler_drives_shipping_and_building_whatever_automation_says(monkeypatch):
    from nexus.workers.queue import InMemoryTaskQueue
    from nexus.workers.scheduler import _enqueue_due

    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    queue = InMemoryTaskQueue()
    await _enqueue_due(queue)
    names = set()
    while True:
        job = await queue.dequeue(timeout=0)
        if job is None:
            break
        names.add(job.name)
    assert {"ship_ledger", "build_ledger_datasets"} <= names


async def test_switching_training_off_queues_the_deletion_of_what_was_already_shipped(client):
    from nexus.workers.queue import InMemoryTaskQueue, set_task_queue

    queue = InMemoryTaskQueue()
    set_task_queue(queue)
    try:
        token = await signup(client, slug="ledgeroff", email="owner@ledgeroff.com",
                             company="Ledger Off")
        r = await client.put("/api/engagement/settings/training", json={"status": "off"},
                             headers=auth(token))
        assert r.status_code == 200, r.text
        jobs = []
        while True:
            job = await queue.dequeue(timeout=0)
            if job is None:
                break
            jobs.append(job)
        assert any(j.name == "ledger_delete_workspace" for j in jobs)
    finally:
        set_task_queue(None)


async def test_erasing_a_shared_person_reaches_the_ledger(monkeypatch):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.people.store import forget_person, resolve_person_record
    from nexus.providers import resolver
    from nexus.workers.queue import InMemoryTaskQueue, set_task_queue

    monkeypatch.setattr(get_settings(), "ledger_pseudonym_secret", SECRET)
    resolver.invalidate()
    queue = InMemoryTaskQueue()
    set_task_queue(queue)
    try:
        async with get_platform_sessionmaker()() as session:
            person = await resolve_person_record(session, email="jane.buyer@acme.io",
                                                 full_name="Jane Buyer")
            await session.commit()
            assert await forget_person(session, person.id) is True
            await session.commit()
        job = await queue.dequeue(timeout=0)
        assert job is not None and job.name == "ledger_erase_person"
        # The address itself is never in the payload: a dead-lettered job logs what it carries.
        assert "jane" not in json.dumps(job.payload).lower()
    finally:
        set_task_queue(None)


# ---- the admin surface ----------------------------------------------------------------------------

async def test_the_ledger_tab_is_platform_only_and_never_shows_a_connection_string(
    client, monkeypatch
):
    member = await signup(client, slug="ledgerui", email="rep@ledgerui.com", company="Ledger UI")
    # 404, not 403: the staff surface must not be enumerable (see `assert_staff_surface_hidden`).
    assert_staff_surface_hidden(await client.get("/api/admin/ledger", headers=auth(member)))

    token = await _superadmin(client, monkeypatch, slug="ledgeradm",
                              email="boss@ledgeradm.com")
    monkeypatch.setattr(get_settings(), "ledger_archive_dsn",
                        "postgresql://nexus_ledger:hunter2@db.example.com:5432/postgres")
    from nexus.providers import resolver

    resolver.invalidate()
    r = await client.get("/api/admin/ledger", headers=auth(token))
    assert r.status_code == 200, r.text
    assert "hunter2" not in r.text and "db.example.com" not in r.text
    body = r.json()
    assert [s["store"] for s in body["stores"]] == ["archive", "training", "insights"]
    assert body["outbox"]["waiting"] >= 0
    assert body["consented_workspaces"] >= 0


async def test_applying_a_schema_to_an_unconfigured_store_is_a_409(client, monkeypatch):
    from nexus.providers import resolver

    token = await _superadmin(client, monkeypatch, slug="ledgersch",
                              email="boss@ledgersch.com")
    monkeypatch.setattr(get_settings(), "ledger_training_dsn", "")
    resolver.invalidate()
    r = await client.post("/api/admin/ledger/training/schema", headers=auth(token))
    assert r.status_code == 409, r.text
    assert (await client.post("/api/admin/ledger/nope/schema",
                              headers=auth(token))).status_code == 404


async def test_erasing_a_person_queues_the_key_and_needs_the_secret(client, monkeypatch):
    from nexus.providers import resolver
    from nexus.workers.queue import InMemoryTaskQueue, set_task_queue

    token = await _superadmin(client, monkeypatch, slug="ledgererase",
                              email="boss@ledgererase.com")
    monkeypatch.setattr(get_settings(), "ledger_pseudonym_secret", "")
    resolver.invalidate()
    r = await client.post("/api/admin/ledger/erase-person",
                          json={"email": "jane.buyer@acme.io"}, headers=auth(token))
    assert r.status_code == 409, r.text

    monkeypatch.setattr(get_settings(), "ledger_pseudonym_secret", SECRET)
    resolver.invalidate()
    queue = InMemoryTaskQueue()
    set_task_queue(queue)
    try:
        r = await client.post("/api/admin/ledger/erase-person",
                              json={"email": "jane.buyer@acme.io"}, headers=auth(token))
        assert r.status_code == 200, r.text
        from nexus.engagement.ledger.pseudonym import person_key

        assert r.json()["person_key"] == person_key(SECRET, "jane.buyer@acme.io")
        job = await queue.dequeue(timeout=0)
        assert job.name == "ledger_erase_person"
    finally:
        set_task_queue(None)


# ---- structure ------------------------------------------------------------------------------------

def test_the_ledger_tab_shows_state_and_the_two_actions_and_no_dsn():
    tab = (SRC / "pages/admin/LedgerTab.tsx").read_text(encoding="utf-8")
    assert "api.ledgerStatus" in tab
    assert "api.applyLedgerSchema" in tab and "api.eraseLedgerPerson" in tab
    for word in ("Waiting to ship", "contributing", "Datasets last built"):
        assert word in tab
    assert "dsn" not in tab.lower()
    host = (SRC / "pages/AdminBillingPage.tsx").read_text(encoding="utf-8")
    assert '{ value: "ledger", label: "Ledger" }' in host
    assert "<LedgerTab />" in host


def test_the_export_script_reads_only_the_training_store():
    script = (SCRIPTS / "export_training_dataset.py").read_text(encoding="utf-8")
    assert 'connect("training")' in script
    assert 'connect("archive")' not in script and 'connect("insights")' not in script
    assert "consent_terms_version" in script and "scrubber_version" in script


def test_the_setup_guide_names_the_role_the_schema_and_the_four_keys():
    guide = (pathlib.Path(__file__).resolve().parents[1]
             / "docs/engagement/setup-supabase.md").read_text(encoding="utf-8")
    for needle in ("create role nexus_ledger", "authorization nexus_ledger", "ledger_archive",
                   "ledger_training", "ledger_insights", "ledger_pseudonym"):
        assert needle in guide
```

- [ ] **Step 2: Run the phase**

Run: `pytest tests/test_engagement_ledger_stores.py -n0 -q`
Expected: `35 passed`.

- [ ] **Step 3: Run everything**

Run: `pytest -q` then `ruff check nexus tests tests_live` then `cd frontend && npm run typecheck`

RUN_RESULT (2026-09-18, CI image `python:3.11`, phases 01–06 applied cumulatively):

```
3618 passed in 5483.57s (1:31:23)
All checks passed!          # ruff check nexus tests tests_live
tsc --noEmit -p tsconfig.json   # clean
```

Integration (real Postgres 16, `NEXUS_TEST_POSTGRES_URL` set): `6 passed`.
Live (`tests_live/engagement`): every ledger test skips naming its four missing secrets.

- [ ] **Step 4: Commit**

```bash
git add tests/test_engagement_ledger_stores.py
git commit -m "test(ledger): keys, scrubbing, datasets, profiles, shipping, jobs and the tab"
```

---

## What later phases depend on

- **`payloads.py` is the contract.** Phases 07–10 build `message.sent`, `draft.edited`, `reply.received`, `reply.classified`, `reply.corrected`, `response.sent` and `outcome.recorded` payloads to those shapes; a field they omit costs an example's context, never a failed build.
- **`refs.answered_message_id` on every reply event** is what labels the outreach example a reply belongs to. Without it the reply is still recorded and still becomes a fact — the send simply never learns it was answered.
- **`outcome.recorded` carries `meta.message_id`** (phase 12), which is how a meeting reaches the example that earned it.
- **Phase 13 reads the insights store** through a client of its own, applying the display rules in D26 — three or more workspaces for a pattern, otherwise only the last reply's speed band.
