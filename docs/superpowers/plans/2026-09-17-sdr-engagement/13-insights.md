# Phase 13: Insights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An SDR sees how likely each prospect is to reply to them, what may be said about how that person replies, and when to send, on the screens where they choose people and approve emails; and the Today plan puts the likeliest people first within each kind of work.

**Architecture:** `nexus/engagement/insights/` holds three pure rule modules and two thin shells. `rules.py` decides what may be shown of a profile (D26), `likelihood.py` scores a reply from the viewing workspace's own fit and signals, `best_time.py` picks a send time. `client.py` reads profiles from the insights store built in phase 06, read-only, cached and never raising; `service.py` assembles all three for a batch of contacts in a fixed number of queries. One router serves a list of contacts and a campaign's best time. The screens add a badge to the review queue, the contact picker and the account page, and a best-time hint wherever a set send time is chosen.

**Tech Stack:** FastAPI, async SQLAlchemy, asyncpg (read-only store client), React + TypeScript, CSS Modules.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §18.3 (the insights store), §18.5 (what the app shows), §19 (Today ranked by likelihood), D25, D26. **Depends on:** phases 06 (ledger stores and the insights builder), 12 (the Today plan).

**Verified:** implemented on `feat/sdr-engagement` on top of phases 01–12. In the CI image: `tests/test_engagement_insights.py` (the display rules at, below and above the three-workspace line; nothing for a workspace that has not opted in; the company threshold; likelihood from the viewer's own fit and signals, and `unknown` when there is nothing to go on; responsiveness moving the answer only when a pattern is allowed; best time from the person, then the company, then the morning; the API answering with no store configured and refusing more than 100 ids; the routes dark with the engine), the new Today ranking test in `tests/test_engagement_reporting.py`, three structural checks in `tests/test_engagement_screens_ui.py`, and every engagement, tiering, outcome, analytics, rep-dashboard, metering-coverage, plan-gated-nav and credential-leak test together (355 passed); `tests_integration/test_ledger_stores_pg.py` against a throwaway Postgres 16 (7 passed; the client reads a profile and never selects `workspace_keys`); `ruff`, `tsc --noEmit`, `npm run build`. In the browser against the isolated preview, at phone width: the review queue showed "Likely to reply" for people at a strong-fit account with recent signals and "Less likely to reply" for a weak one, with the reasons on hover and to screen readers; a step switched to "At a set time" showed the best-time line; the account's Emails tab showed "How they reply"; no horizontal overflow.

---

## Decisions this phase makes

- **The display rules are applied on the server and nowhere else.** The client receives a sentence (`text`), a band, and for a pattern the weekday and hour; it never receives a workspace count, a key, a date or a sender. A structural test refuses `workspace_count` and `workspace_keys` anywhere in the engagement screens and in `types.ts`, so a later screen cannot start doing arithmetic the rules forbid.
- **The workspace keys never enter this process.** The store keeps them so the builder can count distinct workspaces; the client selects named columns that exclude them, and the integration test asserts the list. `SELECT *` would have carried them into memory, logs and tracebacks.
- **Likelihood is the viewer's own evidence**: 60% ICP fit (the latest `account_scores.composite`), 40% signals in the last 30 days (three is full), and a person's general responsiveness only when the rules already allow a pattern, as a 30% blend. Below three workspaces even a reply *rate* would say others have been writing to them. The weights are stated in the module, not learned.
- **No evidence is said by saying nothing.** A contact with no score, no recent signal and no allowed pattern gets `unknown`, and the badge renders nothing. A neutral fit alone scores 0.3, which labelled every prospect nobody had scored yet "Less likely to reply", a claim built from nothing; the preview showed it on the first load.
- **The badge says its band in words and lists its reasons**, on hover (`title`) and to screen readers (`aria-label`), so colour is never the only signal and "why does it say that?" has an answer on the spot.
- **Best time: the person's pattern, then the company's, then 09:30 in their morning.** A campaign's suggestion is the hour most of its people agree on, and only when at least two do; otherwise the morning. It is a hint beside a set-time step and in the move-one-person dialog, with a "Use" button; nothing moves on its own. "At the best time" (auto) already sends in the recipient's working morning, so the hint is only for someone choosing a time by hand. In the dialog the suggestion is in the contact's own zone and the input is the viewer's local time, so `zonedTime.ts` converts through `Intl` twice to land on the right hour across a daylight-saving change.
- **One request per list.** The review queue, the contact picker (its first 100 rows) and the account page each ask for their people in one call, capped at 100 ids (422 above). Per-row requests would be a hundred round trips on the screens an SDR opens most.
- **The client never raises and caches for five minutes, misses included.** An unconfigured, unreachable or failing store answers "no profile"; a review queue must not fail because a store in another cloud is slow. The builder runs on the worker's schedule, so five minutes is well inside how often a profile can change, and caching the unknown stops every render of a list of new prospects querying the store for each of them.
- **Today ranks within a kind by likelihood BAND, then age.** The kinds keep their order (the cost of leaving each thing, phase 12). Ranking by raw score would let a hundredth of a point of fit jump someone the queue and break the oldest-first order the reply-speed reminder measures; by band, people in one band stay oldest first. It applies to answers, decisions, calls and people returning; colleagues at an account and a campaign's review have no one person behind them and keep their age order. If insights cannot be read, everyone ranks `unknown`, which is phase 12's order.

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Create | `nexus/engagement/insights/__init__.py`, `rules.py`, `likelihood.py`, `best_time.py` | the pure rules |
| Create | `nexus/engagement/insights/client.py`, `service.py` | reading the store; assembling a batch |
| Create | `nexus/api/routers/engagement_insights.py` | `/engagement/insights/...` |
| Modify | `nexus/api/routers/__init__.py` | register it |
| Modify | `nexus/engagement/reports/today.py` | rank within a kind |
| Modify | `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts` | types and client |
| Create | `frontend/src/components/engagement/InsightBadge.tsx` (+ CSS), `BestTimeHint.tsx` (+ CSS), `zonedTime.ts`, `AccountConversations.module.css` | the pieces |
| Modify | `ReviewQueue.tsx`, `ContactPicker.tsx`, `StepsEditor.tsx`, `CampaignDetailPage.tsx`, `AccountConversations.tsx` | where they appear |
| Create/Modify | `tests/test_engagement_insights.py`, `tests/test_engagement_reporting.py`, `tests/test_engagement_screens_ui.py`, `tests_integration/test_ledger_stores_pg.py` | tests |

---

### Task 1: The display rules

**Files:** Create `nexus/engagement/insights/__init__.py`, `nexus/engagement/insights/rules.py`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_engagement_insights.py` from Task 7. Run `pytest tests/test_engagement_insights.py -n0 -q -k "workspace or opted or company"` — expected FAIL: `No module named 'nexus.engagement.insights'`.

- [ ] **Step 2: Implement** `nexus/engagement/insights/__init__.py`:

```python
"""What the app may tell an SDR about a prospect from the insights store (spec §18.5, D25, D26)."""
```

and `nexus/engagement/insights/rules.py`:

```python
"""The display rules for cross-workspace insights (D26). Pure: a profile in, what may be shown out.

The insights store keeps real people and companies (D25) so the app can advise about real buyers.
What a viewing workspace may SEE of that is decided here and nowhere else:

* **Patterns** ("usually replies on Tuesdays around 10am, typically within 4 hours") only when the
  person has history from **three or more** workspaces. With fewer, a pattern would let a workspace
  infer that a particular other vendor had been emailing the same buyer.
* **Below that, only the speed band of their last reply** ("within an hour", "same day", "within a
  week"), with no date, no count and no sender.
* **Never** who emailed them, what was said, or which workspaces. The profile's workspace keys never
  reach this process: the client reads only the store's `workspace_count`, which is all these rules
  need.
* **Only opted-in workspaces receive.** A workspace that has not switched the ledger on sees nothing
  from it, the same rule that keeps it from contributing.

A company profile follows the same threshold: an aggregate of one other vendor's campaign is still
that vendor's campaign.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

MIN_WORKSPACES = 3
WEEKDAYS = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")
BAND_TEXT = {
    "within_hour": "Last replied within an hour",
    "same_day": "Last replied the same day",
    "within_week": "Last replied within a week",
    "longer": "Last took over a week to reply",
}


@dataclass(slots=True)
class Insight:
    #: "pattern" | "band" | "none"
    level: str = "none"
    text: str = ""
    best_weekday: int | None = None
    best_hour: int | None = None
    typical_response_hours: float | None = None
    band: str = ""
    #: Reply rate across workspaces, 0..1; present only at the pattern level.
    propensity: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _clock(hour: int) -> str:
    suffix = "am" if hour < 12 else "pm"
    shown = hour % 12 or 12
    return f"{shown}{suffix}"


def _duration(hours: float) -> str:
    if hours < 1:
        return "an hour"
    if hours < 24:
        return f"{round(hours)} hours"
    days = round(hours / 24)
    return "a day" if days <= 1 else f"{days} days"


def describe(profile: dict | None, *, viewer_consented: bool, subject: str = "person") -> Insight:
    """What may be shown of one person (or company) profile to a viewing workspace."""
    if not viewer_consented or not profile:
        return Insight()
    workspaces = int(profile.get("workspace_count") or 0)
    if workspaces >= MIN_WORKSPACES:
        weekday, hour = profile.get("best_weekday"), profile.get("best_hour")
        median_s = profile.get("median_response_s")
        hours = round(median_s / 3600, 1) if median_s is not None else None
        parts = []
        if weekday is not None:
            when = f"on {WEEKDAYS[weekday]}"
            if hour is not None:
                when += f" around {_clock(hour)}"
            parts.append(f"usually replies {when}" if subject == "person"
                         else f"people there usually reply {when}")
        if hours is not None:
            parts.append(f"typically within {_duration(hours)}")
        if parts:
            text = ", ".join(parts)
            return Insight(level="pattern", text=text[0].upper() + text[1:],
                           best_weekday=weekday, best_hour=hour, typical_response_hours=hours,
                           propensity=profile.get("reply_propensity"))
    band = profile.get("last_reply_band") or ""
    if subject == "person" and band in BAND_TEXT:
        return Insight(level="band", text=BAND_TEXT[band], band=band)
    return Insight()
```

- [ ] **Step 3: Run** the same selection — expected PASS.

---

### Task 2: Likelihood and best time

**Files:** Create `nexus/engagement/insights/likelihood.py`, `nexus/engagement/insights/best_time.py`.

- [ ] **Step 1: Run** `pytest tests/test_engagement_insights.py -n0 -q -k "likelihood or responsiveness or best_time"` — expected FAIL (modules missing).

- [ ] **Step 2: Implement** `nexus/engagement/insights/likelihood.py`:

```python
"""How likely a prospect is to reply to YOU (spec §18.5, D26). Pure and deterministic, no model.

Computed from the viewing workspace's own evidence: its ICP fit for the account (the relevance
engine's latest composite score) and the account's recent signals, which are the prospect's
interests as this workspace has observed them. The person's general responsiveness across
workspaces joins only when the display rules allow a pattern (three or more workspaces); below that
threshold even a reply RATE would say that others have been writing to them.

Never another workspace's message content: this module does not receive any.

The weights are stated, not learned: fit carries most of it because it is the one thing the SDR
chose, signals say "now", and responsiveness shifts the answer rather than deciding it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

FIT_WEIGHT = 0.6
SIGNAL_WEIGHT = 0.4
RESPONSIVENESS_SHARE = 0.3
#: Three recent signals is as much "now" as the score can use.
SIGNALS_FOR_FULL = 3
#: A quarter of outreach answered is very responsive for cold email.
PROPENSITY_FOR_FULL = 0.25
HIGH, MEDIUM = 0.66, 0.4


@dataclass(slots=True)
class Likelihood:
    band: str                 # high | medium | low | unknown
    score: float              # 0..1, for ordering only; the screen shows the band
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def likelihood(*, fit: int | None, recent_signals: int, propensity: float | None) -> Likelihood:
    """``fit`` is the 0–100 composite (None = not scored, read as neutral); ``propensity`` only
    when the display rules produced a pattern for this person.

    With none of the three there is nothing to go on, and the answer says so: a neutral fit alone
    scores 0.3, which would label a contact nobody has looked at yet "less likely to reply"."""
    if fit is None and not recent_signals and propensity is None:
        return Likelihood(band="unknown", score=0.0,
                          reasons=["Not scored against your ICP yet, and no recent signals"])
    reasons: list[str] = []
    fit_part = (fit if fit is not None else 50) / 100
    if fit is None:
        reasons.append("Not scored against your ICP yet")
    elif fit >= 70:
        reasons.append(f"Strong fit for your ICP ({fit})")
    elif fit >= 40:
        reasons.append(f"Partial fit for your ICP ({fit})")
    else:
        reasons.append(f"Weak fit for your ICP ({fit})")
    signal_part = min(1.0, recent_signals / SIGNALS_FOR_FULL)
    if recent_signals:
        reasons.append(f"{recent_signals} {'signal' if recent_signals == 1 else 'signals'} in the "
                       "last 30 days")
    score = FIT_WEIGHT * fit_part + SIGNAL_WEIGHT * signal_part
    if propensity is not None:
        responsive = min(1.0, propensity / PROPENSITY_FOR_FULL)
        score = (1 - RESPONSIVENESS_SHARE) * score + RESPONSIVENESS_SHARE * responsive
        if propensity >= 0.15:
            reasons.append("Replies to more outreach than most")
        elif propensity < 0.03:
            reasons.append("Rarely replies to outreach")
    score = round(score, 3)
    band = "high" if score >= HIGH else "medium" if score >= MEDIUM else "low"
    return Likelihood(band=band, score=score, reasons=reasons)
```

and `nexus/engagement/insights/best_time.py`:

```python
"""When to send, for "at a set time" steps and for moving one person's next step (spec §8, §18.5).

Pure. The best time comes from what the display rules allow: a person's own pattern, else their
company's, else the engine's default working-morning time. A campaign's suggestion is the hour most
of its people with a pattern reply at, needing at least two of them to agree, so one prolific
replier does not set everyone's send time.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass

from nexus.engagement.insights.rules import WEEKDAYS, Insight

DEFAULT_HOUR = 9
DEFAULT_MINUTE = 30
MIN_AGREEING = 2


@dataclass(slots=True)
class BestTime:
    hour: int
    minute: int
    weekday: int | None
    #: "person" | "company" | "campaign" | "default"
    source: str
    text: str

    @property
    def clock(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def as_dict(self) -> dict:
        return {**asdict(self), "clock": self.clock}


def _default() -> BestTime:
    return BestTime(DEFAULT_HOUR, DEFAULT_MINUTE, None, "default",
                    "No pattern yet: their working morning is the safe choice.")


def for_person(person: Insight, company: Insight | None = None) -> BestTime:
    for insight, source, whose in ((person, "person", "They"), (company, "company", "People there")):
        if insight is not None and insight.level == "pattern" and insight.best_hour is not None:
            day = f" on {WEEKDAYS[insight.best_weekday]}" if insight.best_weekday is not None else ""
            return BestTime(insight.best_hour, 0, insight.best_weekday, source,
                            f"{whose} usually reply{day} around {insight.best_hour:02d}:00 "
                            "their time.")
    return _default()


def for_campaign(insights: list[Insight]) -> BestTime:
    hours = [i.best_hour for i in insights if i.level == "pattern" and i.best_hour is not None]
    if hours:
        hour, agreeing = Counter(hours).most_common(1)[0]
        if agreeing >= MIN_AGREEING:
            return BestTime(hour, 0, None, "campaign",
                            f"{agreeing} of the people here usually reply around {hour:02d}:00 "
                            "their time.")
    return _default()
```

- [ ] **Step 3: Run** the same selection — expected PASS.

---

### Task 3: Reading the store, assembling a batch, the router

**Files:** Create `nexus/engagement/insights/client.py`, `nexus/engagement/insights/service.py`, `nexus/api/routers/engagement_insights.py`; modify `nexus/api/routers/__init__.py`.

- [ ] **Step 1: Implement** `nexus/engagement/insights/client.py`. It reuses the phase 06 store connection, `stores.connect("insights")` (`StoreNotConfigured` when no insights connection string is stored):

```python
"""Read person and company profiles from the insights store (spec §18.3, §18.5). Read-only.

**Never raises and never blocks a screen.** An unconfigured store, an unreachable one, or a query
that fails all answer "no profile": insights decorate the screens, and a review queue must not fail
because a store in another cloud is slow.

**The workspace keys are not read.** The profile rows carry them so the builder can count distinct
workspaces; the display rules need only the count, so the key list never enters this process.

**A short cache, misses included.** Screens ask about the same people repeatedly (a review queue is
re-read after every approval); five minutes is well inside how often a profile can change, since
the builder runs on the worker's schedule. An unknown person is cached as unknown, or every render
of a list of new prospects would query the store for each of them.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("nexus.engagement.insights.client")

TTL_S = 300
MAX_BATCH = 200
MAX_CACHED = 5000

_PERSON_COLUMNS = ("person_email, company_domain, best_weekday, best_hour, median_response_s, "
                   "reply_propensity, last_reply_band, workspace_count")
_COMPANY_COLUMNS = ("company_domain, best_weekday, best_hour, median_response_s, reply_propensity, "
                    "workspace_count")

_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}


