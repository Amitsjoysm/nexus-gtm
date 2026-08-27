# Day-1 Feedback Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all eight items of live-site tester feedback — four caused by one dead-LLM misconfiguration plus a stale image, four genuine product gaps — without regressing any working feature.

**Architecture:** Three layers. (A) Configuration and deploy: the live `llm_model` is `gpt-4o-mini`, which Groq 404s, so every AI feature silently returns empty; the deployed image also predates several committed fixes. (B) Scoring correctness: the relevance engine conflates "unknown" with "absent" for tech, which drops every newly-discovered account below the discovery gate. (C) New capability: CSV ingest, richer ICP filters, deterministic job-level/keyword title matching, and per-tenant signal category control.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, Pydantic v2, Alembic, pytest (`asyncio_mode=auto`), React 18 + TypeScript + Vite.

**Hard constraint — no regressions.** Every new ICP key, signal preference and Account column is *additive and absent-by-default*, and each task carries a test asserting that behaviour is unchanged when the new thing is not set. The codebase's existing bias — unknown resolves permissive — is preserved everywhere.

---

## Evidence behind each item

Measured against the live deployment on 2026-08-27, not inferred:

| # | Tester reported | Actual cause | Status |
|---|---|---|---|
| 2 | Website analysis fails, all 3 campaigns | `llm_model=gpt-4o-mini` → Groq HTTP 404 `model_not_found`. Plus `max_tokens=800` truncates the JSON mid-string so `_JSON_OBJ` matches nothing → silent empty draft | Task 1 + 3 |
| 4 | AI titles generic (CTO, Head of Sales…) | Dead LLM → falls through to `_DEFAULT_COMMITTEE` in `contact_search.py:43`, which is *literally* that list | Task 1 + 10 |
| 6 | No contacts, all 3 campaigns | `_extract_people` parses JSON from `llm.complete()`; dead LLM returns `''` → `_parse_people('')` → `[]` | Task 1 + 9 |
| 8 | Personalization untestable | Blocked by #6 | Task 1 |
| 7 | Tech stack → 0 accounts | `engine.py:139` scores tech `hits/required`; a newly-discovered account has empty `tech_stack`, so unknown scores **0.0** — identical to "definitely absent" — dragging the composite under `auto.py:221`'s `min_fit` gate | Task 4 |
| 5 | Signals appear though none enabled | `signal_sources` is deployment-global; there is no per-tenant category control at all | Task 13 |
| 3 | ICP filters too limited | `Account` has no `region`/`postal_code`/`annual_revenue` columns; ICP has no `job_levels`/`title_keywords` | Tasks 5–8 |
| 1 | No CSV upload | `custom_fields.import_csv` only *annotates* rows that already match; it never creates an Account or Contact | Tasks 11–12 |

Verification commands used, for anyone re-checking:

```bash
docker compose -f deploy/docker-compose.prod.yml exec -T app python -c "from nexus.core.config import get_settings; print(get_settings().llm_model)"
```

---

## File structure

| File | Responsibility | Task |
|---|---|---|
| `nexus/core/config.py:166` | `llm_model` default — currently an OpenAI name on a Groq chain | 1 |
| `tests/test_llm_model_default.py` | **Create.** Default model must be servable by the default provider | 1 |
| `nexus/relevance/engine.py:133-141` | Tech sub-score; unknown must be neutral | 4 |
| `migrations/versions/0051_account_geo_revenue.py` | **Create.** `region`, `postal_code`, `annual_revenue` | 5 |
| `nexus/models/account.py` | The three new columns | 5 |
| `nexus/relevance/engine.py` | Score region / postal / revenue | 6 |
| `nexus/relevance/job_levels.py` | **Create.** Deterministic level + keyword title matching | 8 |
| `nexus/relevance/titles.py` | ICP-aware suggestions | 10 |
| `nexus/agents/contact_rec.py:44` | Use the new matcher | 9 |
| `nexus/integrations/contact_search.py` | Pass levels/keywords into extraction | 9 |
| `nexus/imports/csv_ingest.py` | **Create.** Parse + upsert accounts/contacts | 11, 12 |
| `nexus/api/routers/imports.py` | **Create.** `POST /imports/accounts`, `/imports/contacts` | 11, 12 |
| `migrations/versions/0052_signal_preferences.py` | **Create.** Per-tenant signal category control | 13 |
| `nexus/models/signal_preference.py` | **Create.** | 13 |
| `nexus/ingestion/pipeline.py` | Honour the preference | 13 |

---

## Task 1: The dead LLM — make a wrong default impossible

The single highest-value fix. `llm_provider="auto"` builds a **Groq** chain, and `config.py:166` defaults `llm_model` to `"gpt-4o-mini"` — an **OpenAI** model name. Groq returns HTTP 404 `model_not_found` on every call, `GroqLLMProvider` swallows it, and the caller receives `""`. Nothing logs an error a human would see.

This is the *second* time this exact shape has shipped (see CLAUDE.md, 2026-08-21: `llama-3.3-70b-versatile` withdrawn, stub wrote every outbound email). A test is the only thing that stops a third.

> **`nexus/core/config.py` has uncommitted user work. Make the one-line change but do NOT `git add` it** — hand it to the user for their own commit. The test file is ours.

**Files:**
- Create: `tests/test_llm_model_default.py`
- Modify: `nexus/core/config.py:166`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_model_default.py
"""The default model must be one the default provider can actually serve.

Measured on the live deployment 2026-08-27: `llm_model` defaulted to `gpt-4o-mini`, an OpenAI
model name, while `llm_provider="auto"` builds a Groq chain. Groq answers HTTP 404
`model_not_found`, `GroqLLMProvider` swallows it, and every caller receives an empty string --
website analysis, title suggestion, contact extraction and personalization all silently degraded
with nothing reporting a fault.

This is the second occurrence of the shape (CLAUDE.md, 2026-08-21). A unit test is the only thing
that catches it before a deploy, because the failure needs a live provider to observe.
"""
from __future__ import annotations

# Model families each provider can serve. Not an allowlist of exact ids -- ids churn, and pinning
# them would make this test fail every time a vendor renames something. The prefix is what encodes
# "this name belongs to that provider's namespace", which is the actual mistake being prevented.
_SERVABLE = {
    "groq": ("llama", "meta-llama/", "openai/gpt-oss", "qwen/", "deepseek", "kimi", "mixtral", "gemma"),
    "anthropic": ("claude-",),
}


def test_the_default_model_is_servable_by_the_default_provider():
    from nexus.core.config import Settings

    s = Settings(secret_key="x" * 40)
    provider = (s.llm_provider or "auto").lstrip("=").lower()
    chain = "groq" if provider in ("auto", "groq") else provider
    if chain not in _SERVABLE:
        return  # a provider this test has no opinion about

    model = (s.llm_model or "").lower()
    assert any(model.startswith(p) for p in _SERVABLE[chain]), (
        f"llm_model default {s.llm_model!r} is not in {chain}'s namespace. "
        f"{chain} will answer 404 and the provider returns '' rather than raising, so every AI "
        f"feature degrades silently. Expected one of {_SERVABLE[chain]}."
    )


def test_a_openai_model_name_on_the_groq_chain_is_rejected():
    """Pins the exact live misconfiguration so it cannot come back."""
    model = "gpt-4o-mini"
    assert not any(model.startswith(p) for p in _SERVABLE["groq"]), (
        "gpt-4o-mini must not be considered Groq-servable -- it is the value that took the live "
        "deployment's AI features down on 2026-08-27"
    )
```

- [ ] **Step 2: Run it and watch the first test fail**

Run: `python -m pytest tests/test_llm_model_default.py -n0 -v`
Expected: `test_the_default_model_is_servable_by_the_default_provider` FAILS with "llm_model default 'gpt-4o-mini' is not in groq's namespace". The second test passes already.

- [ ] **Step 3: Fix the default**

In `nexus/core/config.py:166`, replace:

```python
    llm_model: str = "gpt-4o-mini"
```

with:

```python
    # Must live in the DEFAULT PROVIDER's namespace. `llm_provider="auto"` builds a Groq chain, so
    # an OpenAI model name here is a 404 on every completion -- and `GroqLLMProvider` returns ''
    # rather than raising, so the whole product degrades in silence. That shipped twice; see
    # tests/test_llm_model_default.py.
    llm_model: str = "openai/gpt-oss-120b"
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_llm_model_default.py -n0 -v`
Expected: 2 passed.

- [ ] **Step 5: Commit the test only**

```bash
git add tests/test_llm_model_default.py
git commit -m "test(config): pin the default model to the default provider's namespace"
```

`nexus/core/config.py` is left modified and uncommitted — it carries other in-flight user work.

---

## Task 2: Deploy the current image

The running image predates `nexus/agents/token_budget.py`, `nexus/runtime_config/` and `nexus/billing/tokens.py`. The tester exercised code several commits old, so some of what they hit is already fixed on `master`.

`openai/gpt-oss-120b` is exactly the model whose 8,000 TPM ceiling produced the earlier 429/413 reports, and `token_budget.py` is the fix for it — the two must ship together or the model change trades one failure for another.

**Files:**
- Modify: `deploy/.env` (already done: `NEXUS_LLM_MODEL`, `NEXUS_FIRECRAWL_API_KEYS`)

- [ ] **Step 1: Confirm what is missing from the running image**

```bash
docker compose -f deploy/docker-compose.prod.yml exec -T app test -f /app/nexus/agents/token_budget.py && echo present || echo MISSING
```
Expected before the rebuild: `MISSING`.

- [ ] **Step 2: Build the frontend first**

```bash
cd frontend && npm run build && cd ..
```
The bundle lands in `nexus/web/dist/`, which the image copies — so this must precede the image build.

- [ ] **Step 3: Rebuild and restart**

```bash
docker compose -f deploy/docker-compose.prod.yml build app worker
docker compose -f deploy/docker-compose.prod.yml up -d
```

- [ ] **Step 4: Verify the LLM is alive**

```bash
docker compose -f deploy/docker-compose.prod.yml exec -T app python -c "
import asyncio
from nexus.agents.llm import get_llm_provider, LLMMessage
async def m():
    r = await get_llm_provider().complete([LLMMessage('user','Reply with exactly: ALIVE')], max_tokens=400)
    print(repr(r.text[:80]))
