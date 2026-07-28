# Billing Milestone 4 — Rate Cards, Credits & Invoices — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn recorded usage into money — a configurable credit rate card, an append-only credit ledger, deterministic period rating, and immutable invoices — with the ≥50% gross-margin floor enforced as a *validation rule*, not a guideline.

**Architecture:** Rate cards price each capability in credits (1 credit = $0.01). Cost rates hold per-unit COGS. Rating reads period rollups, applies included quota → overage → credit burn, and emits invoice lines. Invoices are derived state: re-rating a closed period must reproduce identical lines. Nothing charges a card in this milestone (PSP is M5) — this is the money *math*, fully offline-testable.

**Tech Stack:** Python 3.11, async SQLAlchemy 2.0, `Decimal`/integer-cents arithmetic (never floats for money), Alembic, pytest offline.

**Run tests with `PYTEST_XDIST_WORKER=m4 py -3.10 -m pytest`.**

**Prerequisites:** M1–M3 merged (catalog, plans, entitlements, subscriptions, usage events, rollups, `current_usage`).

**Design refs:** [04-Pricing-Engine](../../billing/04-Pricing-Engine.md) ·
[11-Profitability-Analysis](../../billing/11-Profitability-Analysis.md) ·
[12-Cost-Analysis](../../billing/12-Cost-Analysis.md) ·
[13-Pricing-Recommendations](../../billing/13-Pricing-Recommendations.md) §2

**Non-breaking guarantee:** 4 new tables (migration 0023), one new module tree. Existing tables
untouched; no existing endpoint or job modified except append-only worker registration.

---

## File structure

**Create:** `nexus/billing/rates.py` (rate-card + cost-rate seed & lookup), `nexus/billing/credits.py`, `nexus/billing/rating.py`, `nexus/billing/invoicing.py`, `migrations/versions/0023_billing_money.py`, tests `test_billing_rates.py`, `test_billing_credits.py`, `test_billing_rating.py`
**Modify (append-only):** `nexus/models/billing.py` (+4 models), `nexus/models/__init__.py`, `nexus/workers/tasks.py` (period-close handler)

---

## Task 1: Money models

**Files:** Modify `nexus/models/billing.py`, `nexus/models/__init__.py`; Test: `tests/test_billing_rates.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_rates.py
from __future__ import annotations


def test_money_models_registered():
    import nexus.models as m

    for n in ("BillingRateCard", "BillingCostRate", "BillingCreditLedger",
              "BillingInvoice", "BillingInvoiceLine"):
        assert hasattr(m, n), f"{n} not exported"


async def test_rate_card_and_cost_rate_round_trip():
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingCostRate, BillingRateCard

    async with get_sessionmaker()() as s:
        s.add(BillingRateCard(capability_id="ai.email_draft", credits_per_unit=2,
                              tiers=[{"upto": 10000, "credits": 2},
                                     {"upto": None, "credits": 1}]))
        s.add(BillingCostRate(capability_id="ai.email_draft", unit_cost_usd=0.0012,
                              source="groq llama-3.3-70b"))
        await s.commit()

    async with get_sessionmaker()() as s:
        rc = await s.get(BillingRateCard, "ai.email_draft")
        assert rc.credits_per_unit == 2 and rc.tiers[1]["credits"] == 1
        cr = await s.get(BillingCostRate, "ai.email_draft")
        assert float(cr.unit_cost_usd) == 0.0012
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTEST_XDIST_WORKER=m4 py -3.10 -m pytest tests/test_billing_rates.py -v`
Expected: FAIL — `AssertionError: BillingRateCard not exported`

- [ ] **Step 3: Append the models to `nexus/models/billing.py`**

