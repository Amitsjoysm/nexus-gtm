# Billing Milestone 5 — Metering the Real Application — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the billing platform actually observe the product. Until now `check_and_meter()`
exists but nothing calls it. This milestone adds the ergonomic seam, captures **real** COGS from
the LLM chokepoint, and wires the highest-value spend paths — with enforcement still `shadow`, so
nothing can block a customer.

**Architecture — the split that matters:**

- **Revenue** is metered at the *semantic* boundary (the endpoint that means "a draft was
  written"), because that is what a customer understands and what a plan prices.
- **COGS** is reported by the *infrastructure* layer (the LLM provider) into a context-local
  accumulator, and stamped onto that same semantic event.

Metering at the LLM layer instead would double-count (one endpoint can make several model calls)
and would bill a unit no customer recognizes. Estimating cost instead of measuring it would make
every margin dashboard fiction. Keeping both, joined by a context, gives one row per billable
action carrying its true cost.

**Tech Stack:** Python 3.11, `contextvars`, async SQLAlchemy 2.0, FastAPI, pytest offline.

**Run tests with `PYTEST_XDIST_WORKER=m5 py -3.10 -m pytest`.**

**Prerequisites:** M1–M4 merged.

**Design refs:** [03-Metering-Architecture](../../billing/03-Metering-Architecture.md) ·
[09-Billable-Resources](../../billing/09-Billable-Resources.md) ·
[10-Usage-Tracking](../../billing/10-Usage-Tracking.md) ·
[12-Cost-Analysis](../../billing/12-Cost-Analysis.md)

**Non-breaking guarantee:** no schema change. Enforcement default stays `shadow`, so every
`metered()` call allows. A failure inside the seam degrades to allow (inherited from
`check_and_meter`). Every wired endpoint keeps its existing response shape.

---

## File structure

**Create:** `nexus/billing/context.py`, `nexus/billing/meter.py`, tests
`test_billing_cost_context.py`, `test_billing_wiring.py`
**Modify:** `nexus/agents/llm.py` (wrap the provider in `get_llm_provider`), `nexus/core/config.py`
(cost-rate settings), `nexus/api/routers/agents.py`, `nexus/api/routers/chat.py`,
`nexus/api/routers/contacts.py`, `nexus/api/main` error handler for `QuotaExceeded`

---

## Task 1: Cost context

**Files:** Create `nexus/billing/context.py`; Test: `tests/test_billing_cost_context.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_cost_context.py
from __future__ import annotations

import asyncio


def test_report_cost_outside_a_scope_is_a_noop():
    """Infrastructure must never crash because nothing is metering it."""
    from nexus.billing.context import report_cost

    report_cost(usd=0.01, tokens=100)      # must not raise


def test_cost_scope_accumulates():
    from nexus.billing.context import cost_scope, report_cost

    with cost_scope() as cost:
        report_cost(usd=0.004, tokens=400)
        report_cost(usd=0.006, tokens=600)
    assert round(cost.usd, 6) == 0.01
    assert cost.tokens == 1000
    assert cost.calls == 2


def test_nested_scopes_do_not_leak_into_the_parent():
    """An inner scope owns its own costs; the parent keeps only its own."""
    from nexus.billing.context import cost_scope, report_cost

    with cost_scope() as outer:
        report_cost(usd=0.001)
        with cost_scope() as inner:
            report_cost(usd=0.002)
        assert round(inner.usd, 6) == 0.002
    assert round(outer.usd, 6) == 0.001


async def test_concurrent_tasks_get_independent_scopes():
    """ContextVar, not a global: two requests in flight must not pool their costs."""
    from nexus.billing.context import cost_scope, report_cost

    async def one(amount: float) -> float:
        with cost_scope() as cost:
            report_cost(usd=amount)
            await asyncio.sleep(0.01)      # force interleaving
            report_cost(usd=amount)
            return cost.usd

    a, b = await asyncio.gather(one(0.001), one(0.010))
    assert round(a, 6) == 0.002
    assert round(b, 6) == 0.020
```

- [ ] **Step 2: Run to verify it fails.** Expected: `ModuleNotFoundError: nexus.billing.context`

- [ ] **Step 3: Implement**

```python
# nexus/billing/context.py
"""A context-local accumulator for the true cost of servicing one billable action.

The LLM/search layers know what a call COST but not what it was FOR; the endpoint knows what it
was FOR but not what it cost. A ContextVar joins them without threading a parameter through every
call site, and — unlike a module global — keeps two concurrent requests' costs separate.

``report_cost`` is deliberately a no-op when no scope is active: infrastructure must never fail
because nothing happens to be metering it (a background job, a test, a script).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class CostAccumulator:
    """Real spend incurred inside one metered action."""

    usd: float = 0.0
    tokens: int = 0
    calls: int = 0
    detail: list[dict] = field(default_factory=list)

    def add(self, *, usd: float, tokens: int = 0, source: str = "") -> None:
        self.usd += float(usd or 0)
        self.tokens += int(tokens or 0)
        self.calls += 1
        if source:
            self.detail.append({"source": source, "usd": float(usd or 0), "tokens": tokens})


_cost: ContextVar[CostAccumulator | None] = ContextVar("nexus_billing_cost", default=None)


@contextmanager
def cost_scope() -> Iterator[CostAccumulator]:
    """Collect costs reported anywhere inside this block (including nested awaits)."""
    acc = CostAccumulator()
    token = _cost.set(acc)
    try:
        yield acc
    finally:
        _cost.reset(token)


def report_cost(*, usd: float, tokens: int = 0, source: str = "") -> None:
    """Report real spend. Silently ignored when no scope is active — by design."""
    acc = _cost.get()
    if acc is not None:
        acc.add(usd=usd, tokens=tokens, source=source)
```

- [ ] **Step 4: Run** `PYTEST_XDIST_WORKER=m5 py -3.10 -m pytest tests/test_billing_cost_context.py -v` → PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/context.py tests/test_billing_cost_context.py
git commit -m "feat(billing): context-local COGS accumulator"
```

---

## Task 2: The `metered()` seam

**Files:** Create `nexus/billing/meter.py`; Test: `tests/test_billing_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_wiring.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def _seed():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()
    return await make_tenant()


async def test_metered_records_the_action():
    from nexus.billing.meter import metered
    from nexus.models.billing import BillingUsageEvent

    tid = await _seed()
    async with tenant_session(tid) as ts:
        async with metered(ts, "ai.email_draft"):
            pass
        rows = await ts.list(BillingUsageEvent)
        assert len(rows) == 1 and rows[0].capability_id == "ai.email_draft"


async def test_metered_stamps_the_measured_cost():
    """Margin reporting is only real if the cost is measured, not estimated."""
    from nexus.billing.context import report_cost
    from nexus.billing.meter import metered
    from nexus.models.billing import BillingUsageEvent

    tid = await _seed()
    async with tenant_session(tid) as ts:
        async with metered(ts, "ai.email_draft"):
            report_cost(usd=0.0012, tokens=900, source="groq")
        ev = (await ts.list(BillingUsageEvent))[0]
        assert float(ev.unit_cost_usd) == 0.0012


async def test_metered_refunds_when_the_action_fails():
    """A customer must not be billed for an action that raised. The ledger stays append-only,
    so the correction is a compensating negative row, never a delete."""
    from nexus.billing.meter import metered
    from nexus.models.billing import BillingUsageEvent

    tid = await _seed()
    async with tenant_session(tid) as ts:
        try:
            async with metered(ts, "ai.email_draft"):
                raise RuntimeError("provider exploded")
        except RuntimeError:
            pass
        rows = await ts.list(BillingUsageEvent)
        assert len(rows) == 2                                  # charge + compensating row
        assert sum(float(r.quantity) for r in rows) == 0       # nets to zero
        assert any(float(r.quantity) < 0 for r in rows)


async def test_metered_is_transparent_when_enforcement_is_off():
    from nexus.billing.meter import metered
    from nexus.core.config import get_settings
    from nexus.models.billing import BillingUsageEvent

    tid = await _seed()
    settings = get_settings()
    original = settings.billing_enforcement
    settings.billing_enforcement = "off"
    try:
        async with tenant_session(tid) as ts:
            async with metered(ts, "ai.email_draft"):
                pass
            assert await ts.list(BillingUsageEvent) == []
    finally:
        settings.billing_enforcement = original


async def test_metered_never_blocks_in_shadow_mode():
    """The whole point of shadow: a tenant far past quota still gets the action."""
    from nexus.billing.meter import metered
    from nexus.billing.usage import record_usage
    from nexus.models.billing import BillingSubscription

    tid = await _seed()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="free", status="active"))   # quota 20
        await ts.flush()
        for i in range(30):
            await record_usage(ts, capability_id="ai.email_draft", quantity=1,
                               idempotency_key=f"burn{i}")
        async with metered(ts, "ai.email_draft") as m:
            assert m.allowed is True
            assert m.would_block is True        # reported, not enforced