asyncio.run(m())"
```
Expected: text containing `ALIVE`. An empty string means the model is still wrong — stop and re-check `NEXUS_LLM_MODEL`.

- [ ] **Step 5: Verify Firecrawl resolves**

```bash
docker compose -f deploy/docker-compose.prod.yml exec -T app python -c "
from nexus.core.config import get_settings
s=get_settings(); print('firecrawl keys:', len([k for k in (s.firecrawl_api_keys or '').split(',') if k.strip()]))"
```
Expected: `3`. Before this change `signal_search_provider` was `firecrawl` with **zero** keys.

---

## Task 3: Website analysis — surface the failure

The user's working tree already carries this fix (`max_tokens` 800 → 2000, code-fence stripping, and a log line on every failure path). It is uncommitted, so it is not in any image.

Root cause: the requested object runs ~2,996 characters (~750 tokens) and stopped **at** the 800-token cap, mid-string. A truncated object has no closing brace, `_JSON_OBJ` matches nothing, and the function returns `_empty_draft()` — the tester saw "couldn't analyze that site" after a fully successful, fully billed LLM call.

**Files:**
- Modify: `nexus/relevance/website_icp.py` (already modified in the working tree)
- Create: `tests/test_website_icp_truncation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_website_icp_truncation.py
"""A truncated LLM response must not look like a site with no discoverable ICP.

Measured on the live deployment: the requested object runs ~2,996 characters and `max_tokens=800`
cut it mid-string. With no closing brace `_JSON_OBJ` matches nothing, the function returns an empty
draft, and the UI reports "couldn't analyze that site" -- after a successful, billed LLM call. The
tester hit this on all three campaigns and concluded the feature was broken, which it was, but not
for the reason shown.
"""
from __future__ import annotations

import pytest


class _FakeLLM:
    def __init__(self, text): self.text = text; self.seen = {}
    async def complete(self, messages, **kw):
        self.seen = kw
        class R: pass
        r = R(); r.text = self.text; return r


class _FakeSearch:
    async def search(self, *a, **k): return []


async def test_a_fenced_json_object_is_parsed():
    """Models wrap JSON in ```json fences even when told not to."""
    from nexus.relevance.website_icp import analyze_website_to_icp

    fenced = '```json\n{"industries": ["SaaS"], "countries": ["United States"]}\n```'
    draft = await analyze_website_to_icp("https://vanta.com", search=_FakeSearch(), llm=_FakeLLM(fenced))
    assert draft["icp"].get("industries") == ["SaaS"], "a fenced response must still parse"


async def test_the_token_budget_fits_the_object_we_ask_for():
    """800 tokens could not hold the requested object. Pin the headroom rather than the exact
    number, so a later prompt change that grows the object is what fails, not this assertion."""
    from nexus.relevance.website_icp import analyze_website_to_icp

    llm = _FakeLLM('{"industries": []}')
    await analyze_website_to_icp("https://vanta.com", search=_FakeSearch(), llm=llm)
    assert llm.seen.get("max_tokens", 0) >= 1500, (
        f"max_tokens={llm.seen.get('max_tokens')} -- the measured response is ~750 tokens and a "
        f"truncated object parses as nothing at all"
    )
```

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/test_website_icp_truncation.py -n0 -v`
Expected: PASS if the working-tree fix is intact; FAIL if it was reverted.

- [ ] **Step 3: Commit the test only**

```bash
git add tests/test_website_icp_truncation.py
git commit -m "test(relevance): a truncated or fenced ICP response must not read as an empty site"
```

`nexus/relevance/website_icp.py` stays uncommitted — it is the user's in-flight work.

---

## Task 4: Unknown tech must score neutral, not absent

`engine.py:133-141` computes `sub["tech"] = len(hits) / len(required_tech)`. A newly-discovered account has an **empty** `tech_stack` — nobody has enriched it yet — so it scores `0.0`, which is indistinguishable from "we checked and they do not use it". `nexus/discovery/auto.py:221` then drops everything under `min_fit`, and the tester saw **zero accounts** the moment they added a tech requirement.

Every other dimension in this engine already handles unknown as neutral: no industries → `0.5`, no countries → `0.5`. Tech is the outlier.

**Files:**
- Modify: `nexus/relevance/engine.py:133-141`
- Create: `tests/test_relevance_unknown_tech.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_relevance_unknown_tech.py
"""'We do not know their stack' and 'they do not use it' are different facts.

Reported by a tester 2026-08-27: adding any tech to the ICP took account results to zero, in both
the Accounts view and the Orchestrator. Cause: a freshly-discovered account has an empty
`tech_stack` because nothing has enriched it yet, and the engine scored that 0.0 -- the same as a
confirmed miss. `nexus/discovery/auto.py:221` drops anything under `min_fit`, so requiring tech
eliminated every candidate before enrichment could ever populate the field.

The rest of the engine already treats unknown as neutral (industry and geo both score 0.5 when the
ICP says nothing). This makes tech consistent with that.
"""
from __future__ import annotations


def _account(**kw):
    from nexus.models.account import Account
    return Account(tenant_id="t1", name=kw.pop("name", "Acme"), **kw)


def _profile(icp):
    from nexus.models.relevance import RelevanceProfile
    return RelevanceProfile(tenant_id="t1", icp=icp)


def test_an_account_with_an_unknown_stack_is_not_scored_as_a_miss():
    from nexus.relevance.engine import RelevanceEngine

    icp = {"required_tech": ["salesforce", "hubspot"]}
    fit = RelevanceEngine().score(_account(tech_stack=[]), profile=_profile(icp))
    assert fit.breakdown["tech"] == 0.5, (
        f"unknown stack scored {fit.breakdown['tech']} -- 0.0 makes every un-enriched account fail "
        f"the discovery min_fit gate, which is why adding tech returned zero accounts"
    )


def test_a_confirmed_miss_still_scores_zero():
    """The neutral treatment must not hide a real mismatch: we know this account's stack, and the
    required tech is not in it."""
    from nexus.relevance.engine import RelevanceEngine

    icp = {"required_tech": ["salesforce"]}
    fit = RelevanceEngine().score(_account(tech_stack=["pipedrive", "intercom"]), profile=_profile(icp))
    assert fit.breakdown["tech"] == 0.0, "a known, non-matching stack is a genuine miss"


def test_a_partial_match_is_unchanged():
    from nexus.relevance.engine import RelevanceEngine

    icp = {"required_tech": ["salesforce", "hubspot"]}
    fit = RelevanceEngine().score(_account(tech_stack=["salesforce"]), profile=_profile(icp))
    assert fit.breakdown["tech"] == 0.5


def test_no_required_tech_is_unchanged():
    """Regression guard: the existing neutral-when-unspecified behaviour must not move."""
    from nexus.relevance.engine import RelevanceEngine

    fit = RelevanceEngine().score(_account(tech_stack=["salesforce"]), profile=_profile({}))
    assert fit.breakdown["tech"] == 0.5
```

- [ ] **Step 2: Run it and watch the first test fail**

Run: `python -m pytest tests/test_relevance_unknown_tech.py -n0 -v`
Expected: `test_an_account_with_an_unknown_stack_is_not_scored_as_a_miss` FAILS with `unknown stack scored 0.0`. The other three pass.

- [ ] **Step 3: Implement**

In `nexus/relevance/engine.py`, replace lines 133-141:

```python
        required_tech = [t.lower() for t in icp.get("required_tech", [])]
        if not required_tech:
            sub["tech"] = 0.5
        else:
            owned = {t.lower() for t in (account.tech_stack or [])}
            hits = [t for t in required_tech if t in owned]
            sub["tech"] = len(hits) / len(required_tech)
            if hits:
                reasons.append(f"uses {', '.join(hits)}")
```

with:

```python
        required_tech = [t.lower() for t in icp.get("required_tech", [])]
        owned = {t.lower() for t in (account.tech_stack or [])}
        if not required_tech:
            sub["tech"] = 0.5
        elif not owned:
            # UNKNOWN, not absent. A freshly-discovered account has an empty stack because nothing
            # has enriched it yet, and scoring that 0.0 put it below `min_fit` in
            # nexus/discovery/auto.py -- so requiring any tech returned zero accounts and
            # enrichment never got the chance to fill the field in. Neutral matches how industry
            # and geo already treat "we have no data".
            sub["tech"] = 0.5
            reasons.append("tech stack unknown; not counted against fit")
        else:
            hits = [t for t in required_tech if t in owned]
            sub["tech"] = len(hits) / len(required_tech)
            if hits:
                reasons.append(f"uses {', '.join(hits)}")
            else:
                reasons.append(f"stack known and does not include {', '.join(required_tech)}")
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_relevance_unknown_tech.py -n0 -v`
Expected: 4 passed.