def clear_cache() -> None:
    _cache.clear()


def _normal(value: str) -> str:
    return (value or "").strip().lower()


async def _read(kind: str, keys: list[str]) -> dict[str, dict]:
    from nexus.engagement.ledger import stores

    table, key, columns = (("person_profiles", "person_email", _PERSON_COLUMNS) if kind == "person"
                           else ("company_profiles", "company_domain", _COMPANY_COLUMNS))
    try:
        async with stores.connect("insights") as conn:
            rows = await conn.fetch(
                f"SELECT {columns} FROM nexus_ledger.{table} WHERE {key} = ANY($1::text[])", keys)
    except stores.StoreNotConfigured:
        return {}
    except Exception:
        logger.warning("could not read %s profiles from the insights store", kind, exc_info=True)
        return {}
    return {row[key]: dict(row) for row in rows}


async def _profiles(kind: str, values: list[str]) -> dict[str, dict]:
    now = time.monotonic()
    wanted = sorted({_normal(v) for v in values if _normal(v)})[:MAX_BATCH]
    found: dict[str, dict] = {}
    missing: list[str] = []
    for value in wanted:
        hit = _cache.get((kind, value))
        if hit and hit[0] > now:
            if hit[1] is not None:
                found[value] = hit[1]
        else:
            missing.append(value)
    if missing:
        fresh = await _read(kind, missing)
        if len(_cache) > MAX_CACHED:
            _cache.clear()
        for value in missing:
            _cache[(kind, value)] = (now + TTL_S, fresh.get(value))
            if value in fresh:
                found[value] = fresh[value]
    return found