```

- [ ] **Step 2: Run to verify it fails.** Expected: `ModuleNotFoundError: nexus.billing.meter`

- [ ] **Step 3: Implement**

```python
# nexus/billing/meter.py
"""``metered()`` — the one call application code makes to bill an action.

Wraps the M2 gate so a caller writes::

    async with metered(ts, "ai.email_draft", user_id=principal.user_id):
        draft = await write_the_draft()

and gets: the quota decision, the usage row, the measured COGS stamped onto that row, and a
compensating row if the body raises. Application code still never mentions a plan or a price.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import uuid4

from nexus.billing.context import cost_scope
from nexus.billing.entitlements import MeterResult, check_and_meter
from nexus.core.tenancy import TenantSession

logger = logging.getLogger("nexus.billing.meter")


@asynccontextmanager
async def metered(
    ts: TenantSession,
    capability_id: str,
    *,
    quantity: float = 1,
    user_id: str | None = None,
    source: str = "api",
    attrs: dict | None = None,
    idempotency_key: str | None = None,
) -> AsyncIterator[MeterResult]:
    """Gate, record, measure cost, and refund on failure.

    Raises ``QuotaExceeded`` (HTTP 402) only when enforcement is ``on`` AND the plan says no.
    """
    key = idempotency_key or f"{capability_id}:{uuid4().hex}"
    result = await check_and_meter(
        ts, capability_id=capability_id, quantity=quantity, user_id=user_id,
        source=source, idempotency_key=key, attrs=attrs,
    )
    # M2 already owns the block -> 402 translation; reuse it so the payload can never drift.
    result.raise_if_blocked()

    try:
        with cost_scope() as cost:
            yield result
    except Exception:
        # The action failed after we charged for it. Append a compensating row rather than
        # deleting: the event stream is the audit trail, and a disputed charge has to be
        # explainable, not merely absent.
        if result.recorded:
            await _refund(ts, capability_id, quantity, key, user_id, source)
        raise

    if result.recorded and cost.usd > 0:
        await _stamp_cost(ts, key, cost.usd / max(float(quantity), 1.0))


async def _refund(
    ts: TenantSession, capability_id: str, quantity: float, key: str,
    user_id: str | None, source: str,
) -> None:
    from nexus.billing.usage import record_usage

    try:
        await record_usage(
            ts, capability_id=capability_id, quantity=-float(quantity), user_id=user_id,
            source=source, idempotency_key=f"{key}:refund",
            attrs={"refund_of": key, "reason": "action_failed"},
        )
    except Exception:  # never let bookkeeping mask the caller's real exception
        logger.warning("refund failed for %s", capability_id, exc_info=True)


async def _stamp_cost(ts: TenantSession, key: str, unit_cost_usd: float) -> None:
    from nexus.models.billing import BillingUsageEvent

    try:
        ev = await ts.first(BillingUsageEvent, BillingUsageEvent.idempotency_key == key)
        if ev is not None:
            ev.unit_cost_usd = unit_cost_usd
            await ts.flush()
    except Exception:
        logger.warning("cost stamp failed for %s", key, exc_info=True)
```

- [ ] **Step 4: Run** `PYTEST_XDIST_WORKER=m5 py -3.10 -m pytest tests/test_billing_wiring.py -v` → PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/meter.py tests/test_billing_wiring.py
git commit -m "feat(billing): metered() seam with COGS capture and failure refund"
```

---

## Task 3: Measure real LLM cost

**Files:** Modify `nexus/core/config.py`, `nexus/agents/llm.py`; Test: append to
`tests/test_billing_cost_context.py`

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_billing_cost_context.py  (append)
async def test_llm_calls_report_their_cost_into_the_active_scope():
    """Cost is measured from the provider's own token count, not guessed at the endpoint."""
    from nexus.agents.llm import CostTrackingProvider, LLMMessage, StubLLMProvider
    from nexus.billing.context import cost_scope

    inner = StubLLMProvider()
    provider = CostTrackingProvider(inner, usd_per_1k_tokens=0.002)
    with cost_scope() as cost:
        resp = await provider.complete([LLMMessage(role="user", content="hi there")])
    assert resp.text                       # delegation is transparent
    assert cost.calls == 1
    assert cost.usd == resp.tokens / 1000 * 0.002