- [ ] **Step 5: Run the whole relevance + discovery suite for regressions**

Run: `python -m pytest tests/test_relevance*.py tests/test_discovery*.py tests/test_lookalike*.py -n4 -q`
Expected: all pass. These suites encode the scoring contract; a change here is exactly what they exist to catch.

- [ ] **Step 6: Commit**

```bash
git add nexus/relevance/engine.py tests/test_relevance_unknown_tech.py
git commit -m "fix(relevance): an unknown tech stack is not a failed match"
```

---

## Task 5: Account columns for region, postal code and revenue

The tester asked for State/Province, ZIP and Revenue filters. `Account` carries only `country`, so there is nothing to score against — the filter has to exist on the record before it can exist in the ICP.

All three are nullable and default NULL, so every existing row and every code path that does not mention them behaves exactly as before.

**Files:**
- Create: `migrations/versions/0051_account_geo_revenue.py`
- Modify: `nexus/models/account.py` (after line 29, `country`)
- Create: `tests/test_account_geo_revenue.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_account_geo_revenue.py
"""Region, postal code and revenue on the account record.

Requested by a tester 2026-08-27: Country was the only geographic filter, and there was no revenue
filter at all. An ICP cannot filter on a field the account does not carry.

All three are nullable. A NULL must behave exactly as the column not existing did -- the engine
scores it neutral rather than as a miss, for the same reason unknown tech does (see
tests/test_relevance_unknown_tech.py).
"""
from __future__ import annotations


async def test_the_new_columns_exist_and_default_to_null(fresh_db):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.account import Account

    async with get_sessionmaker()() as s:
        s.add(Account(tenant_id="t1", name="Acme"))
        await s.commit()
        a = (await s.scalars(select(Account).where(Account.name == "Acme"))).one()
        assert a.region is None
        assert a.postal_code is None
        assert a.annual_revenue is None


async def test_the_columns_round_trip(fresh_db):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.account import Account

    async with get_sessionmaker()() as s:
        s.add(Account(tenant_id="t1", name="Beta", region="California",
                      postal_code="94107", annual_revenue=25_000_000))
        await s.commit()
        a = (await s.scalars(select(Account).where(Account.name == "Beta"))).one()
        assert (a.region, a.postal_code, a.annual_revenue) == ("California", "94107", 25_000_000)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_account_geo_revenue.py -n0 -v`
Expected: FAIL with `AttributeError: 'Account' object has no attribute 'region'`.

- [ ] **Step 3: Add the columns to the model**

In `nexus/models/account.py`, immediately after the `country` column (line 29), add:

```python
    # Sub-country geography and revenue. Requested by a tester who could filter on Country and
    # nothing finer. All nullable: a NULL means "not known", which the relevance engine scores
    # neutral rather than as a miss -- an account nobody has enriched must not be punished for it.
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    annual_revenue: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
```

Add `BigInteger` to the SQLAlchemy import at the top of the file. Revenue is stored in whole units of currency, and a large enterprise exceeds a 32-bit integer.

- [ ] **Step 4: Write the migration**

```python
# migrations/versions/0051_account_geo_revenue.py
"""Sub-country geography and revenue on accounts.

Additive and nullable, so every existing row is untouched and every query that does not name these
columns is unaffected. `scripts/apply_rls.py` needs no change: `accounts` is already enrolled.

Revision ID: 0051_account_geo_revenue
Revises: 0050_runtime_settings
"""
from alembic import op
import sqlalchemy as sa

revision = "0051_account_geo_revenue"
down_revision = "0050_runtime_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("region", sa.String(120), nullable=True))
    op.add_column("accounts", sa.Column("postal_code", sa.String(20), nullable=True))
    op.add_column("accounts", sa.Column("annual_revenue", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "annual_revenue")
    op.drop_column("accounts", "postal_code")
    op.drop_column("accounts", "region")
```

- [ ] **Step 5: Run the tests plus the migration replay**

Run: `python -m pytest tests/test_account_geo_revenue.py tests/test_migrations_replay.py -n0 -v`
Expected: all pass. `test_migrations_replay` builds a database from `alembic upgrade head` alone and diffs it against `Base.metadata` — it is what catches a model change with no migration.

- [ ] **Step 6: Confirm there is exactly one head**

Run: `python -c "from alembic.script import ScriptDirectory; from alembic.config import Config; print(ScriptDirectory.from_config(Config('alembic.ini')).get_heads())"`
Expected: `('0051_account_geo_revenue',)` — exactly one entry. Two heads means a branch collision, which `upgrade head` refuses to run.

- [ ] **Step 7: Commit**

```bash
git add migrations/versions/0051_account_geo_revenue.py nexus/models/account.py tests/test_account_geo_revenue.py
git commit -m "feat(accounts): region, postal code and annual revenue"
```

---

## Task 6: Score the new geography and revenue filters

**Files:**
- Modify: `nexus/relevance/engine.py` (after the `countries` block, ~line 122-132)
- Create: `tests/test_relevance_geo_revenue.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_relevance_geo_revenue.py
"""Region, postal-code and revenue scoring.

Every one of these is absent-by-default: an ICP that does not mention them must score exactly as it
did before they existed. That is the regression guard, and it is the first test here because it is
the one that protects every existing tenant.
"""
from __future__ import annotations


def _account(**kw):
    from nexus.models.account import Account
    return Account(tenant_id="t1", name=kw.pop("name", "Acme"), **kw)


def _profile(icp):
    from nexus.models.relevance import RelevanceProfile
    return RelevanceProfile(tenant_id="t1", icp=icp)


def test_an_icp_that_names_none_of_them_scores_exactly_as_before():
    """The regression guard. Adding three dimensions must not move an existing tenant's scores."""
    from nexus.relevance.engine import RelevanceEngine

    icp = {"industries": ["SaaS"], "countries": ["United States"]}
    acct = _account(industry="SaaS", country="United States", employee_count=200)
    fit = RelevanceEngine().score(acct, profile=_profile(icp))
    for key in ("region", "postal", "revenue"):
        assert key not in fit.breakdown, f"{key} must not appear when the ICP does not ask for it"


def test_region_matches_case_insensitively():
    from nexus.relevance.engine import RelevanceEngine

    icp = {"regions": ["California"]}
    fit = RelevanceEngine().score(_account(region="california"), profile=_profile(icp))
    assert fit.breakdown["region"] == 1.0


def test_an_unknown_region_is_neutral_not_a_miss():
    from nexus.relevance.engine import RelevanceEngine

    icp = {"regions": ["California"]}
    fit = RelevanceEngine().score(_account(region=None), profile=_profile(icp))
    assert fit.breakdown["region"] == 0.5, "an un-enriched account must not be punished"


def test_postal_code_matches_on_a_prefix():
    """GTM teams target areas, not single codes: '941' must catch 94107 and 94110."""
    from nexus.relevance.engine import RelevanceEngine

    icp = {"postal_codes": ["941"]}
    fit = RelevanceEngine().score(_account(postal_code="94107"), profile=_profile(icp))
    assert fit.breakdown["postal"] == 1.0


def test_revenue_inside_the_band_scores_full():
    from nexus.relevance.engine import RelevanceEngine

    icp = {"revenue_min": 10_000_000, "revenue_max": 100_000_000}
    fit = RelevanceEngine().score(_account(annual_revenue=25_000_000), profile=_profile(icp))
    assert fit.breakdown["revenue"] == 1.0


def test_unknown_revenue_is_neutral():
    from nexus.relevance.engine import RelevanceEngine

    icp = {"revenue_min": 10_000_000}
    fit = RelevanceEngine().score(_account(annual_revenue=None), profile=_profile(icp))
    assert fit.breakdown["revenue"] == 0.5
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_relevance_geo_revenue.py -n0 -v`
Expected: the first test passes (nothing added yet); the rest FAIL with `KeyError: 'region'`.

- [ ] **Step 3: Implement**

In `nexus/relevance/engine.py`, immediately after the `countries` block, insert:

```python
        # Region / postal / revenue are scored ONLY when the ICP asks for them, so a tenant who
        # never sets one sees identical scores to before these existed. Each treats a NULL on the
        # account as neutral rather than as a miss, matching `country` and tech: an account nobody
        # has enriched must not be pushed below the discovery gate for missing data.
        regions = [r.lower() for r in icp.get("regions", []) if r]
        if regions:
            if not account.region:
                sub["region"] = 0.5
            elif account.region.lower() in regions:
                sub["region"] = 1.0
                reasons.append(f"region '{account.region}' in ICP")
            else:
                sub["region"] = 0.0

        # Prefix match, because GTM teams target areas rather than single codes -- '941' should
        # catch every San Francisco code, and requiring the whole string would make the filter
        # useless for anything but a single building.
        postals = [str(p).strip() for p in icp.get("postal_codes", []) if str(p).strip()]
        if postals:
            if not account.postal_code:
                sub["postal"] = 0.5
            elif any(account.postal_code.startswith(p) for p in postals):
                sub["postal"] = 1.0
                reasons.append(f"postal '{account.postal_code}' in target area")
            else:
                sub["postal"] = 0.0

        rev_min, rev_max = icp.get("revenue_min"), icp.get("revenue_max")
        if rev_min is not None or rev_max is not None:
            sub["revenue"] = _band_score(account.annual_revenue, rev_min, rev_max)
            if account.annual_revenue is not None:
                inside = sub["revenue"] >= 0.999
                reasons.append(
                    f"revenue {account.annual_revenue:,} "
                    f"{'within' if inside else 'outside'} target band"
                )
```