```python
# ---- money: rate cards, costs, credits, invoices ---------------------------------------------
class BillingRateCard(TimestampMixin, Base):
    """What one unit of a capability COSTS THE CUSTOMER, in credits (1 credit = $0.01 list).

    Platform-global config, edited in Admin. ``tiers`` is an ordered volume ladder:
    ``[{"upto": 10000, "credits": 2}, {"upto": null, "credits": 1}]`` — the last entry with a
    null ``upto`` is the catch-all. Empty tiers ⇒ flat ``credits_per_unit``.
    """

    __tablename__ = "billing_rate_cards"

    capability_id: Mapped[str] = mapped_column(
        ForeignKey("billing_capabilities.id", ondelete="CASCADE"), primary_key=True
    )
    credits_per_unit: Mapped[float] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    tiers: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Set when finance deliberately ships a line below the margin floor; surfaces on dashboards.
    margin_exception: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    margin_exception_reason: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class BillingCostRate(TimestampMixin, Base):
    """What one unit of a capability COSTS US (COGS). Drives margin validation and reporting.

    Versioned by ``updated_at``; usage events stamp the cost at write time so historical margin
    is immune to later repricing (docs/billing/12-Cost-Analysis.md).
    """

    __tablename__ = "billing_cost_rates"

    capability_id: Mapped[str] = mapped_column(
        ForeignKey("billing_capabilities.id", ondelete="CASCADE"), primary_key=True
    )
    unit_cost_usd: Mapped[float] = mapped_column(Numeric(12, 8), default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(120), default="")   # provider//model provenance
    note: Mapped[str] = mapped_column(Text, default="")


class BillingCreditLedger(IdMixin, TimestampMixin, TenantScoped, Base):
    """Append-only credit movements. Balance is SUM(delta) — never a mutable counter.

    An append-only ledger is the only representation that survives concurrent grants/burns and
    stays auditable when a customer disputes a charge.
    """

    __tablename__ = "billing_credit_ledger"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_credit_idempotency"),
        Index("ix_credit_tenant_time", "tenant_id", "created_at"),
    )

    # Positive = granted/purchased, negative = burned.
    delta: Mapped[float] = mapped_column(Numeric(14, 4), nullable=False)
    kind: Mapped[str] = mapped_column(String(20))   # grant|purchase|burn|expiry|adjustment
    reason: Mapped[str] = mapped_column(Text, default="")
    capability_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    period_key: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    actor: Mapped[str] = mapped_column(String(120), default="system")


class BillingInvoice(IdMixin, TimestampMixin, TenantScoped, Base):
    """A rated billing period. Immutable once finalized — corrections are credit notes."""

    __tablename__ = "billing_invoices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "period_key", name="uq_invoice_period"),
        Index("ix_invoice_tenant_status", "tenant_id", "status"),
    )

    number: Mapped[str] = mapped_column(String(40), default="")
    period_key: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|finalized|paid|void
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    plan_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class BillingInvoiceLine(IdMixin, TimestampMixin, TenantScoped, Base):
    """One charge on an invoice. Always traceable to a capability + period."""

    __tablename__ = "billing_invoice_lines"
    __table_args__ = (Index("ix_invoice_line_invoice", "invoice_id"),)

    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("billing_invoices.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))   # base|seat|overage|credit_pack|discount
    capability_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    description: Mapped[str] = mapped_column(String(300), default="")
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), default=0, nullable=False)
    unit_credits: Mapped[float] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
```

Ensure `Text` and `Boolean` are in the sqlalchemy import list (they already are) and `Numeric`
(added in M2).

- [ ] **Step 4: Register in `nexus/models/__init__.py`** (append the 5 names to the existing
`from nexus.models.billing import (...)` block and to `__all__`).

- [ ] **Step 5: Run to verify it passes**

Run: `PYTEST_XDIST_WORKER=m4 py -3.10 -m pytest tests/test_billing_rates.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add nexus/models/billing.py nexus/models/__init__.py tests/test_billing_rates.py
git commit -m "feat(billing): rate card, cost rate, credit ledger, invoice models"
```

---

## Task 2: Migration 0023

**Files:** Create `migrations/versions/0023_billing_money.py`; Test: append to `tests/test_billing_migration.py`

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_billing_migration.py  (append)
def test_money_migration_creates_tables():
    import importlib
    import inspect

    import nexus.models  # noqa: F401
    from nexus.core.db import Base

    mod = importlib.import_module("migrations.versions.0023_billing_money")
    assert mod.revision == "0023_billing_money"
    assert mod.down_revision == "0022_billing_usage"
    src = inspect.getsource(mod.upgrade)
    for t in ("billing_rate_cards", "billing_cost_rates", "billing_credit_ledger",
              "billing_invoices", "billing_invoice_lines"):
        assert t in Base.metadata.tables
        assert f'"{t}"' in src or f"'{t}'" in src
    down = inspect.getsource(mod.downgrade)
    assert down.index("billing_invoice_lines") < down.index("billing_invoices")