async def test_cost_tracking_provider_is_transparent_without_a_scope():
    from nexus.agents.llm import CostTrackingProvider, LLMMessage, StubLLMProvider

    provider = CostTrackingProvider(StubLLMProvider(), usd_per_1k_tokens=0.002)
    resp = await provider.complete([LLMMessage(role="user", content="hi")])
    assert resp.text                       # no scope, no crash


def test_llm_cost_rate_is_configurable():
    """Nothing about price or cost is hardcoded — it is settings, like every other rate."""
    from nexus.core.config import get_settings

    assert hasattr(get_settings(), "llm_usd_per_1k_tokens")
```

- [ ] **Step 2: Run to verify it fails.** Expected: `ImportError: cannot import name 'CostTrackingProvider'`

- [ ] **Step 3: Add the setting** in `nexus/core/config.py`, beside the other LLM settings:

```python
    # Blended $/1k tokens used to attribute real COGS to a metered action. Config, never a
    # constant: providers reprice, and margin reporting must follow without a deploy.
    llm_usd_per_1k_tokens: float = 0.0006
```

- [ ] **Step 4: Add the wrapper** to `nexus/agents/llm.py`, immediately after `FallbackLLMProvider`:

```python
class CostTrackingProvider(LLMProvider):
    """Delegates to a real provider and reports what the call cost.

    Wrapping here rather than inside each provider means every model call is measured exactly
    once, whichever backend or fallback served it — there is no path to the network that skips
    the meter.
    """

    def __init__(self, inner: LLMProvider, usd_per_1k_tokens: float):
        self.inner = inner
        self.usd_per_1k_tokens = usd_per_1k_tokens

    async def complete(self, messages, *, temperature=0.2, max_tokens=800,
                       purpose=None, variables=None) -> LLMResponse:
        from nexus.billing.context import report_cost

        resp = await self.inner.complete(
            messages, temperature=temperature, max_tokens=max_tokens,
            purpose=purpose, variables=variables,
        )
        report_cost(
            usd=(resp.tokens or 0) / 1000 * self.usd_per_1k_tokens,
            tokens=resp.tokens or 0,
            source=type(self.inner).__name__,
        )
        return resp