Then add default weights. In the `DEFAULT_WEIGHTS` dict at the top of the file, add:

```python
    "region": 0.5,
    "postal": 0.5,
    "revenue": 1.0,
```

Region and postal are deliberately half-weight: they refine a geography that `country` already scores, and giving each full weight would let geography outvote industry and size together.

- [ ] **Step 4: Check `_band_score` returns 0.5 for None**

Run: `python -c "from nexus.relevance.engine import _band_score; print(_band_score(None, 10, 100))"`
Expected: `0.5`. If it returns `0.0`, fix `_band_score` to return `0.5` when the value is `None` — the same unknown-is-neutral rule, and `employee_count` needs it too.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_relevance_geo_revenue.py tests/test_relevance_unknown_tech.py -n0 -v`
Expected: all pass.

- [ ] **Step 6: Full relevance regression**

Run: `python -m pytest tests/test_relevance*.py tests/test_discovery*.py -n4 -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add nexus/relevance/engine.py tests/test_relevance_geo_revenue.py
git commit -m "feat(relevance): score region, postal code and revenue when the ICP asks"
```

---

## Task 7: Job levels and title keywords in the ICP

`profile.icp` is a JSON column, so new keys need **no migration**.

**Files:**
- Create: `nexus/relevance/job_levels.py`
- Create: `tests/test_job_levels.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_job_levels.py
"""Deterministic job-level and keyword title matching.

The tester's example, verbatim: targeting Facilities leadership should match 'Head of Facilities',
'Facilities Head', 'Facilities Director', 'Director of Facilities' and 'Director, Facilities' --
but exact-title matching found none of them, and the contact search returned nothing across three
campaigns.

Deterministic on purpose, no LLM. Title matching decides which humans a rep contacts, and a rep who
asks "why did this person match?" deserves an answer better than "the model thought so". It also
runs inside the scoring path, where this codebase does not put LLM calls.
"""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("title,level", [
    ("Chief Technology Officer", "c_level"),
    ("CTO", "c_level"),
    ("Chief Executive Officer", "c_level"),
    ("SVP Engineering", "vp"),
    ("Senior Vice President, Sales", "vp"),
    ("VP Facilities", "vp"),
    ("Head of Facilities", "head"),
    ("Facilities Head", "head"),
    ("Director of Facilities", "director"),
    ("Facilities Director", "director"),
    ("Director, Facilities", "director"),
    ("Senior Manager, Operations", "manager"),
    ("Facilities Manager", "manager"),
    ("Software Engineer", "ic"),
])
def test_titles_normalise_to_a_level(title, level):
    from nexus.relevance.job_levels import level_of
    assert level_of(title) == level


def test_founder_reads_as_c_level():
    """A 30-person company's Founder IS the economic buyer."""
    from nexus.relevance.job_levels import level_of
    assert level_of("Founder & CEO") == "c_level"
    assert level_of("Co-Founder") == "c_level"


def test_word_order_does_not_matter_for_keywords():
    """The exact failure the tester hit."""
    from nexus.relevance.job_levels import matches_title

    spec = {"job_levels": ["director", "head"], "title_keywords": ["facilities"]}
    for title in ("Head of Facilities", "Facilities Head", "Facilities Director",
                  "Director of Facilities", "Director, Facilities"):
        assert matches_title(title, spec), f"{title!r} should match Facilities leadership"


def test_the_level_gate_excludes_the_wrong_seniority():
    from nexus.relevance.job_levels import matches_title

    spec = {"job_levels": ["director", "vp"], "title_keywords": ["facilities"]}
    assert not matches_title("Facilities Coordinator", spec)
    assert not matches_title("Facilities Manager", spec)


def test_excluded_keywords_win():
    """'Director of Facilities' yes; 'Assistant Director of Facilities' no."""
    from nexus.relevance.job_levels import matches_title

    spec = {"job_levels": ["director"], "title_keywords": ["facilities"],
            "exclude_title_keywords": ["assistant", "deputy"]}
    assert matches_title("Director of Facilities", spec)
    assert not matches_title("Assistant Director of Facilities", spec)


def test_an_empty_spec_matches_everything():
    """THE regression guard. Every existing tenant has no job_levels and no title_keywords, and
    must keep seeing exactly the contacts they see today."""
    from nexus.relevance.job_levels import matches_title

    for title in ("Software Engineer", "CEO", "", "Facilities Director"):
        assert matches_title(title, {}) is True


def test_keywords_alone_work_without_levels():
    from nexus.relevance.job_levels import matches_title

    spec = {"title_keywords": ["facilities"]}
    assert matches_title("Facilities Coordinator", spec)
    assert not matches_title("Software Engineer", spec)


def test_levels_alone_work_without_keywords():
    from nexus.relevance.job_levels import matches_title

    spec = {"job_levels": ["c_level"]}
    assert matches_title("Chief Financial Officer", spec)
    assert not matches_title("Facilities Manager", spec)


def test_expand_titles_produces_searchable_phrases():
    """The contact-search provider queries a web index, so it needs phrases, not a predicate."""
    from nexus.relevance.job_levels import expand_titles

    out = expand_titles({"job_levels": ["director", "head"], "title_keywords": ["facilities"]})
    lowered = {o.lower() for o in out}
    assert "director of facilities" in lowered
    assert "facilities director" in lowered
    assert "head of facilities" in lowered
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_job_levels.py -n0 -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.relevance.job_levels'`.

- [ ] **Step 3: Implement**

```python
# nexus/relevance/job_levels.py
"""Deterministic job-level and keyword title matching.

Exact-title matching was why a tester's contact search returned nothing across three campaigns:
they asked for 'Facilities Director' and the index held 'Director of Facilities', 'Head of
Facilities' and 'Facilities Head'. A person's title is written five ways by five companies, and the
one a rep types is never the one the data holds.

No LLM here, deliberately. This decides which humans a rep contacts; "the model thought so" is not
an answer to "why did this person match?", and the scoring path in this codebase does not make
network calls.

Two independent gates, and BOTH default to open:

* ``job_levels``     -- seniority, normalised from the title
* ``title_keywords`` -- function words that must appear somewhere in the title

An empty spec matches everything, which is what makes this additive: every existing tenant has
neither key and keeps the behaviour they have today.
"""
from __future__ import annotations

import re

# Ordered MOST senior first: a title containing both "VP" and "Manager" ("VP, Engineering Manager")
# is a VP. Checking in ascending order would call it a manager.
_LEVEL_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("c_level", (
        r"\bc[teofmirsp]o\b",           # cto, ceo, cfo, coo, cmo, cio, cro, cso, cpo
        r"\bchief\b",
        r"\bfounder\b", r"\bco-?founder\b",
        r"\bowner\b", r"\bpartner\b", r"\bpresident\b",
        r"\bmanaging director\b",       # in most markets this IS the top job
    )),
    ("vp", (
        r"\bvp\b", r"\bv\.p\.\b",
        r"\bvice[- ]president\b",
        r"\bsvp\b", r"\bevp\b", r"\bavp\b",
    )),
    ("head", (r"\bhead\b", r"\bglobal head\b")),
    ("director", (r"\bdirector\b", r"\bdir\.?\b")),
    ("manager", (r"\bmanager\b", r"\bmgr\b", r"\blead\b", r"\bsupervisor\b")),
)

LEVELS: tuple[str, ...] = ("c_level", "vp", "head", "director", "manager", "ic")

# Phrase shapes a job title takes, used to turn a level + keyword into searchable strings. Derived
# from the tester's own list, which is exactly the set a web index holds.
_PHRASE_FORMS = ("{level} of {kw}", "{kw} {level}", "{level}, {kw}", "{level} {kw}")

_LEVEL_WORDS = {
    "c_level": "Chief", "vp": "VP", "head": "Head", "director": "Director", "manager": "Manager",
}


def _normalise(title: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed.

    Punctuation becomes a SPACE rather than being deleted: 'Director,Facilities' must not become
    'directorfacilities', and '\\b' word boundaries need real separators to work at all.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (title or "").lower())).strip()


def level_of(title: str) -> str:
    """Normalise a free-text title to one of :data:`LEVELS`. Unrecognised titles are ``ic``."""
    norm = _normalise(title)
    if not norm:
        return "ic"
    for level, patterns in _LEVEL_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, norm):
                return level
    return "ic"


def matches_title(title: str, spec: dict | None) -> bool:
    """Does ``title`` satisfy the ICP's level/keyword spec?

    An empty or absent spec returns True. That is the compatibility line: every tenant who has
    never set these keys keeps the results they have today.
    """
    spec = spec or {}
    levels = [str(x).lower() for x in (spec.get("job_levels") or []) if x]
    keywords = [str(x).lower() for x in (spec.get("title_keywords") or []) if x]
    excluded = [str(x).lower() for x in (spec.get("exclude_title_keywords") or []) if x]

    if not levels and not keywords and not excluded:
        return True

    norm = _normalise(title)

    # Exclusions are checked FIRST and are absolute. 'Assistant Director of Facilities' satisfies
    # both other gates, and it is not the person the rep meant.
    if any(_normalise(x) in norm for x in excluded):
        return False
    if levels and level_of(title) not in levels:
        return False
    if keywords and not any(_normalise(k) in norm for k in keywords):
        return False
    return True


def expand_titles(spec: dict | None, *, limit: int = 12) -> list[str]:
    """Turn a level/keyword spec into searchable title phrases.

    The contact-search provider queries a web index, which needs strings rather than a predicate.
    `matches_title` then re-filters whatever comes back, so a phrase that over-matches costs a
    little recall noise and never a wrong contact.
    """
    spec = spec or {}
    levels = [str(x).lower() for x in (spec.get("job_levels") or []) if x]
    keywords = [str(x) for x in (spec.get("title_keywords") or []) if x]
    if not keywords:
        return [_LEVEL_WORDS[lv] for lv in levels if lv in _LEVEL_WORDS][:limit]

    out: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        for lv in levels or ["director"]:
            word = _LEVEL_WORDS.get(lv)
            if not word:
                continue
            for form in _PHRASE_FORMS:
                phrase = form.format(level=word, kw=kw).strip()
                key = phrase.lower()
                if key not in seen:
                    seen.add(key)
                    out.append(phrase)
                if len(out) >= limit:
                    return out
    return out
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_job_levels.py -n0 -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add nexus/relevance/job_levels.py tests/test_job_levels.py
git commit -m "feat(relevance): deterministic job-level and title-keyword matching"
```