```

- [ ] **Step 2: Run to verify it fails.** Expected: module not found.

- [ ] **Step 3: Write the migration**

```python
# migrations/versions/0023_billing_money.py
"""Billing money layer: rate cards, cost rates, credit ledger, invoices.

Additive only — safe on a live database.

Revision ID: 0023_billing_money
Revises: 0022_billing_usage
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_billing_money"
down_revision = "0022_billing_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_rate_cards",
        sa.Column("capability_id", sa.String(length=80),
                  sa.ForeignKey("billing_capabilities.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("credits_per_unit", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("tiers", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("margin_exception", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("margin_exception_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "billing_cost_rates",
        sa.Column("capability_id", sa.String(length=80),
                  sa.ForeignKey("billing_capabilities.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("unit_cost_usd", sa.Numeric(12, 8), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "billing_credit_ledger",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("delta", sa.Numeric(14, 4), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("capability_id", sa.String(length=80), nullable=True),
        sa.Column("period_key", sa.String(length=40), nullable=True, index=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_credit_idempotency"),
    )
    op.create_index("ix_credit_tenant_time", "billing_credit_ledger", ["tenant_id", "created_at"])
    op.create_table(
        "billing_invoices",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("number", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("period_key", sa.String(length=40), nullable=False, index=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("subtotal_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("plan_id", sa.String(length=60), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "period_key", name="uq_invoice_period"),
    )
    op.create_index("ix_invoice_tenant_status", "billing_invoices", ["tenant_id", "status"])
    op.create_table(
        "billing_invoice_lines",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("invoice_id", sa.String(length=32),
                  sa.ForeignKey("billing_invoices.id", ondelete="CASCADE"), nullable=False,
                  index=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("capability_id", sa.String(length=80), nullable=True),
        sa.Column("description", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("unit_credits", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("amount_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_invoice_line_invoice", "billing_invoice_lines", ["invoice_id"])


def downgrade() -> None:
    op.drop_table("billing_invoice_lines")
    op.drop_table("billing_invoices")
    op.drop_table("billing_credit_ledger")
    op.drop_table("billing_cost_rates")
    op.drop_table("billing_rate_cards")
```

- [ ] **Step 4: Run** `PYTEST_XDIST_WORKER=m4 py -3.10 -m pytest tests/test_billing_migration.py -v` → PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/0023_billing_money.py tests/test_billing_migration.py
git commit -m "feat(billing): migration 0023 (rate cards, credits, invoices)"
```

---

## Task 3: Rate-card + cost-rate seed with margin validation

**Files:** Create `nexus/billing/rates.py`; Test: append to `tests/test_billing_rates.py`

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_billing_rates.py  (append)
async def test_seed_rates_creates_cards_and_costs():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import RATE_SEED, sync_rates
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingCostRate, BillingRateCard
    from sqlalchemy import func, select

    await sync_catalog()
    res = await sync_rates()
    assert res["rate_cards"] == len(RATE_SEED)
    assert (await sync_rates())["rate_cards"] == 0        # idempotent

    async with get_sessionmaker()() as s:
        assert await s.scalar(select(func.count()).select_from(BillingRateCard)) == len(RATE_SEED)
        assert await s.scalar(select(func.count()).select_from(BillingCostRate)) > 0


def test_every_seeded_rate_clears_the_margin_floor():
    """The 50% gross-margin floor is a property of the seed, verified in CI — not a wish."""
    from nexus.billing.rates import RATE_SEED, gross_margin

    for r in RATE_SEED:
        m = gross_margin(r["credits_per_unit"], r["unit_cost_usd"])
        assert m >= 0.50, f"{r['capability_id']} margin {m:.2%} below the 50% floor"


def test_gross_margin_math():
    from nexus.billing.rates import gross_margin

    # 2 credits = $0.02 revenue, $0.0012 cost -> 94%
    assert round(gross_margin(2, 0.0012), 2) == 0.94
    assert gross_margin(0, 0.01) == 0.0           # free capability -> no margin
    assert gross_margin(5, 0) == 1.0              # zero COGS -> 100%


async def test_validate_rate_rejects_below_floor():
    from nexus.billing.rates import MarginFloorError, validate_rate

    # 1 credit ($0.01) against $0.008 COGS = 20% -> must be refused
    try:
        validate_rate("ai.account_qa", credits_per_unit=1, unit_cost_usd=0.008)
    except MarginFloorError as exc:
        assert "50" in str(exc) or "margin" in str(exc).lower()
        return
    raise AssertionError("validate_rate must reject a below-floor rate")


async def test_validate_rate_allows_explicit_exception():
    from nexus.billing.rates import validate_rate

    # Finance may override, but must say so explicitly — it becomes visible on the dashboard.
    validate_rate("ai.account_qa", credits_per_unit=1, unit_cost_usd=0.008,
                  margin_exception=True)
```

- [ ] **Step 2: Run to verify it fails.** Expected: `ModuleNotFoundError: nexus.billing.rates`

- [ ] **Step 3: Implement**

```python
# nexus/billing/rates.py
"""Rate cards (what we charge) and cost rates (what it costs us), plus the margin guardrail.

The ≥50% gross-margin floor from docs/billing/11-Profitability-Analysis.md is enforced HERE, as
a validation rule: a rate that would ship underwater is refused unless finance records an
explicit exception. That makes the margin target structural rather than aspirational.