```

- [ ] **Step 5: Wrap in `get_llm_provider()`.** Keep every existing branch exactly as-is; wrap the
result once at the end. `set_llm_provider()` is untouched, so test injection still bypasses the
wrapper.

```python
def get_llm_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        s = get_settings()
        if s.llm_provider == "auto":
            _provider = _build_llm_chain(s)
        elif ...
            ...   # every existing branch unchanged
        else:
            _provider = StubLLMProvider()
        # Measure whatever we ended up with — one wrapper, no bypass.
        _provider = CostTrackingProvider(_provider, s.llm_usd_per_1k_tokens)
    return _provider
```

- [ ] **Step 6: Run** `PYTEST_XDIST_WORKER=m5 py -3.10 -m pytest tests/test_billing_cost_context.py tests/test_agents.py -v` → PASS

- [ ] **Step 7: Commit**

```bash
git add nexus/agents/llm.py nexus/core/config.py tests/test_billing_cost_context.py
git commit -m "feat(billing): measure real LLM cost at the single provider chokepoint"
```

---

## Task 4: 402 handler

**Files:** Modify the FastAPI app (`nexus/main.py` — locate the existing exception handlers)

- [ ] **Step 1: Write the failing test (append to `tests/test_billing_wiring.py`)**

```python
async def test_quota_exceeded_renders_a_useful_402(client):
    """A dead 500 teaches the customer nothing; a 402 with the upgrade path converts."""
    from fastapi import APIRouter

    from nexus.billing.errors import QuotaExceeded
    from nexus.main import app

    r = APIRouter()

    @r.get("/__quota_probe")
    async def probe():
        raise QuotaExceeded("ai.email_draft", reason="quota_exhausted", used=20, quota=20)

    app.include_router(r)
    resp = await client.get("/__quota_probe")
    assert resp.status_code == 402
    body = resp.json()
    assert body["error"] == "quota_exceeded"
    assert body["capability"] == "ai.email_draft"
    assert body["upgrade_url"]