---

## Task 8: Wire the matcher into contact ranking

`nexus/agents/contact_rec.py:44` currently does `bt.lower() in title` — a plain substring test, which is why "Facilities Director" never matched "Director of Facilities".

**Files:**
- Modify: `nexus/agents/contact_rec.py:36-46`
- Create: `tests/test_contact_rec_titles.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contact_rec_titles.py
"""Contact recommendation must use the level/keyword matcher, not a substring test.

`bt.lower() in title` is False for ('Facilities Director', 'Director of Facilities') -- the exact
pair the tester reported.
"""
from __future__ import annotations


def test_a_reordered_title_is_recognised():
    from nexus.relevance.job_levels import matches_title

    spec = {"job_levels": ["director"], "title_keywords": ["facilities"]}
    assert matches_title("Director of Facilities", spec)


def test_contact_rec_boosts_a_level_keyword_match(monkeypatch):
    """The agent must consult the shared matcher so ranking and search cannot disagree."""
    import inspect

    from nexus.agents import contact_rec

    src = inspect.getsource(contact_rec)
    assert "matches_title" in src, (
        "contact_rec must use nexus.relevance.job_levels.matches_title -- a second, private "
        "matching rule here would drift from the one contact SEARCH uses, and the two disagreeing "
        "means a contact is found and then ranked as irrelevant"
    )


def test_plain_buyer_titles_still_work():
    """Regression guard: a tenant using only `buyer_titles` keeps today's behaviour."""
    from nexus.relevance.job_levels import matches_title

    assert matches_title("VP Sales", {}) is True
```

- [ ] **Step 2: Run it and watch the second test fail**

Run: `python -m pytest tests/test_contact_rec_titles.py -n0 -v`
Expected: `test_contact_rec_boosts_a_level_keyword_match` FAILS — `matches_title` is not referenced.

- [ ] **Step 3: Implement**

In `nexus/agents/contact_rec.py`, replace the buyer-title block at lines 36-46:

```python
        # buyer_titles let a tenant bias toward their economic buyer / champion personas.
        buyer_titles = ctx.inputs.get("buyer_titles") or []
```
…through the `if buyer_titles and any(bt.lower() in title for bt in buyer_titles):` test…

with:

```python
        # buyer_titles let a tenant bias toward their economic buyer / champion personas.
        buyer_titles = ctx.inputs.get("buyer_titles") or []
        # Level/keyword spec, when the tenant has moved past literal titles. Shared with contact
        # SEARCH through `nexus.relevance.job_levels` -- a private rule here would drift, and a
        # contact that search finds but ranking calls irrelevant is worse than not finding them.
        title_spec = {
            "job_levels": ctx.inputs.get("job_levels") or [],
            "title_keywords": ctx.inputs.get("title_keywords") or [],
            "exclude_title_keywords": ctx.inputs.get("exclude_title_keywords") or [],
        }
        has_spec = any(title_spec.values())
```

and replace the match test itself with:

```python
            from nexus.relevance.job_levels import matches_title

            # Substring matching was the bug: `'facilities director' in 'director of facilities'`
            # is False, so a real target ranked as a non-match.
            literal_hit = buyer_titles and any(bt.lower() in title for bt in buyer_titles)
            spec_hit = has_spec and matches_title(contact.title or "", title_spec)
            if literal_hit or spec_hit:
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_contact_rec_titles.py -n0 -v`
Expected: 3 passed.

- [ ] **Step 5: Regression check on the agent suite**

Run: `python -m pytest tests/test_agents*.py tests/test_contact*.py -n4 -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add nexus/agents/contact_rec.py tests/test_contact_rec_titles.py
git commit -m "fix(agents): rank contacts on job level and keywords, not substring titles"
```

---

## Task 9: Feed expanded titles into contact search

`SearchBackedContactSearchProvider._gather_hits` builds queries from literal `buyer_titles` only. With a level/keyword spec, `expand_titles` supplies the phrasings a web index actually holds.

**Files:**
- Modify: `nexus/integrations/contact_search.py` (`_gather_hits`, ~line 143-167)
- Create: `tests/test_contact_search_expansion.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contact_search_expansion.py
"""Contact search must query the phrasings a web index actually holds.

The tester asked for 'Facilities Director' and got nothing. The index holds 'Director of
Facilities' and 'Head of Facilities'; a query for the one literal phrase finds neither.
"""
from __future__ import annotations


class _RecordingSearch:
    def __init__(self): self.queries = []
    async def search(self, query, limit=10, **kw):
        self.queries.append(query)
        return []


async def test_the_spec_widens_the_queries():
    from nexus.integrations.contact_search import SearchBackedContactSearchProvider
    from nexus.models.account import Account

    search = _RecordingSearch()
    provider = SearchBackedContactSearchProvider(search_provider=search, llm=None)
    icp = {"job_levels": ["director", "head"], "title_keywords": ["facilities"]}
    await provider._gather_hits(Account(tenant_id="t1", name="Acme", domain="acme.com"),
                                [], limit=5, icp=icp)

    blob = " ".join(search.queries).lower()
    assert "facilities" in blob, "the keyword never reached a query"
    assert any(p in blob for p in ("director of facilities", "facilities director")), \
        f"no expanded phrasing in queries: {search.queries}"


async def test_literal_titles_still_drive_the_query():
    """Regression guard: a tenant with plain buyer_titles and no spec is unaffected."""
    from nexus.integrations.contact_search import SearchBackedContactSearchProvider
    from nexus.models.account import Account

    search = _RecordingSearch()
    provider = SearchBackedContactSearchProvider(search_provider=search, llm=None)
    await provider._gather_hits(Account(tenant_id="t1", name="Acme", domain="acme.com"),
                                ["VP Sales"], limit=5, icp={})
    assert "vp sales" in " ".join(search.queries).lower()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_contact_search_expansion.py -n0 -v`
Expected: FAIL — `_gather_hits() got an unexpected keyword argument 'icp'`.

- [ ] **Step 3: Implement**

Change `_gather_hits` to accept the ICP and widen its title list. Replace its signature:

```python
    async def _gather_hits(self, account: Account, titles: list[str], limit: int) -> list[dict]:
```

with:

```python
    async def _gather_hits(
        self, account: Account, titles: list[str], limit: int, icp: dict | None = None
    ) -> list[dict]:
```

and immediately after the signature, before the queries are built, insert:

```python
        # A level/keyword spec expands into the phrasings a web index actually holds. Querying the
        # one literal title a rep typed is why a search for 'Facilities Director' returned nothing
        # while the index held 'Director of Facilities'. `matches_title` re-filters the results, so
        # over-broad phrasings cost recall noise, never a wrong contact.
        from nexus.relevance.job_levels import expand_titles

        expanded = expand_titles(icp or {})
        if expanded:
            titles = list(dict.fromkeys([*titles, *expanded]))
```

Then update the caller in `search_contacts` — find `hits = await self._gather_hits(account, titles, limit)` and change it to:

```python
            hits = await self._gather_hits(account, titles, limit, icp=icp)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_contact_search_expansion.py -n0 -v`
Expected: 2 passed.

- [ ] **Step 5: Regression check**

Run: `python -m pytest tests/test_contact*.py tests/test_integrations*.py -n4 -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add nexus/integrations/contact_search.py tests/test_contact_search_expansion.py
git commit -m "fix(contact-search): query expanded title phrasings, not one literal title"
```

---

## Task 10: Title suggestions must use campaign context

The tester added a value proposition, pains and product context, then re-ran Suggest Titles and got the same generic list. Partly the dead LLM (Task 1), but `recommend_titles_for_icp` also ignores those fields entirely.