Prices are in credits (1 credit = $0.01 list). Costs are USD per unit, sourced from
docs/billing/12-Cost-Analysis.md §2.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from nexus.core.db import get_sessionmaker
from nexus.models.billing import BillingCostRate, BillingRateCard

logger = logging.getLogger("nexus.billing.rates")

CREDIT_USD = 0.01
MIN_GROSS_MARGIN = 0.50


class MarginFloorError(ValueError):
    """Raised when a rate would ship below the gross-margin floor without an exception."""


def gross_margin(credits_per_unit: float, unit_cost_usd: float) -> float:
    """(revenue - cost) / revenue for one unit. 0.0 when the capability is free."""
    revenue = float(credits_per_unit) * CREDIT_USD
    if revenue <= 0:
        return 0.0
    return max(0.0, (revenue - float(unit_cost_usd)) / revenue)


def validate_rate(
    capability_id: str, *, credits_per_unit: float, unit_cost_usd: float,
    margin_exception: bool = False,
) -> float:
    """Return the margin, or raise if it is below floor without an explicit exception."""
    margin = gross_margin(credits_per_unit, unit_cost_usd)
    if margin < MIN_GROSS_MARGIN and not margin_exception:
        raise MarginFloorError(
            f"{capability_id}: {margin:.1%} gross margin is below the "
            f"{MIN_GROSS_MARGIN:.0%} floor (price {credits_per_unit} credits vs "
            f"${unit_cost_usd} cost). Reprice, or record a margin exception."
        )
    return margin


def _r(capability_id: str, credits: float, cost: float, source: str = "",
       tiers: list | None = None) -> dict:
    return {
        "capability_id": capability_id, "credits_per_unit": credits,
        "unit_cost_usd": cost, "source": source, "tiers": tiers or [],
    }


# Launch rate card (docs/billing/13-Pricing-Recommendations.md §2). Every line clears 50%.
RATE_SEED: list[dict] = [
    _r("ai.email_draft", 2, 0.0012, "groq llama-3.3-70b"),
    _r("outreach.cadence_touch", 2, 0.0013, "groq + smtp"),
    _r("ai.account_qa", 3, 0.012, "groq + 2 web searches"),
    _r("ai.research_brief", 3, 0.012, "exa research + groq"),
    _r("ai.call_script", 2, 0.0016, "groq"),
    _r("ai.contact_rank", 1, 0.0009, "groq"),
    _r("ai.chat_turn", 1, 0.0010, "groq budgeted envelope"),
    _r("ai.icp_from_website", 5, 0.010, "crawl + groq"),
    _r("ai.personalization_fetch", 8, 0.030, "apify actor"),
    _r("discovery.account_added", 5, 0.015, "exa pool + enrich amortized"),
    _r("discovery.lookalike_company", 25, 0.10, "search + enrich + llm"),
    _r("discovery.lookalike_contact", 2, 0.0005, "in-workspace scoring"),
    _r("enrich.account", 3, 0.010, "crawl + llm"),
    _r("enrich.contact", 4, 0.012, "search + finder + verify"),
    _r("enrich.source_committee", 15, 0.05, "search + llm + verifies"),
    _r("enrich.linkedin_finder", 2, 0.004, "search"),
    _r("verify.email", 0.25, 0.0002, "reacher self-hosted"),
    _r("outreach.email_send", 1, 0.0001, "customer smtp"),
    _r("outreach.email_draft_save", 1, 0.0001, "customer imap"),
    _r("outreach.sep_push", 0.5, 0.0001, "customer sep account"),
    _r("integration.crm_sync", 0.5, 0.0001, "customer crm account"),
    _r("network.source_sync", 2, 0.002, "google/microsoft graph"),
    _r("network.search", 0.5, 0.0002, "indexed sql"),
    _r("network.linkedin_import", 5, 0.0005, "csv parse"),
    _r("calling.brief", 2, 0.001, "assembled dossier"),
    _r("calling.minutes", 4, 0.014, "twilio"),
    _r("notify.webhook", 0.1, 0.0001, "http post"),
    _r("notify.slack", 0.1, 0.0001, "http post"),
    _r("notify.email_digest", 0.5, 0.0001, "smtp"),
    _r("data.export", 5, 0.001, "compute"),
    _r("data.import_csv", 2, 0.0005, "compute"),
    _r("workflow.orchestration_run", 5, 0.005, "multi-step tools"),
    _r("automation.play_run", 1, 0.0002, "compute"),
    _r("platform.storage", 25, 0.10, "postgres gb-month"),
    _r("search.web", 1, 0.004, "exa/brave/serper blended"),
    _r("signal.news_scan", 1, 0.004, "search"),
]