async def person_profiles(emails: list[str]) -> dict[str, dict]:
    """``{normalised email: profile}`` for the people the store knows."""
    return await _profiles("person", emails)


async def company_profiles(domains: list[str]) -> dict[str, dict]:
    return await _profiles("company", domains)
```

- [ ] **Step 2: Implement** `nexus/engagement/insights/service.py`:

```python
"""Insights for contacts as a screen shows them: what may be said, how likely a reply is, when to
send (spec §18.5). Assembles the pure rules over one batch of reads.

A fixed number of queries for any list: the contacts, their accounts, the latest score per account,
recent signals per account, and (for an opted-in workspace) one read each of person and company
profiles from the insights store. A workspace that has not opted in reads nothing from the store;
its likelihood is built from its own data only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from nexus.engagement.insights.best_time import BestTime, for_campaign, for_person
from nexus.engagement.insights.likelihood import Likelihood, likelihood
from nexus.engagement.insights.rules import Insight, describe

SIGNAL_WINDOW_DAYS = 30
MAX_CONTACTS = 100


@dataclass(slots=True)
class ContactInsight:
    contact_id: str
    person: Insight
    company: Insight
    likelihood: Likelihood
    best_time: BestTime

    def as_dict(self) -> dict:
        return {"contact_id": self.contact_id, "person": self.person.as_dict(),
                "company": self.company.as_dict(), "likelihood": self.likelihood.as_dict(),
                "best_time": self.best_time.as_dict()}


async def _latest_scores(ts, account_ids: set[str]) -> dict[str, int]:
    from nexus.models.intelligence import AccountScore

    scores = await ts.list(AccountScore, AccountScore.account_id.in_(account_ids)) \
        if account_ids else []
    latest: dict[str, AccountScore] = {}
    for s in scores:
        if s.account_id not in latest or s.computed_at > latest[s.account_id].computed_at:
            latest[s.account_id] = s
    return {a: s.composite for a, s in latest.items()}


async def _recent_signals(ts, account_ids: set[str], now: datetime) -> dict[str, int]:
    from sqlalchemy import func, select

    from nexus.models.signal import SignalEvent

    if not account_ids:
        return {}
    rows = (await ts.session.execute(
        select(SignalEvent.account_id, func.count())
        .where(SignalEvent.tenant_id == ts.tenant_id)
        .where(SignalEvent.account_id.in_(account_ids))
        .where(SignalEvent.occurred_at >= now - timedelta(days=SIGNAL_WINDOW_DAYS))
        .group_by(SignalEvent.account_id))).all()
    return {account_id: int(n) for account_id, n in rows}


async def for_contacts(ts, contact_ids: list[str], *, now: datetime) -> list[ContactInsight]:
    from nexus.engagement.insights import client
    from nexus.engagement.ledger import consent
    from nexus.models.account import Account, Contact

    ids = list(dict.fromkeys(contact_ids))[:MAX_CONTACTS]
    contacts = await ts.list(Contact, Contact.id.in_(ids)) if ids else []
    if not contacts:
        return []
    account_ids = {c.account_id for c in contacts if c.account_id}
    accounts = {a.id: a for a in await ts.list(Account, Account.id.in_(account_ids))} \
        if account_ids else {}
    scores = await _latest_scores(ts, account_ids)
    signals = await _recent_signals(ts, account_ids, now)

    consented = await consent.status(ts) == "on"
    people = await client.person_profiles([c.email for c in contacts if c.email]) \
        if consented else {}
    domains = [getattr(accounts.get(c.account_id), "domain", "") or "" for c in contacts]
    companies = await client.company_profiles([d for d in domains if d]) if consented else {}

    out = []
    order = {cid: i for i, cid in enumerate(ids)}
    for c in sorted(contacts, key=lambda c: order[c.id]):
        account = accounts.get(c.account_id)
        person = describe(people.get((c.email or "").strip().lower()), viewer_consented=consented)
        company = describe(companies.get((getattr(account, "domain", "") or "").strip().lower()),
                           viewer_consented=consented, subject="company")
        out.append(ContactInsight(
            contact_id=c.id, person=person, company=company,
            likelihood=likelihood(fit=scores.get(c.account_id),
                                  recent_signals=signals.get(c.account_id, 0),
                                  propensity=person.propensity if person.level == "pattern"
                                  else None),
            best_time=for_person(person, company)))
    return out


async def campaign_best_time(ts, campaign, *, now: datetime) -> BestTime:
    """The hour most of a campaign's people reply at, where the rules allow a pattern."""
    from nexus.models.engagement import EngagementEnrollment

    enrollments = await ts.list(EngagementEnrollment,
                                EngagementEnrollment.campaign_id == campaign.id)
    insights = await for_contacts(ts, [e.contact_id for e in enrollments], now=now)
    return for_campaign([i.person for i in insights])