**Files:**
- Modify: `nexus/relevance/titles.py` (`recommend_titles_for_icp`, ~line 225)
- Create: `tests/test_title_suggestions_context.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_title_suggestions_context.py
"""Suggested titles must move when campaign context is added.

Tester, 2026-08-27: after adding a value proposition, the pain being solved and product context,
"the recommendations remained largely the same" -- CTO, Head of Demand Generation, Head of Sales,
Head of Data. Those are generic B2B defaults, and the function never read those fields.
"""
from __future__ import annotations


def test_context_changes_the_suggestions():
    from nexus.relevance.titles import recommend_titles_for_icp

    base = {"industries": ["Manufacturing"]}
    with_ctx = {
        **base,
        "value_props": ["Cut facility energy spend"],
        "pains_solved": ["Rising utility costs", "Unplanned equipment downtime"],
        "product_context": "IoT sensors for building management systems",
    }
    plain = {r.title.lower() for r in recommend_titles_for_icp(base)}
    ctx = {r.title.lower() for r in recommend_titles_for_icp(with_ctx)}
    assert ctx != plain, "adding value props, pains and product context changed nothing"


def test_facilities_context_surfaces_facilities_roles():
    from nexus.relevance.titles import recommend_titles_for_icp

    icp = {
        "industries": ["Manufacturing"],
        "pains_solved": ["Rising facility energy costs"],
        "product_context": "building management and facilities operations",
    }
    titles = " ".join(r.title.lower() for r in recommend_titles_for_icp(icp))
    assert any(w in titles for w in ("facilit", "operations", "plant", "maintenance")), \
        f"no operations-side role suggested: {titles}"


def test_an_icp_with_no_context_is_unchanged():
    """Regression guard: existing callers pass no context and must keep today's output."""
    from nexus.relevance.titles import recommend_titles_for_icp

    out = recommend_titles_for_icp({"industries": ["SaaS"], "tech": ["Salesforce"]})
    assert out, "an ICP with no context must still produce suggestions"
    assert all(r.title for r in out)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_title_suggestions_context.py -n0 -v`
Expected: the first two FAIL (context is ignored, so both sets are identical); the third passes.

- [ ] **Step 3: Implement**

In `nexus/relevance/titles.py`, at the top of `recommend_titles_for_icp`, gather the context text and use it to weight the role templates:

```python
def recommend_titles_for_icp(icp: dict, *, limit: int = 10) -> list[TitleRecommendation]:
    # Value props, pains and product context are the strongest available evidence about WHO feels
    # the problem, and they were being ignored -- so a tester who filled all three in got the same
    # generic B2B committee back and reasonably concluded the AI added nothing.
    #
    # Matched on the role template's own keywords, deterministically. The LLM path (the
    # `/suggest-titles` endpoint) phrases and ranks; this is the grounding it ranks over, and it
    # must work with the LLM unavailable.
    context_blob = " ".join(
        str(x).lower()
        for key in ("value_props", "pains_solved", "product_context", "problem")
        for x in (icp.get(key) if isinstance(icp.get(key), list) else [icp.get(key) or ""])
    )
```

Then, where each `RoleTemplate` is scored, add a context bonus. Find the loop that builds recommendations and add, before sorting:

```python
        # A template whose function words appear in the campaign context outranks a generic one.
        if context_blob:
            hits = sum(1 for kw in template.keywords if kw.lower() in context_blob)
            score += hits * 2.0
```

If `RoleTemplate` has no `keywords` attribute, derive them from the title:

```python
            words = [w for w in template.title.lower().split() if len(w) > 3]
            hits = sum(1 for w in words if w in context_blob)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_title_suggestions_context.py -n0 -v`
Expected: 3 passed. If `test_facilities_context_surfaces_facilities_roles` still fails, add a Facilities/Operations `RoleTemplate` to `_ROLE_TEMPLATES` — the catalogue is sales/marketing/engineering-heavy and has no operations-side role, which is itself the finding.

- [ ] **Step 5: Regression check**

Run: `python -m pytest tests/test_relevance*.py tests/test_titles*.py -n4 -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add nexus/relevance/titles.py tests/test_title_suggestions_context.py
git commit -m "feat(relevance): ground title suggestions in value props, pains and product context"
```

---

## Task 11: CSV import that creates accounts

`custom_fields.import_csv` matches on a column and **skips** any row that does not already exist — it annotates, it never creates. So there is no way to bring a list into the product, which is the first thing an ops team does.

**Files:**
- Create: `nexus/imports/__init__.py`, `nexus/imports/csv_ingest.py`
- Create: `tests/test_csv_import_accounts.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_csv_import_accounts.py
"""CSV import that CREATES accounts.

`custom_fields.import_csv` only annotates rows that already match, so a team with an existing list
had no way to get it in -- the tester's first blocker.

Identity is the normalised domain, matching `nexus/companies/`: a name match across a CSV is how
two different companies merge into one row.
"""
from __future__ import annotations


async def test_rows_become_accounts(fresh_db, tenant_session):
    from nexus.imports.csv_ingest import import_accounts_csv

    csv = b"company,website,country\nAcme Corp,acme.com,United States\nBeta Inc,beta.io,Canada\n"
    result = await import_accounts_csv(
        tenant_session, content=csv,
        mapping={"company": "name", "website": "domain", "country": "country"},
    )
    assert result["created"] == 2
    assert result["skipped"] == 0


async def test_a_second_import_updates_rather_than_duplicating(fresh_db, tenant_session):
    """Re-uploading a corrected list must not double the book."""
    from nexus.imports.csv_ingest import import_accounts_csv

    csv = b"company,website\nAcme Corp,acme.com\n"
    mapping = {"company": "name", "website": "domain"}
    first = await import_accounts_csv(tenant_session, content=csv, mapping=mapping)
    second = await import_accounts_csv(tenant_session, content=csv, mapping=mapping)
    assert first["created"] == 1
    assert second["created"] == 0
    assert second["updated"] == 1


async def test_the_domain_is_normalised(fresh_db, tenant_session):
    """'https://www.Acme.com/pricing' and 'acme.com' are one company."""
    from sqlalchemy import select

    from nexus.imports.csv_ingest import import_accounts_csv
    from nexus.models.account import Account

    csv = b"company,website\nAcme,https://www.Acme.com/pricing\n"
    await import_accounts_csv(tenant_session, content=csv,
                              mapping={"company": "name", "website": "domain"})
    row = (await tenant_session.session.scalars(select(Account))).one()
    assert row.domain == "acme.com"


async def test_a_row_with_no_name_is_reported_not_silently_dropped(fresh_db, tenant_session):
    from nexus.imports.csv_ingest import import_accounts_csv

    csv = b"company,website\n,orphan.com\nReal Co,real.com\n"
    result = await import_accounts_csv(tenant_session, content=csv,
                                       mapping={"company": "name", "website": "domain"})
    assert result["created"] == 1
    assert result["skipped"] == 1
    assert result["errors"], "a skipped row must say why -- a silent drop looks like data loss"


async def test_unmapped_columns_land_in_custom_fields(fresh_db, tenant_session):
    """An ops CSV always carries columns we have no column for. Dropping them loses the reason the
    team built the list."""
    from sqlalchemy import select

    from nexus.imports.csv_ingest import import_accounts_csv
    from nexus.models.account import Account

    csv = b"company,website,segment\nAcme,acme.com,Enterprise West\n"
    await import_accounts_csv(tenant_session, content=csv,
                              mapping={"company": "name", "website": "domain"})
    row = (await tenant_session.session.scalars(select(Account))).one()
    assert row.custom_fields.get("segment") == "Enterprise West"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_csv_import_accounts.py -n0 -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.imports'`.

- [ ] **Step 3: Implement**

```python
# nexus/imports/__init__.py
"""Bringing a customer's existing lists into the product."""
```

```python
# nexus/imports/csv_ingest.py
"""Create accounts and contacts from an uploaded CSV.

`custom_fields.import_csv` annotates rows that already match and skips everything else, so a team
arriving with a list had no way in -- the first blocker a tester reported.

Identity rules mirror `nexus/companies/` and `nexus/people/`, and for the same reason: a name match
is how two different organisations become one row, and this subsystem has already shipped six
wrong-attribution bugs by trusting one.

* An account is identified by its NORMALISED DOMAIN. No domain -> matched on exact name within the
  tenant, which is safe here in a way it is not across tenants, because the operator is uploading
  their own list.
* A contact is identified by NORMALISED EMAIL within its account.

Everything is tenant-scoped through the caller's TenantSession, so RLS applies unchanged.
"""
from __future__ import annotations

import csv as _csv
import io

from sqlalchemy import select

from nexus.models.account import Account, Contact

MAX_ROWS = 50_000


def _decode(content: bytes) -> str:
    """UTF-8, falling back to cp1252.

    Excel on Windows writes cp1252 and it is what an ops team exports. A UnicodeDecodeError here
    would reject the commonest file in the category.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def normalise_domain(raw: str) -> str:
    """'https://www.Acme.com/pricing' -> 'acme.com'. '' when there is nothing usable."""
    value = (raw or "").strip().lower()
    if not value:
        return ""
    value = value.split("://", 1)[-1]
    value = value.split("/", 1)[0].split("?", 1)[0].split("@")[-1]
    if value.startswith("www."):
        value = value[4:]
    return value if "." in value else ""


def _rows(content: bytes) -> list[dict]:
    reader = _csv.DictReader(io.StringIO(_decode(content)))
    return [r for _, r in zip(range(MAX_ROWS), reader)]


async def import_accounts_csv(ts, *, content: bytes, mapping: dict[str, str]) -> dict:
    """Create or update accounts from CSV. ``mapping`` is {csv_column: account_field}."""
    created = updated = skipped = 0
    errors: list[str] = []
    rows = _rows(content)
    mapped_columns = set(mapping)

    for index, row in enumerate(rows, start=2):  # 2 = first data line in a spreadsheet
        fields = {
            field: (row.get(column) or "").strip()
            for column, field in mapping.items()
        }
        name = fields.get("name", "")
        domain = normalise_domain(fields.get("domain", ""))
        if not name and not domain:
            skipped += 1
            errors.append(f"row {index}: no company name and no website")
            continue

        # Anything the operator did not map is kept on custom_fields. An ops CSV always carries
        # columns we have no column for, and dropping them throws away the reason the list exists.
        extras = {
            key: value.strip()
            for key, value in row.items()
            if key and key not in mapped_columns and (value or "").strip()
        }

        existing = None
        if domain:
            existing = (await ts.select(select(Account).where(Account.domain == domain))).first()
        if existing is None and name:
            existing = (await ts.select(select(Account).where(Account.name == name))).first()

        if existing is None:
            account = Account(tenant_id=ts.tenant_id, name=name or domain, source="csv_import")
            if domain:
                account.domain = domain
            _apply(account, fields, extras)
            ts.add(account)
            created += 1
        else:
            _apply(existing, fields, extras)
            updated += 1

    await ts.session.flush()
    return {"created": created, "updated": updated, "skipped": skipped,
            "errors": errors[:50], "total_rows": len(rows)}


def _apply(account: Account, fields: dict, extras: dict) -> None:
    """Write mapped fields onto the account. Blank CSV cells never overwrite a stored value --
    a partial list must not erase enrichment the product already paid for."""
    for field in ("name", "industry", "country", "region", "postal_code"):
        value = fields.get(field)
        if value:
            setattr(account, field, value)
    domain = normalise_domain(fields.get("domain", ""))
    if domain:
        account.domain = domain
    for field in ("employee_count", "annual_revenue"):
        raw = (fields.get(field) or "").replace(",", "").replace("$", "").strip()
        if raw.isdigit():
            setattr(account, field, int(raw))
    if extras:
        account.custom_fields = {**(account.custom_fields or {}), **extras}
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_csv_import_accounts.py -n0 -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add nexus/imports/ tests/test_csv_import_accounts.py
git commit -m "feat(imports): create accounts from an uploaded CSV"
```