async def sync_rates() -> dict:
    """Seed rate cards + cost rates for capabilities that have none. Never overwrites an
    existing rate: once live, pricing is owned by Admin, not by a redeploy."""
    created_rc = created_cr = 0
    async with get_sessionmaker()() as session:
        have_rc = {r.capability_id for r in (await session.scalars(select(BillingRateCard))).all()}
        have_cr = {r.capability_id for r in (await session.scalars(select(BillingCostRate))).all()}
        for spec in RATE_SEED:
            cid = spec["capability_id"]
            # Guardrail runs on the seed itself, so a bad price can never reach the database.
            validate_rate(
                cid, credits_per_unit=spec["credits_per_unit"],
                unit_cost_usd=spec["unit_cost_usd"],
            )
            if cid not in have_rc:
                session.add(BillingRateCard(
                    capability_id=cid, credits_per_unit=spec["credits_per_unit"],
                    tiers=spec["tiers"],
                ))
                created_rc += 1
            if cid not in have_cr:
                session.add(BillingCostRate(
                    capability_id=cid, unit_cost_usd=spec["unit_cost_usd"],
                    source=spec["source"],
                ))
                created_cr += 1
        await session.commit()
    if created_rc or created_cr:
        logger.info("rate sync: %d cards, %d cost rates created", created_rc, created_cr)
    return {"rate_cards": created_rc, "cost_rates": created_cr}
```

- [ ] **Step 4: Run** → PASS (7 tests). **If `test_every_seeded_rate_clears_the_margin_floor`
fails, do NOT lower the floor — fix the offending price in `RATE_SEED` and report it.**

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/rates.py tests/test_billing_rates.py
git commit -m "feat(billing): rate card seed + enforced 50% margin floor"
```

---

## Task 4: Credit ledger

**Files:** Create `nexus/billing/credits.py`; Test: `tests/test_billing_credits.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_credits.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def test_grant_and_balance():
    from nexus.billing.credits import balance, grant_credits

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 2000, kind="grant", reason="monthly plan grant",
                            idempotency_key="grant:2026-07")
        assert await balance(ts) == 2000


async def test_grant_is_idempotent():
    from nexus.billing.credits import balance, grant_credits

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        for _ in range(3):
            await grant_credits(ts, 500, kind="grant", reason="monthly",
                                idempotency_key="grant:2026-07")
        assert await balance(ts) == 500          # the monthly grant lands exactly once


async def test_burn_reduces_balance_and_can_go_negative_only_when_allowed():
    from nexus.billing.credits import balance, burn_credits, grant_credits

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 100, kind="grant", reason="x", idempotency_key="g1")
        ok = await burn_credits(ts, 30, reason="ai.email_draft overage",
                                idempotency_key="b1", capability_id="ai.email_draft")
        assert ok is True and await balance(ts) == 70

        # Insufficient balance without allow_negative -> refused, ledger unchanged.
        ok = await burn_credits(ts, 500, reason="too much", idempotency_key="b2")
        assert ok is False and await balance(ts) == 70

        # Overage billing explicitly permits going negative (invoiced later).
        ok = await burn_credits(ts, 500, reason="overage", idempotency_key="b3",
                                allow_negative=True)
        assert ok is True and await balance(ts) == -430


async def test_ledger_is_append_only_history():
    from nexus.billing.credits import burn_credits, grant_credits, history
    from nexus.models.billing import BillingCreditLedger

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 100, kind="grant", reason="a", idempotency_key="g")
        await burn_credits(ts, 40, reason="b", idempotency_key="b")
        rows = await ts.list(BillingCreditLedger)
        assert len(rows) == 2                       # nothing mutated, both movements kept
        h = await history(ts, limit=10)
        assert len(h) == 2
```

- [ ] **Step 2: Run to verify it fails.** Expected: `ModuleNotFoundError: nexus.billing.credits`

- [ ] **Step 3: Implement**