```

- [ ] **Step 3: Implement** `nexus/api/routers/engagement_insights.py`:

```python
"""Insights for the engagement screens (spec §18.5, D26): what may be said about each person, how
likely they are to reply to you, and when to send.

Dark with the rest of the engine. Every rule about what may be shown lives in
`nexus/engagement/insights/rules.py`; this router only batches and serialises.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.routers.engagement_campaigns import _campaign, require_campaigns_enabled
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession

router = APIRouter(prefix="/engagement/insights", tags=["engagement"],
                   dependencies=[Depends(require_campaigns_enabled)])


@router.get("/contacts")
async def contact_insights(
    ids: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_engagement)),
) -> list[dict]:
    """``ids`` is a comma-separated list, at most 100: one call per screen, not one per row."""
    from nexus.core.db import utcnow
    from nexus.engagement.insights.service import MAX_CONTACTS, for_contacts

    wanted = [i for i in (s.strip() for s in ids.split(",")) if i]
    if len(wanted) > MAX_CONTACTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Ask about at most {MAX_CONTACTS} contacts at a time")
    return [i.as_dict() for i in await for_contacts(ts, wanted, now=utcnow())]


@router.get("/campaigns/{campaign_id}/best-time")
async def campaign_best_time(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.core.db import utcnow
    from nexus.engagement.insights.service import campaign_best_time as suggest

    campaign = await _campaign(ts, campaign_id, principal)
    return (await suggest(ts, campaign, now=utcnow())).as_dict()
```

and register it in `nexus/api/routers/__init__.py`:

```diff
diff --git a/nexus/api/routers/__init__.py b/nexus/api/routers/__init__.py
index 05759b5..cc0b3c3 100644
--- a/nexus/api/routers/__init__.py
+++ b/nexus/api/routers/__init__.py
@@ -32,4 +32,5 @@ from nexus.api.routers import (
     engagement_campaigns,
     engagement_desk,
+    engagement_insights,
     engagement_reports,
     engagement_settings,
@@ -89,4 +90,5 @@ all_routers = [
     engagement_campaigns.router,
     engagement_desk.router,
+    engagement_insights.router,
     engagement_reports.router,
     engagement_settings.router,
```

- [ ] **Step 4: Run** `pytest tests/test_engagement_insights.py -n0 -q` — expected PASS.

- [ ] **Step 5: The client against real Postgres.** Append to `tests_integration/test_ledger_stores_pg.py`:

```diff
diff --git a/tests_integration/test_ledger_stores_pg.py b/tests_integration/test_ledger_stores_pg.py
index 5cc0d28..b1bbe47 100644
--- a/tests_integration/test_ledger_stores_pg.py
+++ b/tests_integration/test_ledger_stores_pg.py
@@ -313,2 +313,37 @@ async def test_a_store_that_refuses_leaves_the_rows_to_retry_with_backoff(stores
     assert shipper.is_due(row.attempts, row.updated_at, row.updated_at + timedelta(minutes=1)) \
         is False
+
+
+async def test_the_insights_client_reads_profiles_without_their_workspace_keys(stores):
+    """Phase 13: the app reads profiles back from the real store; the key list stays in the store,
+    a miss is remembered, and an unconfigured store is simply no answer."""
+    from nexus.core.config import get_settings
+    from nexus.engagement.insights import client
+    from nexus.engagement.ledger import stores as ledger_stores
+    from nexus.providers import resolver
+
+    await _apply_all()
+    client.clear_cache()
+    async with ledger_stores.connect("insights") as conn:
+        await conn.execute(
+            "INSERT INTO nexus_ledger.person_profiles (person_email, person_key, company_domain,"
+            " best_weekday, best_hour, median_response_s, reply_propensity, last_reply_band,"
+            " sends, replies, workspace_count, workspace_keys) VALUES ('jane@acme.io', 'pk',"
+            " 'acme.io', 1, 10, 14400, 0.2, 'same_day', 10, 2, 3, ARRAY['w1','w2','w3'])")
+
+    found = await client.person_profiles(["Jane@Acme.io", "nobody@acme.io"])
+    assert list(found) == ["jane@acme.io"]
+    assert found["jane@acme.io"]["workspace_count"] == 3
+    assert "workspace_keys" not in found["jane@acme.io"]
+
+    # Remembered for a few minutes, the miss included: no second query for either person.
+    async with ledger_stores.connect("insights") as conn:
+        await conn.execute("DELETE FROM nexus_ledger.person_profiles")
+    assert list(await client.person_profiles(["jane@acme.io", "nobody@acme.io"])) == ["jane@acme.io"]
+    client.clear_cache()
+    assert await client.person_profiles(["jane@acme.io"]) == {}
+
+    get_settings().ledger_insights_dsn = ""
+    resolver.invalidate()
+    client.clear_cache()
+    assert await client.person_profiles(["jane@acme.io"]) == {}
```

Run with a throwaway Postgres (never a shared database):

```bash
docker network create nexus-it-net
docker run -d --rm --name nexus-it-pg --network nexus-it-net -e POSTGRES_PASSWORD=itpass postgres:16-alpine
NEXUS_TEST_POSTGRES_URL=postgresql+asyncpg://postgres:itpass@nexus-it-pg:5432/postgres pytest tests_integration/test_ledger_stores_pg.py -n0 -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add nexus/engagement/insights nexus/api/routers/engagement_insights.py nexus/api/routers/__init__.py tests/test_engagement_insights.py tests_integration/test_ledger_stores_pg.py
git commit -m "feat(engagement): insights rules, likelihood, best time and the read-only store client"
```

---

### Task 4: Today, ranked by likelihood

**Files:** Modify `nexus/engagement/reports/today.py`; test in `tests/test_engagement_reporting.py`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_engagement_reporting.py`:

```diff
diff --git a/tests/test_engagement_reporting.py b/tests/test_engagement_reporting.py
index c7fde1b..747aa8e 100644
--- a/tests/test_engagement_reporting.py
+++ b/tests/test_engagement_reporting.py
@@ -284,4 +284,56 @@ async def test_today_puts_waiting_buyers_first_and_returning_people_last(client,
 
 
+async def test_within_a_kind_today_ranks_by_reply_likelihood_then_age(client, engine_on):
+    """§19: the list is ranked by reply likelihood (§18.5). By BAND, not raw score, so people in one
+    band stay oldest-first, which is what the reply-speed reminder measures."""
+    from nexus.core.db import utcnow
+    from nexus.engagement.insights.client import clear_cache
+    from nexus.models.account import Account, Contact
+    from nexus.models.calling import CallTask
+    from nexus.models.engagement import EngagementCampaign, EngagementEnrollment, MailboxConnection
+    from nexus.models.intelligence import AccountScore
+    from nexus.models.signal import SignalEvent
+
+    clear_cache()
+    token = await signup(client, slug="reprank", email="sam@reprank.com", company="R")
+    me = principal_from_token(token)
+    now = utcnow()
+    async with tenant_session(me.tenant_id) as ts:
+        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
+                                    email="sam@reprank.com", status="connected", timezone="UTC")
+        strong, fresh = Account(name="Acme", domain="acme.io"), Account(name="Globex", domain="globex.com")
+        for row in (mailbox, strong, fresh):
+            ts.add(row)
+        await ts.flush()
+        ts.add(AccountScore(account_id=strong.id, composite=90, computed_at=now))
+        for i in range(3):
+            ts.add(SignalEvent(account_id=strong.id, kind="funding", source="web", title=f"Round {i}",
+                               strength=0.9, occurred_at=now - timedelta(days=2),
+                               dedupe_key=f"rank-{i}"))
+        campaign = EngagementCampaign(name="Q4", owner_user_id=me.user_id,
+                                      mailbox_connection_id=mailbox.id, status="active")
+        ts.add(campaign)
+        await ts.flush()
+        # Globex has no score and no signals; Acme is a strong fit with three recent signals.
+        for name, account, hours_ago in (("Gia", fresh, 2), ("Ada", strong, 1), ("Gus", fresh, 3)):
+            person = Contact(account_id=account.id, full_name=name, email=f"{name.lower()}@x.io")
+            ts.add(person)
+            await ts.flush()
+            enrollment = EngagementEnrollment(campaign_id=campaign.id, contact_id=person.id,
+                                              account_id=account.id, mailbox_connection_id=mailbox.id,
+                                              status="active")
+            ts.add(enrollment)
+            await ts.flush()
+            ts.add(CallTask(account_id=account.id, contact_id=person.id, reason="Call step",
+                            owner_user_id=me.user_id, due_at=now - timedelta(hours=hours_ago),
+                            engagement_enrollment_id=enrollment.id))
+
+    r = await client.get("/api/engagement/today", headers=auth(token))
+    assert r.status_code == 200, r.text
+    calls = [i["title"] for i in r.json() if i["kind"] == "call"]
+    # Ada is the newest call but the likeliest reply; Gus and Gia tie (no evidence) and stay by age.
+    assert calls == ["Call Ada at Acme", "Call Gus at Globex", "Call Gia at Globex"]
+
+
 # ---- mailbox health, where the SDR already looks -------------------------------------------------
 
```

Run `pytest tests/test_engagement_reporting.py -n0 -q -k ranks` — expected FAIL: the calls come back oldest first (`Gus, Gia, Ada`).

- [ ] **Step 2: Implement.** Apply to `nexus/engagement/reports/today.py`. `contact_id` rides on the item for ranking and is dropped from `as_dict`, so the response is unchanged:

```diff
diff --git a/nexus/engagement/reports/today.py b/nexus/engagement/reports/today.py
index 94decdd..7d1e4e4 100644
--- a/nexus/engagement/reports/today.py
+++ b/nexus/engagement/reports/today.py
@@ -6,6 +6,8 @@ decide, then colleagues a reply paused, then calls due, then opening emails wait
 then the people coming back today, who need nothing but are worth knowing about.
 
-Within a kind, the longest-waiting first. Ranking by reply likelihood is phase 13's (insights); until
-then age is the honest order, and it is also what the reply-speed reminder measures.
+Within a kind, ranked by reply likelihood (§18.5), and by its BAND rather than its score: people in
+one band stay longest-waiting first, which is what the reply-speed reminder measures, and a
+hundredth of a point of fit does not jump someone the queue. Items with no one person behind them
+(colleagues at an account, a campaign's review) keep their age order.
 
 Scoped to the caller's own mailboxes and campaigns: this is a personal list, and a manager's team
@@ -19,4 +21,6 @@ from datetime import UTC, datetime, timedelta
 #: The order kinds appear in, and what each one asks of the SDR.
 KINDS = ("reply", "decide", "colleagues", "call", "review", "returning")
+#: Likelihood bands, likeliest first. `unknown` (nothing to go on) ranks with the unrated items.
+_BAND_RANK = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
 
 
@@ -36,7 +40,11 @@ class TodayItem:
     at: datetime | None
     count: int = 1
+    #: Whose item this is, for ranking; not part of what the screen receives.
+    contact_id: str | None = None
 
     def as_dict(self) -> dict:
-        return asdict(self)
+        out = asdict(self)
+        out.pop("contact_id")
+        return out
 
 
@@ -105,10 +113,12 @@ async def today(ts, *, user_id: str, now: datetime) -> list[TodayItem]:
             items.append(TodayItem("decide", f"Decide on {who(r.contact_id)}",
                                    "Their reply needs a person to say what happens next.",
-                                   f"/engagement/replies?reply={r.id}", arrived(r)))
+                                   f"/engagement/replies?reply={r.id}", arrived(r),
+                                   contact_id=r.contact_id))
         else:
             said = {"interested": "They're interested.", "question": "They asked a question.",
                     "referral": "They pointed you to someone else."}.get(category, "They replied.")
             items.append(TodayItem("reply", f"Answer {who(r.contact_id)}", said,
-                                   f"/engagement/replies?reply={r.id}", arrived(r)))
+                                   f"/engagement/replies?reply={r.id}", arrived(r),
+                                   contact_id=r.contact_id))
 
     by_account: dict[str, list] = {}
@@ -125,5 +135,5 @@ async def today(ts, *, user_id: str, now: datetime) -> list[TodayItem]:
     for c in sorted(calls, key=lambda c: _aware(c.due_at) or now):
         items.append(TodayItem("call", f"Call {who(c.contact_id)}", c.reason or "A call step is due.",
-                               "/calls", c.due_at))
+                               "/calls", c.due_at, contact_id=c.contact_id))
 
     campaigns = await ts.list(EngagementCampaign, EngagementCampaign.owner_user_id == user_id,
@@ -147,6 +157,23 @@ async def today(ts, *, user_id: str, now: datetime) -> list[TodayItem]:
         items.append(TodayItem("returning", f"{who(e.contact_id)} comes back today",
                                "Their sequence resumes on its own.", "/engagement/replies?tab=scheduled",
-                               e.snoozed_until))
+                               e.snoozed_until, contact_id=e.contact_id))
 
+    bands = await _likelihood_bands(ts, [i.contact_id for i in items if i.contact_id], now)
     order = {kind: i for i, kind in enumerate(KINDS)}
-    return sorted(items, key=lambda i: order[i.kind])
+    # A stable sort: within one kind and one band, the age order built above survives.
+    return sorted(items, key=lambda i: (order[i.kind],
+                                        _BAND_RANK[bands.get(i.contact_id or "", "unknown")]))
+
+
+async def _likelihood_bands(ts, contact_ids: list[str], now: datetime) -> dict[str, str]:
+    """Each person's reply-likelihood band. The Today plan must load even when insights cannot:
+    a failure ranks everyone `unknown`, which is the age order."""
+    from nexus.engagement.insights.service import for_contacts
+
+    if not contact_ids:
+        return {}
+    try:
+        insights = await for_contacts(ts, contact_ids, now=now)
+    except Exception:  # noqa: BLE001 - ranking is a nicety; the list is the product
+        return {}
+    return {i.contact_id: i.likelihood.band for i in insights}
```

- [ ] **Step 3: Run** `pytest tests/test_engagement_reporting.py -n0 -q` — expected PASS (the phase 12 order test still holds: kinds keep their order).

---

### Task 5: Types, client and the pieces

- [ ] **Step 1: Types** — `frontend/src/lib/types.ts`:

```diff
diff --git a/frontend/src/lib/types.ts b/frontend/src/lib/types.ts
index 18f6c21..cc021fc 100644
--- a/frontend/src/lib/types.ts
+++ b/frontend/src/lib/types.ts
@@ -2542,2 +2542,40 @@ export interface TodayItem {
   count: number;
 }
+
+// ---- engagement: insights (phase 13) -----------------------------------------------------------
+
+/** What may be shown of a person or company (D26): a pattern at 3+ workspaces, else a speed band. */
+export interface ProspectInsight {
+  level: "pattern" | "band" | "none";
+  text: string;
+  best_weekday: number | null;
+  best_hour: number | null;
+  typical_response_hours: number | null;
+  band: string;
+  propensity: number | null;
+}
+
+export interface ReplyLikelihood {
+  /** `unknown` when there is no fit, no recent signal and no allowed pattern to go on. */
+  band: "high" | "medium" | "low" | "unknown";
+  score: number;
+  reasons: string[];
+}
+
+export interface BestTimeSuggestion {
+  hour: number;
+  minute: number;
+  weekday: number | null;
+  source: "person" | "company" | "campaign" | "default";
+  text: string;
+  /** "HH:MM", in the contact's own timezone. */
+  clock: string;
+}
+
+export interface ContactInsight {
+  contact_id: string;
+  person: ProspectInsight;
+  company: ProspectInsight;
+  likelihood: ReplyLikelihood;
+  best_time: BestTimeSuggestion;
+}
```

and `frontend/src/lib/api.ts`:

```diff
diff --git a/frontend/src/lib/api.ts b/frontend/src/lib/api.ts
index 79229d3..96a09b3 100644
--- a/frontend/src/lib/api.ts
+++ b/frontend/src/lib/api.ts
@@ -96,4 +96,6 @@ import type {
   ResponseTimeRow,
   TodayItem,
+  ContactInsight,
+  BestTimeSuggestion,
   DoNotContactEntry,
   LedgerStatus,
@@ -1360,4 +1362,16 @@ export class ApiClient {
   }
 
+  // ---- engagement: insights ----
+  contactInsights(contactIds: string[], signal?: AbortSignal) {
+    return this.request<ContactInsight[]>("/engagement/insights/contacts", {
+      query: { ids: contactIds.slice(0, 100).join(",") }, signal,
+    });
+  }
+  campaignBestTime(id: string, signal?: AbortSignal) {
+    return this.request<BestTimeSuggestion>(`/engagement/insights/campaigns/${id}/best-time`, {
+      signal,
+    });
+  }
+
   // ---- engagement: sequence templates ----
   listSequenceTemplates(signal?: AbortSignal) {
```

- [ ] **Step 2: The badge** — `frontend/src/components/engagement/InsightBadge.tsx`. `useContactInsights` asks for a whole list in one request; `hasInsight` lets a screen leave out people there is nothing to say about:

```tsx
import { useMemo } from "react";
import { Badge, Icons } from "@/components/ui";
import type { BadgeTone } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { ContactInsight, ReplyLikelihood } from "@/lib/types";
import styles from "./InsightBadge.module.css";

/**
 * How likely a person is to reply to you, and what may be said about how they reply (spec §18.5).
 *
 * The server has already applied every display rule (D26): a pattern only at three or more
 * workspaces, otherwise the last reply's speed band, and nothing for a workspace that has not
 * opted in. This component shows what it is given and never infers more. The likelihood badge says
 * its band in words and lists its reasons on hover and to screen readers, so colour is never the
 * only signal.
 */

const LIKELIHOOD: Record<Exclude<ReplyLikelihood["band"], "unknown">, { label: string; tone: BadgeTone }> = {
  high: { label: "Likely to reply", tone: "success" },
  medium: { label: "May reply", tone: "info" },
  low: { label: "Less likely to reply", tone: "neutral" },
};

/** Insights for a list of contacts, in one request (at most 100). */
export function useContactInsights(contactIds: string[]): Map<string, ContactInsight> {
  const api = useApiClient();
  const key = contactIds.slice(0, 100).join(",");
  const state = useApi<ContactInsight[]>(
    (s) => (key ? api.contactInsights(key.split(","), s) : Promise.resolve([])), [key],
  );
  return useMemo(() => new Map((state.data ?? []).map((i) => [i.contact_id, i])), [state.data]);
}

/** Whether the full (non-compact) badge would show anything for this insight. */
export function hasInsight(insight?: ContactInsight): boolean {
  if (!insight) return false;
  return insight.likelihood.band !== "unknown" || insight.person.level !== "none"
    || insight.company.level === "pattern";
}

export function InsightBadge({ insight, compact = false }: { insight?: ContactInsight; compact?: boolean }) {
  if (!insight) return null;
  const band = insight.likelihood.band;
  // Nothing to go on is said by saying nothing, not by a "less likely" badge built from no evidence.
  const like = band === "unknown" ? null : LIKELIHOOD[band];
  const why = insight.likelihood.reasons.join(". ");
  const pattern = insight.person.level !== "none" ? insight.person
    : insight.company.level === "pattern" ? insight.company : null;
  if (!like && !(pattern && !compact)) return null;
  return (
    <span className={compact ? styles.compact : styles.insight}>
      {like && <Badge tone={like.tone} title={why} aria-label={`${like.label}. ${why}`}>{like.label}</Badge>}
      {!compact && pattern && (
        <span className={styles.pattern}>
          <Icons.ActivityIcon aria-hidden className={styles.icon} />
          {pattern.text}
        </span>
      )}
    </span>
  );
}
```

```css
.insight {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

.compact {
  display: inline-flex;
}

.pattern {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.icon {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
}
```

- [ ] **Step 3: The best-time hint** — `frontend/src/components/engagement/BestTimeHint.tsx`. The "Use" button appears only when the suggestion differs from what is set:

```tsx
import { Button } from "@/components/ui";
import type { BestTimeSuggestion } from "@/lib/types";
import styles from "./BestTimeHint.module.css";

/**
 * "Suggest best time" for manual timing (spec §8, §18.5): the suggestion in words, and a button that
 * applies it. The clock is in the contact's own time, which is also how a step's set time is read,
 * so applying it needs no conversion for a step; moving one person's next step converts it first.
 */
export function BestTimeHint({ suggestion, onUse, current }: {
  suggestion?: BestTimeSuggestion;
  onUse: (clock: string) => void;
  /** The time already chosen, so the button is not offered for what is already set. */
  current?: string | null;
}) {
  if (!suggestion) return null;
  return (
    <p className={styles.hint}>
      <span>{suggestion.text}</span>
      {current !== suggestion.clock && (
        <Button size="sm" variant="ghost" type="button" onClick={() => onUse(suggestion.clock)}>
          Use {suggestion.clock}
        </Button>
      )}
    </p>
  );
}
```

```css
.hint {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
  line-height: var(--leading);
}
```

- [ ] **Step 4: Zoned time** — `frontend/src/components/engagement/zonedTime.ts`:

```ts
/**
 * Turn "this clock time in the contact's timezone, on this date" into the value a
 * `datetime-local` input needs, which is the viewer's own local time.
 *
 * The browser has no API for "wall time in zone X", so the offset of the zone at that instant is
 * read from `Intl` and applied, twice, so a date that crosses a daylight-saving change still lands
 * on the right hour. An unknown zone falls back to the viewer's own, rather than throwing.
 */

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

export function toLocalInput(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function zoneOffsetMinutes(at: Date, timeZone: string): number {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).formatToParts(at).map((p) => [p.type, p.value]),
  );
  const asUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
  return Math.round((asUtc - at.getTime()) / 60000);
}

/** `date` is the `YYYY-MM-DD` part of the input's current value; `clock` is `HH:MM` in `timeZone`. */
export function zonedClockToLocalInput(date: string, clock: string, timeZone: string): string {
  const [y, m, d] = date.split("-").map(Number);
  const [hh, mm] = clock.split(":").map(Number);
  const wall = Date.UTC(y, m - 1, d, hh, mm);
  try {
    let instant = wall;
    for (let i = 0; i < 2; i += 1) instant = wall - zoneOffsetMinutes(new Date(instant), timeZone) * 60000;
    return toLocalInput(new Date(instant));
  } catch {
    return `${date}T${clock}`;
  }
}
```

---

### Task 6: Where they appear

- [ ] **Step 1: The review queue** — `frontend/src/pages/engagement/ReviewQueue.tsx`:

```diff
diff --git a/frontend/src/pages/engagement/ReviewQueue.tsx b/frontend/src/pages/engagement/ReviewQueue.tsx
index f2f1ab6..34bb8a1 100644
--- a/frontend/src/pages/engagement/ReviewQueue.tsx
+++ b/frontend/src/pages/engagement/ReviewQueue.tsx
@@ -7,5 +7,6 @@ import { useApi } from "@/hooks/useApi";
 import { useApiClient } from "@/app/AuthContext";
 import { ApiError } from "@/lib/api";
-import type { ReviewItem } from "@/lib/types";
+import type { ContactInsight, ReviewItem } from "@/lib/types";
+import { InsightBadge, useContactInsights } from "@/components/engagement/InsightBadge";
 import styles from "./Engagement.module.css";
 
@@ -41,4 +42,6 @@ export function ReviewQueue({ campaignId, onChanged }: ReviewQueueProps) {
 
   const items = review.data ?? [];
+  // How likely each person is to reply, and how they reply where that may be shown (D26).
+  const insights = useContactInsights(items.map((i) => i.contact_id));
   const undrafted = items.filter((i) => i.status === "undrafted").length;
   const passing = items.filter((i) => i.status === "draft" && i.quality_problems.length === 0).length;
@@ -147,5 +150,5 @@ export function ReviewQueue({ campaignId, onChanged }: ReviewQueueProps) {
       <ol className={styles.reviewList}>
         {items.map((item) => (
-          <ReviewCard key={item.enrollment_id} item={item}
+          <ReviewCard key={item.enrollment_id} item={item} insight={insights.get(item.contact_id)}
             onChanged={() => { review.refetch(); onChanged(); }} />
         ))}
@@ -155,5 +158,9 @@ export function ReviewQueue({ campaignId, onChanged }: ReviewQueueProps) {
 }
 
-function ReviewCard({ item, onChanged }: { item: ReviewItem; onChanged: () => void }) {
+function ReviewCard({ item, insight, onChanged }: {
+  item: ReviewItem;
+  insight?: ContactInsight;
+  onChanged: () => void;
+}) {
   const api = useApiClient();
   const toast = useToast();
@@ -187,4 +194,5 @@ function ReviewCard({ item, onChanged }: { item: ReviewItem; onChanged: () => vo
           </span>
           <span className={styles.muted}>{item.contact_email}</span>
+          <InsightBadge insight={insight} />
         </div>
         <Badge tone={status.tone} dot>{status.label}</Badge>
```

- [ ] **Step 2: The contact picker** (compact badge, first 100 rows) — `frontend/src/components/engagement/ContactPicker.tsx`:

```diff
diff --git a/frontend/src/components/engagement/ContactPicker.tsx b/frontend/src/components/engagement/ContactPicker.tsx
index 4fb76b7..4f3b36a 100644
--- a/frontend/src/components/engagement/ContactPicker.tsx
+++ b/frontend/src/components/engagement/ContactPicker.tsx
@@ -7,4 +7,5 @@ import { useApi } from "@/hooks/useApi";
 import { useApiClient } from "@/app/AuthContext";
 import type { EngagementCandidate, ProspectList } from "@/lib/types";
+import { InsightBadge, useContactInsights } from "./InsightBadge";
 import styles from "./ContactPicker.module.css";
 
@@ -68,4 +69,6 @@ export function ContactPicker({ enrolledIds, onAdd }: ContactPickerProps) {
 
   const rows = found.data ?? [];
+  // The likelihood badge for the first hundred shown: one request, not one per row.
+  const insights = useContactInsights(rows.slice(0, 100).map((r) => r.contact_id));
   const selectable = useMemo(
     () => rows.filter((r) => !r.blocked && !enrolledIds.has(r.contact_id)),
@@ -132,4 +135,5 @@ export function ContactPicker({ enrolledIds, onAdd }: ContactPickerProps) {
           <span className={styles.name}>{r.full_name}</span>
           {r.title && <span className={styles.sub}>{r.title}</span>}
+          <InsightBadge insight={insights.get(r.contact_id)} compact />
         </div>
       ),
```

- [ ] **Step 3: A set-time step** — `frontend/src/components/engagement/StepsEditor.tsx`:

```diff
diff --git a/frontend/src/components/engagement/StepsEditor.tsx b/frontend/src/components/engagement/StepsEditor.tsx
index 9535535..b6b4899 100644
--- a/frontend/src/components/engagement/StepsEditor.tsx
+++ b/frontend/src/components/engagement/StepsEditor.tsx
@@ -1,4 +1,5 @@
 import { Button, Field, IconButton, Icons, Input, Select, Textarea } from "@/components/ui";
-import type { EngagementStep } from "@/lib/types";
+import type { BestTimeSuggestion, EngagementStep } from "@/lib/types";
+import { BestTimeHint } from "./BestTimeHint";
 import { WEEKDAYS } from "./labels";
 import styles from "./StepsEditor.module.css";
@@ -47,7 +48,9 @@ export interface StepsEditorProps {
   onChange: (steps: EngagementStep[]) => void;
   disabled?: boolean;
+  /** When the people are known (a campaign, not a template), when most of them reply. */
+  bestTime?: BestTimeSuggestion;
 }
 
-export function StepsEditor({ steps, onChange, disabled = false }: StepsEditorProps) {
+export function StepsEditor({ steps, onChange, disabled = false, bestTime }: StepsEditorProps) {
   function update(index: number, patch: Partial<EngagementStep>) {
     onChange(steps.map((s, i) => (i === index ? { ...s, ...patch } : s)));
@@ -160,4 +163,9 @@ export function StepsEditor({ steps, onChange, disabled = false }: StepsEditorPr
                 </div>
 
+                {step.timing_mode === "manual" && (
+                  <BestTimeHint suggestion={bestTime} current={step.send_time_local}
+                    onUse={(clock) => update(index, { send_time_local: clock })} />
+                )}
+
                 {step.channel === "email" && (
                   <Field
```

- [ ] **Step 4: The campaign page** — the Steps tab asks for the campaign's best time only while the steps are editable, and the move-one-person dialog converts that person's suggestion into the viewer's local input. `frontend/src/pages/engagement/CampaignDetailPage.tsx`:

```diff
diff --git a/frontend/src/pages/engagement/CampaignDetailPage.tsx b/frontend/src/pages/engagement/CampaignDetailPage.tsx
index 3f5b78d..a28793f 100644
--- a/frontend/src/pages/engagement/CampaignDetailPage.tsx
+++ b/frontend/src/pages/engagement/CampaignDetailPage.tsx
@@ -8,5 +8,8 @@ import {
 import type { Column } from "@/components/ui";
 import { useToast } from "@/components/ui/Toast";
+import { BestTimeHint } from "@/components/engagement/BestTimeHint";
 import { ContactPicker } from "@/components/engagement/ContactPicker";
+import { useContactInsights } from "@/components/engagement/InsightBadge";
+import { zonedClockToLocalInput } from "@/components/engagement/zonedTime";
 import { StepsEditor, stepsProblem } from "@/components/engagement/StepsEditor";
 import {
@@ -17,5 +20,6 @@ import { useApiClient } from "@/app/AuthContext";
 import { ApiError } from "@/lib/api";
 import type {
-  ConnectedMailbox, EngagementCampaign, EngagementEnrollment, EngagementStep, EnrollResult,
+  BestTimeSuggestion, ConnectedMailbox, EngagementCampaign, EngagementEnrollment, EngagementStep,
+  EnrollResult,
 } from "@/lib/types";
 import { LaunchPanel } from "./LaunchPanel";
@@ -265,4 +269,6 @@ function PeopleTable({
   const [moving, setMoving] = useState<EngagementEnrollment | null>(null);
   const [moveTo, setMoveTo] = useState("");
+  // When this person usually replies, in their own time (D26 applied on the server).
+  const movingInsight = useContactInsights(moving ? [moving.contact_id] : []).get(moving?.contact_id ?? "");
 
   async function act(row: EngagementEnrollment, action: "pause" | "resume" | "stop" | "send-now") {
@@ -408,4 +414,11 @@ function PeopleTable({
           <Input type="datetime-local" value={moveTo} onChange={(e) => setMoveTo(e.target.value)} />
         </Field>
+        {moving && movingInsight && movingInsight.best_time.source !== "default" && (
+          <BestTimeHint
+            suggestion={movingInsight.best_time}
+            onUse={(clock) => setMoveTo(zonedClockToLocalInput(
+              moveTo.slice(0, 10), clock, moving.contact_timezone))}
+          />
+        )}
       </Modal>
     </>
@@ -422,4 +435,9 @@ function StepsPanel({ campaign, editable, onSaved }: {
   const [steps, setSteps] = useState<EngagementStep[]>(() => campaign.steps.map((s) => ({ ...s })));
   const [saving, setSaving] = useState(false);
+  // Only while the steps can still change: a launched campaign's set times are fixed.
+  const best = useApi<BestTimeSuggestion | null>(
+    (s) => (editable ? api.campaignBestTime(campaign.id, s) : Promise.resolve(null)),
+    [campaign.id, editable],
+  );
   const problem = stepsProblem(steps);
   const dirty = JSON.stringify(steps) !== JSON.stringify(campaign.steps);
@@ -463,5 +481,5 @@ function StepsPanel({ campaign, editable, onSaved }: {
   return (
     <Card padding="lg" className={styles.section}>
-      <StepsEditor steps={steps} onChange={setSteps} />
+      <StepsEditor steps={steps} onChange={setSteps} bestTime={best.data ?? undefined} />
       {problem && dirty && <p className={styles.formError} role="alert">{problem}</p>}
       <div className={styles.formActions}>
```

- [ ] **Step 5: The account page** — "How they reply" above the timeline, listing only the people there is something to say about. `frontend/src/components/engagement/AccountConversations.tsx`:

```diff
diff --git a/frontend/src/components/engagement/AccountConversations.tsx b/frontend/src/components/engagement/AccountConversations.tsx
index 5914d1a..c0d631d 100644
--- a/frontend/src/components/engagement/AccountConversations.tsx
+++ b/frontend/src/components/engagement/AccountConversations.tsx
@@ -2,18 +2,24 @@ import { Card, ErrorState, Skeleton } from "@/components/ui";
 import { useApi } from "@/hooks/useApi";
 import { useApiClient } from "@/app/AuthContext";
-import type { ReplyCategory, TimelineEntry } from "@/lib/types";
+import type { Contact, ReplyCategory, TimelineEntry } from "@/lib/types";
 import { ConversationTimeline } from "./ConversationTimeline";
+import { InsightBadge, hasInsight, useContactInsights } from "./InsightBadge";
+import styles from "./AccountConversations.module.css";
 
 /**
- * Every email to and from the people at an account (or one contact), across campaigns and one-off
- * sends (spec §9, "a cross-campaign conversation timeline"). Previews, oldest first; the full thread
- * of a reply is on the reply desk.
+ * Every email to and from the people at an account, across campaigns and one-off sends (spec §9,
+ * "a cross-campaign conversation timeline"), and above it what may be said about how each of them
+ * replies (§18.5: insights appear on the contact page). Previews, oldest first; the full thread of a
+ * reply is on the reply desk.
  */
-export function AccountConversations({ accountId, contactId }: { accountId?: string; contactId?: string }) {
+export function AccountConversations({ accountId }: { accountId: string }) {
   const api = useApiClient();
   const timeline = useApi<TimelineEntry[]>(
-    (s) => api.engagementTimeline({ account_id: accountId, contact_id: contactId }, s),
-    [accountId, contactId],
+    (s) => api.engagementTimeline({ account_id: accountId }, s), [accountId],
   );
+  const contacts = useApi<Contact[]>((s) => api.listContacts(accountId, s), [accountId]);
+  const withEmail = (contacts.data ?? []).filter((c) => c.email);
+  const insights = useContactInsights(withEmail.map((c) => c.id));
+  const people = withEmail.filter((c) => hasInsight(insights.get(c.id)));
 
   if (timeline.error) {
@@ -22,22 +28,37 @@ export function AccountConversations({ accountId, contactId }: { accountId?: str
   if (!timeline.data) return <Skeleton width="100%" height={240} />;
   return (
-    <Card padding="lg">
-      <ConversationTimeline
-        clamp
-        empty="No emails have been sent to anyone here from NEXUS yet."
-        messages={timeline.data.map((e) => ({
-          id: e.message_id,
-          direction: e.direction,
-          subject: e.subject,
-          body: e.preview,
-          at: e.at,
-          who: e.contact_name || undefined,
-          context: e.direction === "out"
-            ? `${e.contact_name ? `To ${e.contact_name} · ` : ""}${e.campaign_name || "One-off email"}`
-            : e.campaign_name || undefined,
-          category: (e.category as ReplyCategory | null) ?? null,
-        }))}
-      />
-    </Card>
+    <div className={styles.stack}>
+      {people.length > 0 && (
+        <Card padding="lg">
+          <h3 className={styles.title}>How they reply</h3>
+          <ul className={styles.people}>
+            {people.map((c) => (
+              <li key={c.id} className={styles.person}>
+                <span className={styles.name}>{c.full_name}</span>
+                <InsightBadge insight={insights.get(c.id)} />
+              </li>
+            ))}
+          </ul>
+        </Card>
+      )}
+      <Card padding="lg">
+        <ConversationTimeline
+          clamp
+          empty="No emails have been sent to anyone here from NEXUS yet."
+          messages={timeline.data.map((e) => ({
+            id: e.message_id,
+            direction: e.direction,
+            subject: e.subject,
+            body: e.preview,
+            at: e.at,
+            who: e.contact_name || undefined,
+            context: e.direction === "out"
+              ? `${e.contact_name ? `To ${e.contact_name} · ` : ""}${e.campaign_name || "One-off email"}`
+              : e.campaign_name || undefined,
+            category: (e.category as ReplyCategory | null) ?? null,
+          }))}
+        />
+      </Card>
+    </div>
   );
 }
```

```css
.stack {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.title {
  margin: 0 0 var(--space-3);
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--text);
}

.people {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.person {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.name {
  font-weight: var(--weight-medium);
  color: var(--text);
}
```

- [ ] **Step 6: Build** — `npm run typecheck && npm run build` in `frontend/` — expected: no errors.

---

### Task 7: The tests

- [ ] **Step 1:** `tests/test_engagement_insights.py`:

```python
"""Insights for the engagement screens (spec §18.5, D25, D26).