```

- [ ] **Step 2: Run to verify it fails.** Expected: 500, not 402.

- [ ] **Step 3: Register the handler** in `nexus/main.py`, beside the existing handlers:

```python
    @app.exception_handler(QuotaExceeded)
    async def _quota_exceeded(request, exc: QuotaExceeded):
        # 402 Payment Required, carrying what the UI needs to render an upsell in place of a
        # dead error.
        return JSONResponse(status_code=402, content=exc.to_payload())
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit**

```bash
git add nexus/main.py tests/test_billing_wiring.py
git commit -m "feat(billing): render QuotaExceeded as an actionable 402"
```

---

## Task 5: Wire the spend paths

**Files:** Modify `nexus/api/routers/agents.py`, `chat.py`, `contacts.py`; Test: append to
`tests/test_billing_wiring.py`

`POST /agents/{agent_name}/run` is the highest-leverage seam in the product — one wrap covers
every AI action. Map agent name → capability via a module-level dict so adding an agent is a
one-line config change, not a new code path.

- [ ] **Step 1: Write the failing test (append)**

```python
async def test_running_an_agent_records_usage(client):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession
    from nexus.models.billing import BillingUsageEvent
    from nexus.models.identity import Tenant
    from sqlalchemy import select
    from tests.conftest import auth, signup

    await sync_catalog()
    await sync_plans()
    token = await signup(client, slug="wire", email="w@wire.com", company="Wire")
    # Any agent run; the exact agent does not matter, only that it is metered.
    r = await client.post("/api/agents/research/run", headers=auth(token),
                          json={"account_id": None, "prompt": "hello"})
    assert r.status_code in (200, 201, 400, 404, 422)   # shape is not this test's concern

    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "wire"))).first()
        ts = TenantSession(s, tid)
        rows = await ts.list(BillingUsageEvent)
    if r.status_code in (200, 201):
        assert rows, "a successful agent run must be metered"
        assert rows[0].capability_id.startswith("ai.")


async def test_reverify_meters_one_event_per_email(client):
    """Quantity must reflect real consumption: 12 verifications is 12 units, not one call."""
    from nexus.billing.meter import metered
    from tests.conftest import make_tenant, tenant_session
    from nexus.billing.catalog import sync_catalog
    from nexus.models.billing import BillingUsageEvent

    await sync_catalog()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        async with metered(ts, "verify.email", quantity=12):
            pass
        ev = (await ts.list(BillingUsageEvent))[0]
        assert float(ev.quantity) == 12
```