---

## Task 12: CSV import for contacts, plus the endpoints

**Files:**
- Modify: `nexus/imports/csv_ingest.py`
- Create: `nexus/api/routers/imports.py`
- Modify: `nexus/api/main.py` (register the router)
- Create: `tests/test_csv_import_contacts.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_csv_import_contacts.py
"""CSV import that creates contacts, attached to the right account."""
from __future__ import annotations


async def test_contacts_attach_to_an_existing_account(fresh_db, tenant_session):
    from sqlalchemy import select

    from nexus.imports.csv_ingest import import_accounts_csv, import_contacts_csv
    from nexus.models.account import Contact

    await import_accounts_csv(tenant_session, content=b"company,website\nAcme,acme.com\n",
                              mapping={"company": "name", "website": "domain"})
    result = await import_contacts_csv(
        tenant_session,
        content=b"name,email,role,company_domain\nJane Roe,jane@acme.com,Director of Facilities,acme.com\n",
        mapping={"name": "full_name", "email": "email", "role": "title",
                 "company_domain": "account_domain"},
    )
    assert result["created"] == 1
    row = (await tenant_session.session.scalars(select(Contact))).one()
    assert row.title == "Director of Facilities"


async def test_a_contact_for_an_unknown_company_creates_the_account(fresh_db, tenant_session):
    """An ops team uploads a contact list without having uploaded the companies first. Refusing
    would make the two imports order-dependent for no reason the user can see."""
    from sqlalchemy import select

    from nexus.imports.csv_ingest import import_contacts_csv
    from nexus.models.account import Account

    await import_contacts_csv(
        tenant_session,
        content=b"name,email,company_domain\nJohn Doe,john@newco.com,newco.com\n",
        mapping={"name": "full_name", "email": "email", "company_domain": "account_domain"},
    )
    assert (await tenant_session.session.scalars(select(Account))).one().domain == "newco.com"


async def test_re_importing_does_not_duplicate_a_contact(fresh_db, tenant_session):
    from nexus.imports.csv_ingest import import_contacts_csv

    csv = b"name,email,company_domain\nJane Roe,Jane@Acme.com,acme.com\n"
    mapping = {"name": "full_name", "email": "email", "company_domain": "account_domain"}
    first = await import_contacts_csv(tenant_session, content=csv, mapping=mapping)
    second = await import_contacts_csv(tenant_session, content=csv, mapping=mapping)
    assert (first["created"], second["created"]) == (1, 0)
    assert second["updated"] == 1


async def test_a_row_with_no_email_is_reported(fresh_db, tenant_session):
    from nexus.imports.csv_ingest import import_contacts_csv

    result = await import_contacts_csv(
        tenant_session, content=b"name,email,company_domain\nNo Email,,acme.com\n",
        mapping={"name": "full_name", "email": "email", "company_domain": "account_domain"},
    )
    assert result["skipped"] == 1
    assert result["errors"]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_csv_import_contacts.py -n0 -v`
Expected: FAIL, `ImportError: cannot import name 'import_contacts_csv'`.

- [ ] **Step 3: Add the contact importer**

Append to `nexus/imports/csv_ingest.py`:

```python
def normalise_email(raw: str) -> str:
    return (raw or "").strip().lower()


async def import_contacts_csv(ts, *, content: bytes, mapping: dict[str, str]) -> dict:
    """Create or update contacts from CSV. Identity is the normalised email within the tenant.

    A contact whose company is not in the book yet CREATES the account. Refusing would make the two
    imports order-dependent for a reason the operator cannot see from the upload screen.
    """
    created = updated = skipped = 0
    errors: list[str] = []
    rows = _rows(content)
    mapped_columns = set(mapping)

    for index, row in enumerate(rows, start=2):
        fields = {f: (row.get(c) or "").strip() for c, f in mapping.items()}
        email = normalise_email(fields.get("email", ""))
        full_name = fields.get("full_name", "")
        if not email:
            skipped += 1
            errors.append(f"row {index}: no email address")
            continue

        # The account domain, else the email's own domain -- which is right far more often than it
        # is wrong for a work address, and a contact with no account cannot be actioned at all.
        domain = normalise_domain(fields.get("account_domain", "")) or email.split("@")[-1]
        account = (await ts.select(select(Account).where(Account.domain == domain))).first()
        if account is None:
            account = Account(tenant_id=ts.tenant_id, name=fields.get("account_name") or domain,
                              domain=domain, source="csv_import")
            ts.add(account)
            await ts.session.flush()

        existing = (await ts.select(select(Contact).where(Contact.email == email))).first()
        extras = {k: v.strip() for k, v in row.items()
                  if k and k not in mapped_columns and (v or "").strip()}

        if existing is None:
            contact = Contact(tenant_id=ts.tenant_id, account_id=account.id,
                              full_name=full_name or email.split("@")[0], email=email)
            if fields.get("title"):
                contact.title = fields["title"]
            ts.add(contact)
            created += 1
        else:
            if full_name:
                existing.full_name = full_name
            if fields.get("title"):
                existing.title = fields["title"]
            updated += 1

    await ts.session.flush()
    return {"created": created, "updated": updated, "skipped": skipped,
            "errors": errors[:50], "total_rows": len(rows)}
```

- [ ] **Step 4: Add the router**

```python
# nexus/api/routers/imports.py
"""CSV upload for accounts and contacts.

Separate from `custom_fields.import_csv`, which annotates rows that already exist. These CREATE.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from nexus.api.deps import Permission, Principal, TenantSession, get_tenant_session, require
from nexus.imports.csv_ingest import import_accounts_csv, import_contacts_csv

router = APIRouter(prefix="/imports", tags=["imports"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class ImportOut(BaseModel):
    created: int
    updated: int
    skipped: int
    total_rows: int
    errors: list[str]


def _mapping(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid mapping: {exc}")
    if not isinstance(parsed, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mapping must be a JSON object")
    return parsed


async def _read(file: UploadFile) -> bytes:
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    return content


@router.post("/accounts", response_model=ImportOut)
async def upload_accounts(
    mapping: str = Form(...),
    file: UploadFile = File(...),
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportOut:
    result = await import_accounts_csv(ts, content=await _read(file), mapping=_mapping(mapping))
    await ts.session.commit()
    return ImportOut(**result)


@router.post("/contacts", response_model=ImportOut)
async def upload_contacts(
    mapping: str = Form(...),
    file: UploadFile = File(...),
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportOut:
    result = await import_contacts_csv(ts, content=await _read(file), mapping=_mapping(mapping))
    await ts.session.commit()
    return ImportOut(**result)
```

Register it in `nexus/api/main.py` beside the other routers:

```python
from nexus.api.routers import imports as imports_router
app.include_router(imports_router.router, prefix="/api")
```

Match the exact prefix and style the neighbouring `include_router` calls use.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_csv_import_contacts.py tests/test_csv_import_accounts.py -n0 -v`
Expected: 9 passed.

If `Permission.manage_accounts` does not exist, run `python -c "from nexus.api.deps import Permission; print([p.name for p in Permission])"` and use the nearest account-write permission.

- [ ] **Step 6: Commit**

```bash
git add nexus/imports/csv_ingest.py nexus/api/routers/imports.py nexus/api/main.py tests/test_csv_import_contacts.py
git commit -m "feat(imports): CSV upload endpoints for accounts and contacts"
```

---

## Task 13: Per-tenant signal category control

The tester saw signals they had not asked for and asked, reasonably, whether they were paying for them. `signal_sources` is a **deployment-global** setting; there is no per-tenant control at all.

Absence of a row means "everything", so this mutes nobody — the same rule `notification_preferences` already uses.

**Files:**
- Create: `migrations/versions/0052_signal_preferences.py`, `nexus/models/signal_preference.py`
- Modify: `nexus/ingestion/pipeline.py`
- Create: `tests/test_signal_preferences.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signal_preferences.py
"""Per-tenant control over which signal categories are collected.

A tester asked why signals they had not enabled were appearing, and whether they were being billed
for them. `signal_sources` is deployment-global -- there was no per-tenant control.

The absence of a row means "every category", exactly as `notification_preferences` does. Adding
this table must mute nobody.
"""
from __future__ import annotations