```python
# nexus/billing/credits.py
"""Credit ledger: append-only movements, balance = SUM(delta).

Never a mutable counter. An append-only ledger is the only shape that stays correct under
concurrency and remains auditable when a customer disputes a charge
(docs/billing/04-Pricing-Engine.md §1).
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select

from nexus.core.tenancy import TenantSession
from nexus.models.billing import BillingCreditLedger

logger = logging.getLogger("nexus.billing.credits")


async def balance(ts: TenantSession) -> float:
    """Current credit balance for the tenant."""
    total = await ts.session.scalar(
        select(func.coalesce(func.sum(BillingCreditLedger.delta), 0)).where(
            BillingCreditLedger.tenant_id == ts.tenant_id
        )
    )
    return float(total or 0)


async def _append(
    ts: TenantSession, delta: float, *, kind: str, reason: str, idempotency_key: str,
    capability_id: str | None = None, period_key: str | None = None,
    expires_at: datetime | None = None, actor: str = "system",
) -> bool:
    """Append one movement unless the idempotency key was already used."""
    dup = (
        await ts.session.scalars(
            ts.select(
                BillingCreditLedger,
                BillingCreditLedger.idempotency_key == idempotency_key,
            ).limit(1)
        )
    ).first()
    if dup is not None:
        return False
    ts.add(
        BillingCreditLedger(
            delta=delta, kind=kind, reason=reason, capability_id=capability_id,
            period_key=period_key, expires_at=expires_at,
            idempotency_key=idempotency_key, actor=actor,
        )
    )
    await ts.flush()
    return True


async def grant_credits(
    ts: TenantSession, amount: float, *, kind: str = "grant", reason: str = "",
    idempotency_key: str, expires_at: datetime | None = None, actor: str = "system",
    period_key: str | None = None,
) -> bool:
    """Add credits (monthly grant, purchased pack, promo, manual adjustment)."""
    if amount <= 0:
        return False
    return await _append(
        ts, float(amount), kind=kind, reason=reason, idempotency_key=idempotency_key,
        expires_at=expires_at, actor=actor, period_key=period_key,
    )


async def burn_credits(
    ts: TenantSession, amount: float, *, reason: str = "", idempotency_key: str,
    capability_id: str | None = None, period_key: str | None = None,
    allow_negative: bool = False, actor: str = "system",
) -> bool:
    """Spend credits. Refuses to overdraw unless ``allow_negative`` (overage billing).

    Returns False when refused or when the key was already applied — the caller never needs to
    distinguish, because both mean "no new charge was made".
    """
    if amount <= 0:
        return False
    if not allow_negative and await balance(ts) < amount:
        return False
    return await _append(
        ts, -float(amount), kind="burn", reason=reason, idempotency_key=idempotency_key,
        capability_id=capability_id, period_key=period_key, actor=actor,
    )


async def history(ts: TenantSession, *, limit: int = 100) -> list[BillingCreditLedger]:
    """Most recent movements first — powers the customer's credit history view."""
    return list(
        (
            await ts.session.scalars(
                ts.select(BillingCreditLedger)
                .order_by(BillingCreditLedger.created_at.desc())
                .limit(limit)
            )
        ).all()
    )
```

- [ ] **Step 4: Run** `PYTEST_XDIST_WORKER=m4 py -3.10 -m pytest tests/test_billing_credits.py -v` → PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/credits.py tests/test_billing_credits.py
git commit -m "feat(billing): append-only credit ledger"
```

---

## Task 5: Period rating → invoice

**Files:** Create `nexus/billing/rating.py`; Test: `tests/test_billing_rating.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_rating.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def _setup(plan_id="growth"):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        await ts.flush()
    return tid


async def _use(ts, cap, qty, *, key):
    from nexus.billing.usage import record_usage

    await record_usage(ts, capability_id=cap, quantity=qty, idempotency_key=key)


async def test_rate_period_charges_base_fee_only_when_no_overage():
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import rebuild_rollups
    from nexus.billing.rollups import period_key
    from nexus.core.db import utcnow

    tid = await _setup("growth")
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await _use(ts, "verify.email", 10, key="v1")      # far under the 5000 quota
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=key)
        assert inv.status == "draft"
        kinds = {ln.kind for ln in await _lines(ts, inv)}
        assert "base" in kinds
        assert "overage" not in kinds
        assert inv.total_cents == 7900                      # Growth base fee only


async def _lines(ts, inv):
    from nexus.models.billing import BillingInvoiceLine

    return [ln for ln in await ts.list(BillingInvoiceLine) if ln.invoice_id == inv.id]


async def test_rate_period_charges_overage_beyond_quota():
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    tid = await _setup("free")     # Free: ai.email_draft quota 20
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await _use(ts, "ai.email_draft", 30, key="d1")     # 10 over
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=key)
        over = [ln for ln in await _lines(ts, inv) if ln.kind == "overage"]
        assert len(over) == 1
        assert float(over[0].quantity) == 10
        # 10 units x 2 credits x $0.01 = $0.20 = 20 cents
        assert over[0].amount_cents == 20


async def test_rating_is_deterministic_and_replayable():
    """Re-rating a period must reproduce identical lines — the audit guarantee."""
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    tid = await _setup("free")
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await _use(ts, "ai.email_draft", 25, key="d1")
        await rebuild_rollups(ts)
        first = await rate_period(ts, period_key=key)
        first_total = first.total_cents
        first_lines = sorted((ln.kind, float(ln.quantity), ln.amount_cents)
                             for ln in await _lines(ts, first))

        second = await rate_period(ts, period_key=key)   # re-rate the same period
        assert second.id == first.id                      # upserted, not duplicated
        assert second.total_cents == first_total
        second_lines = sorted((ln.kind, float(ln.quantity), ln.amount_cents)
                              for ln in await _lines(ts, second))
        assert second_lines == first_lines