- [ ] **Step 2: Run to verify the first test fails** (no usage recorded).

- [ ] **Step 3: Wire `agents.py`**

```python
# Agent name -> billed capability. A dict, not branching: adding an agent is config.
_AGENT_CAPABILITY = {
    "research": "ai.research_brief",
    "email": "ai.email_draft",
    "qa": "ai.account_qa",
    "contact_rank": "ai.contact_rank",
    "call_script": "ai.call_script",
}
_DEFAULT_AGENT_CAPABILITY = "ai.chat_turn"
```

Wrap the existing body of `run_agent` — do not restructure it:

```python
    capability = _AGENT_CAPABILITY.get(agent_name, _DEFAULT_AGENT_CAPABILITY)
    async with metered(ts, capability, user_id=principal.user_id,
                       attrs={"agent": agent_name}):
        ...   # the existing body, unchanged
```

Do the same for `run_pipeline` with `"workflow.orchestration_run"`.

- [ ] **Step 4: Wire `chat.py`** — `create_session` and `post_message` with `"ai.chat_turn"`;
`save_icp` with `"ai.icp_from_website"`.

- [ ] **Step 5: Wire `contacts.py`** — `reverify_contact_emails` with `"verify.email"`. The
quantity is the number of addresses actually verified, so meter *after* the count is known, using
the explicit-quantity form rather than wrapping the whole handler.

- [ ] **Step 6: Run** the wiring tests plus each touched router's existing suite:

```bash
PYTEST_XDIST_WORKER=m5 py -3.10 -m pytest tests/test_billing_wiring.py tests/test_agents.py tests/test_chat.py tests/test_contacts.py -v
```

Expected: PASS, with **no change to any existing assertion**. If an existing test now fails,
STOP and report — a metered endpoint must behave identically in shadow mode.

- [ ] **Step 7: Commit**

```bash
git add nexus/api/routers/agents.py nexus/api/routers/chat.py nexus/api/routers/contacts.py tests/test_billing_wiring.py
git commit -m "feat(billing): meter agent runs, chat turns, and email verification"
```

---

## Task 6: Gate

- [ ] `PYTEST_XDIST_WORKER=m5 py -3.10 -m pytest tests/ -k billing -q` → all pass
- [ ] `py -3.10 -m ruff check nexus/ tests/ migrations/` → All checks passed
- [ ] Confirm `get_settings().billing_enforcement == "shadow"` is still the default.
- [ ] Orchestrator runs the full suite.

---

## Self-review

**Spec coverage:** single metering seam ([03](../../billing/03-Metering-Architecture.md) §1) → T2;
measured not estimated COGS ([12](../../billing/12-Cost-Analysis.md) §1) → T1/T3;
402 with upgrade path ([10](../../billing/10-Usage-Tracking.md) §3) → T4;
billable resource coverage ([09](../../billing/09-Billable-Resources.md)) → T5 (first tranche).
Deferred: worker-side handlers (`workers/tasks.py`), network/calling/campaign routers, search
provider COGS — a second tranche once this one proves out in shadow.

**Placeholder scan:** none — all steps ship complete code, except T5 steps 3–5 which
deliberately wrap existing handler bodies the implementer must read first.

**Type consistency:** `cost_scope()`/`report_cost()` (T1) consumed by `metered()` (T2) and
`CostTrackingProvider` (T3). `metered()` yields the M2 `MeterResult` (`.allowed`, `.would_block`,
`.used`, `.quota`, `.entitlement`, `.recorded`) — matching the M2 dataclass. `QuotaExceeded`
constructor matches `nexus/billing/errors.py` (`capability_id` positional; `reason` keyword-only).
`record_usage(...)` keywords match M2.