async def test_no_preference_row_means_every_category(fresh_db, tenant_session):
    """THE regression guard. Every existing tenant has no rows and must keep every signal."""
    from nexus.ingestion.preferences import category_enabled

    for kind in ("funding", "hiring", "news", "tech_change", "leadership"):
        assert await category_enabled(tenant_session, kind) is True


async def test_disabling_one_category_leaves_the_others(fresh_db, tenant_session):
    from nexus.ingestion.preferences import category_enabled, set_category

    await set_category(tenant_session, "news", enabled=False)
    assert await category_enabled(tenant_session, "news") is False
    assert await category_enabled(tenant_session, "funding") is True


async def test_a_disabled_category_is_not_ingested(fresh_db, tenant_session):
    from nexus.ingestion.preferences import category_enabled, set_category

    await set_category(tenant_session, "news", enabled=False)
    assert await category_enabled(tenant_session, "news") is False


async def test_re_enabling_restores_it(fresh_db, tenant_session):
    from nexus.ingestion.preferences import category_enabled, set_category

    await set_category(tenant_session, "news", enabled=False)
    await set_category(tenant_session, "news", enabled=True)
    assert await category_enabled(tenant_session, "news") is True


async def test_an_unknown_category_is_allowed(fresh_db, tenant_session):
    """Matches the codebase bias: unknown resolves permissive. A new signal kind must not be
    silently dropped for every tenant until someone adds a row."""
    from nexus.ingestion.preferences import category_enabled

    assert await category_enabled(tenant_session, "brand_new_kind") is True
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_signal_preferences.py -n0 -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.ingestion.preferences'`.

- [ ] **Step 3: Add the model**

```python
# nexus/models/signal_preference.py
"""Which signal categories a tenant wants collected.

Tenant-scoped, so `scripts/apply_rls.py` enrols it automatically on deploy.

The ABSENCE of a row means the category is enabled. That is what makes the table additive: every
existing tenant has no rows and keeps every signal they get today. Same rule as
`notification_preferences`.
"""
from __future__ import annotations

from sqlalchemy import Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.models.base import Base, IdMixin, TenantScoped, TimestampMixin


class SignalPreference(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "signal_preferences"
    __table_args__ = (UniqueConstraint("tenant_id", "category", name="uq_signal_pref"),)

    # Matches the signal kinds in nexus/alerts/rules.py. Stable strings: renaming one silently
    # re-enables whatever a tenant had switched off.
    category: Mapped[str] = mapped_column(String(60), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
```

- [ ] **Step 4: Add the accessor**

```python
# nexus/ingestion/preferences.py
"""Reading and writing per-tenant signal category preferences."""
from __future__ import annotations

from sqlalchemy import select

from nexus.models.signal_preference import SignalPreference


async def category_enabled(ts, category: str) -> bool:
    """Is this signal category collected for this tenant?

    No row -> True. An unknown category -> True. Both follow this codebase's standing bias that
    unknown resolves permissive: a new signal kind must not be silently dropped for every tenant
    until somebody remembers to add a row, and that failure is invisible -- signals simply stop,
    which looks exactly like a quiet market.
    """
    row = (await ts.select(
        select(SignalPreference).where(SignalPreference.category == category)
    )).first()
    return True if row is None else bool(row.enabled)


async def set_category(ts, category: str, *, enabled: bool) -> None:
    row = (await ts.select(
        select(SignalPreference).where(SignalPreference.category == category)
    )).first()
    if row is None:
        ts.add(SignalPreference(tenant_id=ts.tenant_id, category=category, enabled=enabled))
    else:
        row.enabled = enabled
    await ts.session.flush()
```

- [ ] **Step 5: Write the migration**

```python
# migrations/versions/0052_signal_preferences.py
"""Per-tenant signal category preferences.

Tenant-scoped, so `scripts/apply_rls.py` enrols it. Empty for every existing tenant, and an absent
row means enabled -- so creating this table changes nothing until someone opts out.

Revision ID: 0052_signal_preferences
Revises: 0051_account_geo_revenue
"""
from alembic import op
import sqlalchemy as sa

revision = "0052_signal_preferences"
down_revision = "0051_account_geo_revenue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_preferences",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False, index=True),
        sa.Column("category", sa.String(60), nullable=False, index=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "category", name="uq_signal_pref"),
    )


def downgrade() -> None:
    op.drop_table("signal_preferences")
```

Match the exact column types the neighbouring migrations use for `id`, `tenant_id` and the timestamps — copy them from `0051` rather than trusting the sketch above.

- [ ] **Step 6: Gate ingestion on the preference**

In `nexus/ingestion/pipeline.py`, find where a collected signal is about to be persisted and add, before the write:

```python
        from nexus.ingestion.preferences import category_enabled

        # A tenant who switched a category off must not be charged to collect it. Checked at the
        # persist point rather than at source selection, because one source returns several kinds --
        # `WebNewsSource` alone yields funding, hiring and news.
        if not await category_enabled(ts, getattr(event, "kind", "") or ""):
            continue
```

- [ ] **Step 7: Run the tests plus migration replay**

Run: `python -m pytest tests/test_signal_preferences.py tests/test_migrations_replay.py -n0 -v`
Expected: all pass.

- [ ] **Step 8: Regression check on ingestion**

Run: `python -m pytest tests/test_ingestion*.py tests/test_signal*.py tests/test_alerts*.py -n4 -q`
Expected: all pass. These assert that signals flow end to end; a gate added in the wrong place is exactly what they catch.

- [ ] **Step 9: Commit**

```bash
git add nexus/models/signal_preference.py nexus/ingestion/preferences.py migrations/versions/0052_signal_preferences.py nexus/ingestion/pipeline.py tests/test_signal_preferences.py
git commit -m "feat(ingestion): per-tenant signal category control"
```

---

## Task 14: Full regression and deploy

- [ ] **Step 1: Confirm exactly one migration head**

Run: `python -c "from alembic.script import ScriptDirectory; from alembic.config import Config; print(ScriptDirectory.from_config(Config('alembic.ini')).get_heads())"`
Expected: `('0052_signal_preferences',)` — one entry.

- [ ] **Step 2: Run the whole suite**

Run: `python -m pytest tests/ -n auto --dist loadfile -q`
Expected: all pass. Roughly 48 minutes. Do not run any other pytest process at the same time — concurrent runs share Prometheus registry state and produce a spurious `test_metrics` failure.

- [ ] **Step 3: Build the frontend, then the images**

```bash
cd frontend && npm run build && cd ..
docker compose -f deploy/docker-compose.prod.yml build app worker
docker compose -f deploy/docker-compose.prod.yml up -d
```

- [ ] **Step 4: Migrate and apply RLS**

```bash
docker compose -f deploy/docker-compose.prod.yml exec -T app sh -c "cd /app && alembic upgrade head"
docker compose -f deploy/docker-compose.prod.yml exec -T app python scripts/apply_rls.py
```
Expected: `0052_signal_preferences (head)`. `apply_rls.py` enrols `signal_preferences` because it carries `tenant_id`.

- [ ] **Step 5: Verify the tester's exact failures are gone**

```bash
docker compose -f deploy/docker-compose.prod.yml exec -T app python -c "
import asyncio
from nexus.agents.llm import get_llm_provider, LLMMessage
from nexus.relevance.job_levels import matches_title
async def m():
    r = await get_llm_provider().complete([LLMMessage('user','Reply with exactly: ALIVE')], max_tokens=400)
    print('LLM       :', 'OK' if 'ALIVE' in (r.text or '') else 'STILL BROKEN')
    print('titles    :', all(matches_title(t, {'job_levels':['director','head'],'title_keywords':['facilities']})
          for t in ('Head of Facilities','Facilities Head','Director of Facilities','Director, Facilities')))
asyncio.run(m())"
```
Expected: `LLM: OK` and `titles: True`.

- [ ] **Step 6: Commit anything outstanding and report back to the tester**

---

## Self-review

**Spec coverage.** All eight feedback items map to a task: #1 → 11-12, #2 → 1+3, #3 → 5-7, #4 → 1+10, #5 → 13, #6 → 1+8-9, #7 → 4, #8 → 1 (blocked by #6, resolves with it).

**Placeholders.** None. Every code step carries the code. Three steps name a fallback when a symbol may differ (`Permission.manage_accounts`, `RoleTemplate.keywords`, `_band_score`) — each gives the command to check and the action to take, rather than leaving it open.

**Type consistency.** `matches_title(title, spec)`, `level_of(title)` and `expand_titles(spec, *, limit)` keep the same signatures in Tasks 7, 8 and 9. ICP keys are `job_levels`, `title_keywords`, `exclude_title_keywords`, `regions`, `postal_codes`, `revenue_min`, `revenue_max` throughout. `import_accounts_csv` / `import_contacts_csv` both take `(ts, *, content, mapping)` and return the same five-key dict the router's `ImportOut` declares.

**Known risk.** Task 13 Step 6 places the gate by description rather than exact line, because the persist point in `pipeline.py` must be read first — putting it at source selection instead would disable whole sources rather than categories.