The display rules are pure and tested at 1, 2 and 3 workspaces (spec §14). The endpoint runs on the
offline suite with no insights store configured, which is also a real case: a deployment that has
not set the store up still gets a likelihood from its own data. Reading profiles from a real store
is covered in `tests_integration/test_ledger_stores_pg.py`.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session


def _profile(workspaces: int, **over) -> dict:
    base = {"person_email": "jane@acme.io", "company_domain": "acme.io", "best_weekday": 1,
            "best_hour": 10, "median_response_s": 4 * 3600, "reply_propensity": 0.2,
            "last_reply_band": "same_day", "workspace_count": workspaces}
    return {**base, **over}


# ---- the display rules (D26) ---------------------------------------------------------------------

@pytest.mark.parametrize("workspaces", [1, 2])
def test_below_three_workspaces_only_the_last_replys_speed_band_is_shown(workspaces):
    from nexus.engagement.insights.rules import describe

    insight = describe(_profile(workspaces), viewer_consented=True)
    assert insight.level == "band" and insight.text == "Last replied the same day"
    # No weekday, hour, typical time or reply rate: any of them is a pattern.
    assert (insight.best_weekday, insight.best_hour, insight.typical_response_hours,
            insight.propensity) == (None, None, None, None)


def test_at_three_workspaces_the_pattern_is_shown():
    from nexus.engagement.insights.rules import describe

    insight = describe(_profile(3), viewer_consented=True)
    assert insight.level == "pattern"
    assert insight.text == "Usually replies on Tuesdays around 10am, typically within 4 hours"
    assert insight.propensity == 0.2


