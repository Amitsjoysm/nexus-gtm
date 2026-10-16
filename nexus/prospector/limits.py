"""Prospector's cost guards: how often one person may search, how much one workspace may browse.

Browsing masked results is free to the customer (D3) and paid per row returned by us, so without
these one script could run up the provider bill on a workspace that has paid nothing.

* **Rate** (D21): ``prospector_rate_limit_per_minute`` searches per person per minute, through
  ``core.ratelimit.hit`` (Valkey when reachable, in-process otherwise; never fails closed).
* **Daily rows** (D11, D22): rows served to a workspace per UTC day, counted in
  ``prospector_daily_rows``. The limit is the quota on the workspace's plan entitlement for
  ``prospector.daily_rows`` (set per plan, or per customer through a custom plan), else
  ``prospector_daily_rows_default``. Enforced whatever ``NEXUS_BILLING_ENFORCEMENT`` says: it is a
  cost guard, not a billing rule, and ``off`` would otherwise make it vanish.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from nexus.core.db import utcnow
from nexus.models.prospector import ProspectorDailyRows
from nexus.prospector.errors import ProspectorError

LIMIT_CAPABILITY = "prospector.daily_rows"


def _settings():
    from nexus.core.config import get_settings

    return get_settings()


async def check_rate(tenant_id: str, user_id: str) -> None:
    from nexus.core import ratelimit

    limit = max(1, int(_settings().prospector_rate_limit_per_minute))
    retry = await ratelimit.hit(f"prospector:{tenant_id}:{user_id}", window=60, limit=limit)
    if retry is not None:
        raise ProspectorError(
            429, f"Too many searches in a minute. Try again in {retry} seconds.",
            headers={"Retry-After": str(max(1, int(retry)))})


def _today() -> str:
    return utcnow().date().isoformat()


def _seconds_to_midnight() -> int:
    now = utcnow()
    midnight = datetime.combine(now.date() + timedelta(days=1), time(0), tzinfo=timezone.utc)
    return max(1, int((midnight - now).total_seconds()))


async def daily_limit(ts) -> int:
    from nexus.billing.entitlements import resolve_entitlement

    default = max(1, int(_settings().prospector_daily_rows_default))
    try:
        ent = await resolve_entitlement(ts, LIMIT_CAPABILITY)
    except Exception:  # a billing read must never stop a search; fall back to the default
        return default
    quota = getattr(ent, "quota", None)
    if ent is not None and ent.source == "plan" and isinstance(quota, int) and quota > 0:
        return quota
    return default


async def rows_today(ts) -> int:
    row = await ts.first(ProspectorDailyRows, ProspectorDailyRows.day == _today())
    return int(row.rows) if row is not None else 0


async def ensure_budget(ts, rows: int) -> int:
    """Rows still available today, after checking ``rows`` more fit. Raises 429 when they do not."""
    limit = await daily_limit(ts)
    used = await rows_today(ts)
    if used + max(0, rows) > limit:
        raise ProspectorError(
            429, f"Today's Prospector browsing limit ({limit:,} rows) is used up. It resets at "
                 "00:00 UTC; an admin can ask for a higher limit.",
            headers={"Retry-After": str(_seconds_to_midnight())})
    return limit - used


async def remaining(ts) -> int:
    return max(0, await daily_limit(ts) - await rows_today(ts))


async def count_rows(ts, rows: int) -> None:
    """Add ``rows`` to today's count. Creates the day's row under a savepoint, so two requests
    racing to create it cannot fail the transaction around them."""
    if rows <= 0:
        return
    day = _today()
    row = await ts.first(ProspectorDailyRows, ProspectorDailyRows.day == day)
    if row is None:
        try:
            async with ts.session.begin_nested():
                ts.add(ProspectorDailyRows(tenant_id=ts.tenant_id, day=day, rows=rows))
                await ts.flush()
            return
        except IntegrityError:
            row = await ts.first(ProspectorDailyRows, ProspectorDailyRows.day == day)
            if row is None:
                return
    row.rows = int(row.rows or 0) + rows
    await ts.flush()