async def test_unlimited_plan_is_never_charged_overage():
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    tid = await _setup("legacy-unlimited")
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await _use(ts, "ai.email_draft", 50_000, key="huge")
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=key)
        assert inv.total_cents == 0                       # $0 plan, no overage, ever
        assert [ln for ln in await _lines(ts, inv) if ln.kind == "overage"] == []


async def test_finalize_makes_invoice_immutable():
    from nexus.billing.rating import finalize_invoice, rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    tid = await _setup("growth")
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=key)
        finalized = await finalize_invoice(ts, inv.id)
        assert finalized.status == "finalized"
        assert finalized.number.startswith("INV-")
        assert finalized.finalized_at is not None

        # Re-rating a finalized period must NOT silently rewrite history.
        again = await rate_period(ts, period_key=key)
        assert again.status == "finalized"
        assert again.total_cents == finalized.total_cents
```

- [ ] **Step 2: Run to verify it fails.** Expected: `ModuleNotFoundError: nexus.billing.rating`

- [ ] **Step 3: Implement**

```python
# nexus/billing/rating.py
"""Rating: turn a period's rollups into invoice lines.

Deterministic and replayable by construction — it reads only rollups + config, so re-rating a
period always reproduces identical lines (docs/billing/04-Pricing-Engine.md §2). That property is
what makes an invoice defensible in a dispute.

Money is integer cents everywhere. Credits are the intermediate unit (1 credit = $0.01).
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.billing import (
    BillingInvoice,
    BillingInvoiceLine,
    BillingPlan,
    BillingPlanEntitlement,
    BillingRateCard,
    BillingSubscription,
    BillingUsageRollup,
)

logger = logging.getLogger("nexus.billing.rating")

CREDIT_CENTS = 1  # 1 credit = $0.01 = 1 cent

# Plan classes that are never charged usage overage.
_NO_OVERAGE_CLASSES = {"unlimited", "internal", "partner"}


def tiered_credits(units: float, card: BillingRateCard) -> float:
    """Credits for ``units`` under the card's volume ladder (flat rate when no tiers)."""
    tiers = list(card.tiers or [])
    if not tiers:
        return float(units) * float(card.credits_per_unit)
    total = 0.0
    remaining = float(units)
    consumed = 0.0
    for tier in tiers:
        upto = tier.get("upto")
        price = float(tier.get("credits", card.credits_per_unit))
        if upto is None:
            total += remaining * price
            remaining = 0.0
            break
        span = max(0.0, float(upto) - consumed)
        take = min(remaining, span)
        total += take * price
        remaining -= take
        consumed += take
        if remaining <= 0:
            break
    if remaining > 0:  # ladder didn't cover everything; charge the base rate for the tail
        total += remaining * float(card.credits_per_unit)
    return total


async def rate_period(ts: TenantSession, *, period_key: str) -> BillingInvoice:
    """Rate one billing period into a draft invoice. Idempotent (upserts by period).

    A finalized invoice is returned untouched: history is never silently rewritten.
    """
    invoice = (
        await ts.session.scalars(
            ts.select(BillingInvoice, BillingInvoice.period_key == period_key).limit(1)
        )
    ).first()
    if invoice is not None and invoice.status in ("finalized", "paid", "void"):
        return invoice

    subs = await ts.list(BillingSubscription, limit=5)
    sub = next((s for s in subs if s.status in ("trialing", "active", "past_due")), None)
    plan = await ts.session.get(BillingPlan, sub.plan_id) if sub else None

    if invoice is None:
        invoice = BillingInvoice(period_key=period_key, status="draft",
                                 plan_id=sub.plan_id if sub else None)
        ts.add(invoice)
        await ts.flush()
    else:
        # Rebuild lines from scratch so re-rating is a pure function of current data.
        for old in [ln for ln in await ts.list(BillingInvoiceLine)
                    if ln.invoice_id == invoice.id]:
            await ts.delete(old)
        await ts.flush()

    lines: list[BillingInvoiceLine] = []

    # 1. Base subscription fee.
    if plan is not None and plan.base_price_cents:
        lines.append(BillingInvoiceLine(
            invoice_id=invoice.id, kind="base", description=f"{plan.name} plan",
            quantity=1, unit_credits=0, amount_cents=plan.base_price_cents,
        ))

    # 2. Usage overage, per capability.
    charge_overage = plan is not None and plan.plan_class not in _NO_OVERAGE_CLASSES
    if charge_overage and sub is not None:
        ents = {
            e.capability_id: e
            for e in (
                await ts.session.scalars(
                    select(BillingPlanEntitlement).where(
                        BillingPlanEntitlement.plan_id == sub.plan_id
                    )
                )
            ).all()
        }
        cards = {
            c.capability_id: c
            for c in (await ts.session.scalars(select(BillingRateCard))).all()
        }
        rollups = [
            r for r in await ts.list(BillingUsageRollup)
            if r.period_kind == "period" and r.period_key == period_key
        ]
        for r in sorted(rollups, key=lambda x: x.capability_id):
            ent = ents.get(r.capability_id)
            quota = ent.quota if ent is not None else None
            if quota is None:
                continue                      # unlimited/unpriced -> nothing to charge
            over = float(r.quantity) - float(quota)
            if over <= 0:
                continue
            card = cards.get(r.capability_id)
            if card is None or not card.active:
                continue
            credits = tiered_credits(over, card)
            amount = int(round(credits * CREDIT_CENTS))
            if amount <= 0:
                continue
            lines.append(BillingInvoiceLine(
                invoice_id=invoice.id, kind="overage", capability_id=r.capability_id,
                description=f"{r.capability_id} overage ({over:g} over {quota})",
                quantity=over, unit_credits=card.credits_per_unit, amount_cents=amount,
            ))

    for ln in lines:
        ts.add(ln)
    await ts.flush()

    subtotal = sum(ln.amount_cents for ln in lines)
    invoice.subtotal_cents = subtotal
    invoice.total_cents = subtotal
    invoice.currency = plan.currency if plan else "USD"
    await ts.flush()
    return invoice