def test_a_workspace_that_has_not_opted_in_receives_nothing():
    from nexus.engagement.insights.rules import describe

    assert describe(_profile(9), viewer_consented=False).level == "none"
    assert describe(None, viewer_consented=True).level == "none"


def test_a_company_pattern_needs_three_workspaces_and_has_no_band():
    from nexus.engagement.insights.rules import describe

    company = {"company_domain": "acme.io", "best_weekday": 2, "best_hour": 14,
               "median_response_s": 86400 * 2, "reply_propensity": 0.1}
    assert describe({**company, "workspace_count": 2}, viewer_consented=True,
                    subject="company").level == "none"
    shown = describe({**company, "workspace_count": 3}, viewer_consented=True, subject="company")
    assert shown.text == "People there usually reply on Wednesdays around 2pm, typically within 2 days"


def test_nothing_that_could_name_another_workspace_is_read_or_returned():
    from nexus.engagement.insights import client
    from nexus.engagement.insights.rules import Insight

    assert not any("workspace" in key for key in Insight().as_dict())
    # The store's key lists never leave it: the client does not select them.
    assert "workspace_keys" not in client._PERSON_COLUMNS
    assert "workspace_keys" not in client._COMPANY_COLUMNS


# ---- likelihood and best time --------------------------------------------------------------------

def test_likelihood_comes_from_your_own_fit_and_signals():
    from nexus.engagement.insights.likelihood import likelihood

    strong = likelihood(fit=85, recent_signals=2, propensity=None)
    weak = likelihood(fit=20, recent_signals=0, propensity=None)
    assert (strong.band, weak.band) == ("high", "low")
    assert "Strong fit for your ICP (85)" in strong.reasons
    assert "2 signals in the last 30 days" in strong.reasons
    # Unscored is neutral, not zero: a new account is not a bad one.
    assert likelihood(fit=None, recent_signals=3, propensity=None).score == 0.7
    # And with nothing at all there is no answer, rather than "less likely to reply".
    blank = likelihood(fit=None, recent_signals=0, propensity=None)
    assert (blank.band, blank.score) == ("unknown", 0.0)


def test_responsiveness_moves_the_answer_only_when_a_pattern_is_allowed():
    from nexus.engagement.insights.likelihood import likelihood

    base = likelihood(fit=50, recent_signals=1, propensity=None)
    keen = likelihood(fit=50, recent_signals=1, propensity=0.3)
    quiet = likelihood(fit=50, recent_signals=1, propensity=0.01)
    assert quiet.score < base.score < keen.score
    assert "Replies to more outreach than most" in keen.reasons
    assert "Rarely replies to outreach" in quiet.reasons