async def finalize_invoice(ts: TenantSession, invoice_id: str) -> BillingInvoice:
    """Freeze an invoice and assign its number. Idempotent."""
    inv = await ts.get(BillingInvoice, invoice_id)
    if inv is None:
        raise ValueError(f"invoice {invoice_id} not found")
    if inv.status != "draft":
        return inv
    now = utcnow()
    inv.status = "finalized"
    inv.finalized_at = now
    inv.number = f"INV-{now:%Y%m}-{inv.id[:8].upper()}"
    await ts.flush()
    return inv
```

- [ ] **Step 4: Run** `PYTEST_XDIST_WORKER=m4 py -3.10 -m pytest tests/test_billing_rating.py -v` → PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/rating.py tests/test_billing_rating.py
git commit -m "feat(billing): deterministic period rating + invoice finalization"
```

---

## Task 6: Seed rates on startup

**Files:** Modify `nexus/main.py`

- [ ] **Step 1:** In `nexus/main.py`'s lifespan, inside the existing billing seed `try` block,
add `sync_rates()` after `sync_plans()`:

```python
            from nexus.billing.rates import sync_rates

            await sync_rates()
```

- [ ] **Step 2: Run** `PYTEST_XDIST_WORKER=m4 py -3.10 -m pytest tests/test_api.py -q` → PASS

- [ ] **Step 3: Commit**

```bash
git add nexus/main.py
git commit -m "feat(billing): seed rate cards on startup"
```

---

## Task 7: Gate

- [ ] `PYTEST_XDIST_WORKER=m4 py -3.10 -m pytest tests/ -k billing -q` → all pass
- [ ] `py -3.10 -m ruff check nexus/billing nexus/models/billing.py` → All checks passed
- [ ] Orchestrator runs the full suite.

---

## Self-review

**Spec coverage:** rate cards + volume tiers + credits unit ([04](../../billing/04-Pricing-Engine.md) §1) → T1/T3/T5;
margin floor as validation ([04](../../billing/04-Pricing-Engine.md) §5, [11](../../billing/11-Profitability-Analysis.md)) → T3;
credit ledger + burn order primitives ([04](../../billing/04-Pricing-Engine.md) §2) → T4;
deterministic replayable rating + immutable invoices ([04](../../billing/04-Pricing-Engine.md) §2,
[16](../../billing/16-Testing-Strategy.md) §2) → T5; cost rates for margin reporting
([12](../../billing/12-Cost-Analysis.md)) → T1/T3.
Deferred: coupons/price books/regional currency (admin CRUD, M5), PSP collection + dunning (M5),
seat-day proration (M5), credit expiry sweep (M5).

**Placeholder scan:** none — all steps ship complete code.

**Type consistency:** `gross_margin`/`validate_rate`/`sync_rates` (T3) used in T3 tests and T6.
`grant_credits`/`burn_credits`/`balance`/`history` (T4) — keyword-only `idempotency_key`
consistently. `rate_period(ts, period_key=)` and `finalize_invoice(ts, invoice_id)` (T5) match the
test calls. `period_key()` imported from `nexus.billing.rollups` (M3). Model names in T1 match
T2's migration table names and T5's imports.