def test_best_time_is_the_persons_then_the_companys_then_the_morning():
    from nexus.engagement.insights.best_time import for_campaign, for_person
    from nexus.engagement.insights.rules import Insight

    person = Insight(level="pattern", best_weekday=1, best_hour=10)
    company = Insight(level="pattern", best_hour=15)
    band_only = Insight(level="band", band="same_day")
    assert for_person(person, company).as_dict()["clock"] == "10:00"
    assert for_person(band_only, company).source == "company"
    assert for_person(band_only, None).source == "default"
    assert for_person(band_only, None).clock == "09:30"
    # One prolific replier does not set everyone's send time.
    assert for_campaign([person]).source == "default"
    assert for_campaign([person, Insight(level="pattern", best_hour=10)]).clock == "10:00"


# ---- the endpoints -------------------------------------------------------------------------------

@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


async def test_the_screens_get_likelihood_even_without_an_insights_store(client, engine_on):
    from nexus.core.db import utcnow
    from nexus.engagement.insights.client import clear_cache
    from nexus.models.account import Account, Contact
    from nexus.models.intelligence import AccountScore
    from nexus.models.signal import SignalEvent

    clear_cache()
    token = await signup(client, slug="insnostore", email="sam@insnostore.com", company="I")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        good, poor = Account(name="Acme", domain="acme.io"), Account(name="Globex", domain="globex.com")
        ts.add(good)
        ts.add(poor)
        await ts.flush()
        ts.add(AccountScore(account_id=good.id, composite=85, computed_at=utcnow()))
        ts.add(AccountScore(account_id=good.id, composite=10, computed_at=utcnow() - timedelta(days=9)))
        ts.add(AccountScore(account_id=poor.id, composite=20, computed_at=utcnow()))
        for i in range(2):
            ts.add(SignalEvent(account_id=good.id, kind="funding", source="web", title=f"Round {i}",
                               strength=0.9, occurred_at=utcnow() - timedelta(days=3),
                               dedupe_key=f"ins-{i}"))
        jane = Contact(account_id=good.id, full_name="Jane", email="jane@acme.io")
        ken = Contact(account_id=poor.id, full_name="Ken", email="ken@globex.com")
        ts.add(jane)
        ts.add(ken)
        await ts.flush()
        ids = [jane.id, ken.id]

    r = await client.get("/api/engagement/insights/contacts", headers=auth(token),
                         params={"ids": ",".join(ids)})
    assert r.status_code == 200, r.text
    rows = {row["contact_id"]: row for row in r.json()}
    # The LATEST score counts, not an older one.
    assert rows[ids[0]]["likelihood"]["band"] == "high"
    assert rows[ids[1]]["likelihood"]["band"] == "low"
    assert rows[ids[0]]["person"]["level"] == "none"
    assert rows[ids[0]]["best_time"]["source"] == "default"

    too_many = ",".join(f"c{i}" for i in range(101))
    assert (await client.get("/api/engagement/insights/contacts", headers=auth(token),
                             params={"ids": too_many})).status_code == 422


async def test_insights_are_dark_with_the_engine(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    token = await signup(client, slug="insdark", email="sam@insdark.com", company="D")
    r = await client.get("/api/engagement/insights/contacts", headers=auth(token),
                         params={"ids": "x"})
    assert r.status_code == 404
```

- [ ] **Step 2:** The structural checks appended to `tests/test_engagement_screens_ui.py`:

```diff
diff --git a/tests/test_engagement_screens_ui.py b/tests/test_engagement_screens_ui.py
index 8c4f7bf..e94d1e4 100644
--- a/tests/test_engagement_screens_ui.py
+++ b/tests/test_engagement_screens_ui.py
@@ -125,2 +125,31 @@ def test_the_funnel_is_one_hue_with_bounces_beside_it_not_in_it():
 def test_my_mailboxes_shows_this_weeks_bounce_rate():
     assert "<MailboxHealth mailbox={mailbox} />" in _read(PAGES / "MailboxesPage.tsx")
+
+
+# ---- insights (phase 13) -------------------------------------------------------------------------
+
+COMPONENTS = SRC / "components" / "engagement"
+
+
+def test_the_screens_never_handle_anything_that_could_name_another_workspace():
+    """The server applies D26; the client must not even have the fields that would undo it."""
+    for path in list(COMPONENTS.glob("*.ts*")) + list(PAGES.glob("*.tsx")) + [SRC / "lib" / "types.ts"]:
+        source = _read(path)
+        assert "workspace_count" not in source and "workspace_keys" not in source, path.name
+
+
+def test_insights_are_fetched_once_per_list_not_once_per_row():
+    for path in (PAGES / "ReviewQueue.tsx", COMPONENTS / "ContactPicker.tsx",
+                 COMPONENTS / "AccountConversations.tsx"):
+        assert "useContactInsights(" in _read(path), path.name
+    api = _read(SRC / "lib" / "api.ts")
+    assert 'query: { ids: contactIds.slice(0, 100).join(",") }' in api
+
+
+def test_a_set_time_step_offers_the_best_time():
+    editor = _read(COMPONENTS / "StepsEditor.tsx")
+    assert '{step.timing_mode === "manual" && (\n                  <BestTimeHint' in editor
+    detail = _read(PAGES / "CampaignDetailPage.tsx")
+    assert "bestTime={best.data ?? undefined}" in detail
+    # Moving one person converts THEIR clock into the viewer's local input.
+    assert "zonedClockToLocalInput(" in detail and "moving.contact_timezone" in detail
```

- [ ] **Step 3: Run** `pytest tests/test_engagement_*.py tests/test_refresh_tiering.py tests/test_outcomes.py tests/test_analytics_activity.py tests/test_rep_dashboard.py tests/test_billing_metering_coverage.py tests/test_plan_gated_nav.py tests/test_credential_leaks.py -q -n 6` — expected PASS; `ruff check nexus tests tests_integration` — clean.

- [ ] **Step 4: See it.** In the isolated preview (phase 11, Task 7), give the seeded accounts varied scores (one strong with two recent signals, one partial, one weak). The review queue shows "Likely to reply" and "Less likely to reply" with reasons on hover; switching a step to "At a set time" shows the best-time line; the account's Emails tab shows "How they reply"; a workspace where nothing is scored shows no badge and no "How they reply" card.

- [ ] **Step 5: Commit**

```bash
git add -A frontend/src nexus tests tests_integration docs/superpowers/plans/2026-09-17-sdr-engagement/13-insights.md
git commit -m "feat(engagement): phase 13 - insights on the screens, Today ranked by likelihood"
```

---

## What phase 14 depends on

`service.for_contacts` is the one way to ask about a batch of people; the signal re-engagement in phase 14 uses `likelihood` to choose which paused or finished people a new signal is worth waking, and the best time to place the email it drafts.
