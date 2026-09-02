# nexus/billing/entitlements.py
"""The entitlement engine: resolve policy, decide, and meter — the ONE billing seam.

Resolution order (docs/billing/02-Entitlement-Engine.md §2):
    plan class 'unlimited'  ->  plan entitlement  ->  catalog default  ->  unknown (allow)

Everything about this module is biased toward NOT breaking the product: unknown capabilities,
missing subscriptions, and internal errors all resolve to "allow".
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from nexus.core import metrics
from nexus.core.tenancy import TenantSession
from nexus.models.billing import (
    BillingCapability,
    BillingPlan,
    BillingPlanEntitlement,
    BillingSubscription,
)

logger = logging.getLogger("nexus.billing.entitlements")

# Plan classes that are never limited: they exist to observe cost, not to gate features.
_UNLIMITED_CLASSES = {"unlimited", "internal", "partner"}
# Subscription states in which entitlements still apply normally.
_LIVE_STATUSES = {"trialing", "active", "past_due"}


@dataclass(slots=True)
class ResolvedEntitlement:
    """The effective policy for one tenant x capability, plus where it came from (for admin
    debugging and the 402 payload)."""

    capability_id: str
    mode: str                       # shadow|enabled|metered|unlimited|disabled|enterprise
    quota: int | None = None
    soft_limit_pct: int = 80
    hard_limit: int | None = None
    overage_price_credits: int | None = None
    cooldown_s: int | None = None
    burst_limit: int | None = None
    reset_policy: str = "monthly_anniversary"
    depends_on: tuple[str, ...] = ()
    unit: str = "action"
    plan_id: str | None = None
    source: str = "catalog"         # plan_class | plan | catalog | unknown


# Platform-wide per-minute ceilings for capabilities a person triggers ONE AT A TIME.
#
# `burst_limit` has been read by this engine since M2 and was set on no plan entitlement anywhere,
# so the throttle never fired: a tenant looping an agent endpoint could drive unbounded COGS with
# nothing but the monthly quota in the way.
#
# Deliberately narrow, because the wrong entry here is an outage rather than a saving:
#
#   * `search.web` genuinely runs hundreds of times a minute for one tenant during a crawl — the
#     dork source alone issues several queries per account across a 100-account batch.
#   * The bulk paths (`verify.email`, `enrich.account`, `enrich.contact`) record ONE event with
#     `quantity=N`, not N events, and `_over_burst` counts events. A limit there would either do
#     nothing or refuse a legitimate sweep, depending on batch size.
#   * `ai.scoring` and `ai.tokens` are recorded alongside other work rather than requested.
#
# What is left is the set a human asks for and waits on. The numbers are set where no real
# workflow reaches them: they exist to stop a runaway loop, not to shape usage — quotas do that.
# A plan may still override per capability, in either direction.
DEFAULT_BURST_LIMITS: dict[str, int] = {
    "ai.email_draft": 120,
    "ai.call_script": 120,
    "ai.chat_turn": 120,
    "ai.research_brief": 60,
    "ai.account_qa": 120,
    "ai.icp_from_website": 60,
}


async def _active_subscription(ts: TenantSession) -> BillingSubscription | None:
    rows = await ts.list(BillingSubscription, limit=5)
    live = [s for s in rows if s.status in _LIVE_STATUSES]
    return live[0] if live else (rows[0] if rows else None)


async def resolve_entitlement(
    ts: TenantSession, capability_id: str, *, _depth: int = 0
) -> ResolvedEntitlement:
    """Compute the effective entitlement. Never raises.

    A capability whose ``depends_on`` module is disabled is itself disabled: that is what makes
    a module gate ("this plan does not include Network") actually gate, rather than being a note
    in the catalog.
    """
    try:
        cap = await ts.session.get(BillingCapability, capability_id)
        if cap is None:
            # Unregistered capability: allow and record. This is what makes shipping the engine
            # incapable of breaking a feature nobody remembered to catalog.
            return ResolvedEntitlement(capability_id, mode="shadow", source="unknown")

        base = ResolvedEntitlement(
            capability_id=capability_id,
            mode=cap.default_mode,
            unit=cap.unit,
            depends_on=tuple(cap.depends_on or ()),
            source="catalog",
            # Platform default. A plan entitlement overrides it below when it names one; an
            # `unlimited` plan class returns before this is ever consulted, because a throttle on
            # a plan that exists to observe cost is still a gate.
            burst_limit=DEFAULT_BURST_LIMITS.get(capability_id),
        )

        sub = await _active_subscription(ts)
        if sub is None:
            return await _apply_dependencies(ts, base, _depth)
        base.plan_id = sub.plan_id

        if sub.status == "suspended":
            # A pause that keeps full access is free service — the same failure as a trial that
            # never ends. Note this is still subject to NEXUS_BILLING_ENFORCEMENT: in the default
            # shadow mode it is recorded and not blocked, so arming pause and arming enforcement
            # stay one decision rather than two.
            base.mode = "disabled"
            base.quota = 0
            base.source = "suspended"
            return base

        plan = await ts.session.get(BillingPlan, sub.plan_id)
        if plan is not None and plan.plan_class in _UNLIMITED_CLASSES:
            base.mode = "unlimited"
            base.quota = None
            base.burst_limit = None    # a throttle is a gate; this class exists not to gate
            base.source = "plan_class"
            return base        # unlimited classes bypass module gates by definition

        ent = (
            await ts.session.scalars(
                select(BillingPlanEntitlement).where(
                    BillingPlanEntitlement.plan_id == sub.plan_id,
                    BillingPlanEntitlement.capability_id == capability_id,
                )
            )
        ).first()
        if ent is None:
            return await _apply_dependencies(ts, base, _depth)

        base.mode = ent.mode
        base.quota = ent.quota
        base.soft_limit_pct = ent.soft_limit_pct
        base.hard_limit = ent.hard_limit
        base.overage_price_credits = ent.overage_price_credits
        base.cooldown_s = ent.cooldown_s
        # `or` rather than a straight assignment: a plan that simply does not mention a burst
        # keeps the platform default, while one that names a number — higher or lower — wins.
        base.burst_limit = ent.burst_limit if ent.burst_limit is not None else base.burst_limit
        base.reset_policy = ent.reset_policy
        base.source = "plan"

        # M24: `feature_flag` has been stored, admin-editable and copied by the custom-plan builder
        # since the schema was written, and was never read — the same dead-config class that
        # `burst_limit` and `depends_on` were in. A flag that is off disables the capability the
        # same way a plan entitlement of `disabled` does, so the 402 payload and the admin debug
        # view both explain it without a special case.
        if ent.feature_flag:
            from nexus.billing.flags import flag_enabled
            from nexus.core.config import get_settings

            if not await flag_enabled(ts, ent.feature_flag, env=get_settings().env):
                base.mode = "disabled"
                base.source = "feature_flag"
                return base

        return await _apply_dependencies(ts, base, _depth)
    except Exception:  # resolution failure must degrade to allow, never to a 500
        logger.warning("entitlement resolution failed for %s", capability_id, exc_info=True)
        return ResolvedEntitlement(capability_id, mode="shadow", source="unknown")


# ---- the seam ------------------------------------------------------------------------------
@dataclass(slots=True)
class MeterResult:
    """Outcome of one seam call. Callers only ever need ``allowed``."""

    allowed: bool
    recorded: bool = False
    reason: str | None = None        # quota_exhausted | disabled | dependency | throttled
    would_block: bool = False        # True when shadow mode suppressed a real block
    used: float = 0
    quota: int | None = None
    entitlement: "ResolvedEntitlement | None" = None

    def raise_if_blocked(self) -> None:
        """Convenience for routers: turn a block into the typed HTTP-mappable exception."""
        if self.allowed:
            return
        from nexus.billing.errors import BillingThrottled, QuotaExceeded

        ent = self.entitlement
        if self.reason == "throttled":
            # Rate, not entitlement: 429 with a retry hint, never a 402 upsell. Telling someone
            # to upgrade when they simply need to slow down is the wrong instruction.
            raise BillingThrottled(
                ent.capability_id if ent else "unknown", retry_after_s=60
            )
        raise QuotaExceeded(
            ent.capability_id if ent else "unknown",
            reason=self.reason or "quota_exhausted",
            used=self.used,
            quota=self.quota,
            plan_id=ent.plan_id if ent else None,
        )


async def current_usage(ts: TenantSession, capability_id: str) -> float:
    """Authoritative usage for the current billing period.

    Reads the ``period`` rollup (a single indexed row) and adds the events that rollup has not
    folded in yet, identified by ``rolled_at IS NULL`` rather than by comparing timestamps.
    Both a rollup's write time and an event's are stamped from Python's clock, and on a coarse
    timer (Windows ticks at ~15ms) they can land on the same value — a tie drops a real event
    from the count, which under enforcement hands a tenant free quota. A marker cannot tie.

    It is also liveness-safe: if the rollup worker stops, every event simply stays unrolled and
    is summed live, so the answer remains exact and merely gets slower — it can never drift
    downward. Postgres — never a cache — is the source of truth for hard limits
    (docs/billing/02-Entitlement-Engine.md §4).
    """
    from sqlalchemy import func

    from nexus.billing.rollups import period_key, period_start
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent, BillingUsageRollup

    # Gauges answer "how many exist right now", not "how many happened this period". Summing
    # events would only ever climb: remove a member and a counter still shows the old seat count,
    # so the customer could never get back under their limit.
    gauge = _GAUGE_RESOLVERS.get(capability_id)
    if gauge is not None:
        return await gauge(ts)

    now = utcnow()
    rollup = await ts.first(
        BillingUsageRollup,
        BillingUsageRollup.capability_id == capability_id,
        BillingUsageRollup.period_kind == "period",
        BillingUsageRollup.period_key == period_key(now, "period"),
    )
    total = float(rollup.quantity) if rollup is not None else 0.0

    unrolled = await ts.session.scalar(
        select(func.coalesce(func.sum(BillingUsageEvent.quantity), 0)).where(
            BillingUsageEvent.tenant_id == ts.tenant_id,
            BillingUsageEvent.capability_id == capability_id,
            BillingUsageEvent.rolled_at.is_(None),
            # Stragglers from a previous period belong to that period's invoice, not this
            # period's quota.
            BillingUsageEvent.occurred_at >= period_start(now),
        )
    )
    return total + float(unrolled or 0)


# A single action cannot plausibly consume more than this. The cap is a blast radius limit, not
# a business rule: it stops one bad caller (or a unit-conversion bug) from draining a balance or
# poisoning a rollup with an absurd number.
MAX_QUANTITY_PER_CALL = 1_000_000


async def _count_seats(ts: TenantSession) -> float:
    """Live members of this workspace. `seat.member` is a gauge, not a counter."""
    from sqlalchemy import func

    from nexus.models.identity import Membership

    total = await ts.session.scalar(
        select(func.count(Membership.id)).where(Membership.tenant_id == ts.tenant_id)
    )
    return float(total or 0)


# capability_id -> resolver returning the CURRENT level rather than a period total.
_GAUGE_RESOLVERS = {"seat.member": _count_seats}


def _valid_quantity(quantity: float) -> bool:
    """Reject anything that would corrupt the counters.

    Negative quantity is the interesting one: usage is summed, so a negative would *reduce*
    recorded usage and hand back quota. Compensating rows are written by the refund path with
    an explicit key, never through the gate.
    """
    try:
        q = float(quantity)
    except (TypeError, ValueError):
        return False
    if q != q or q in (float("inf"), float("-inf")):   # NaN / infinity
        return False
    return 0 < q <= MAX_QUANTITY_PER_CALL


async def _lock_capability(ts: TenantSession, capability_id: str) -> None:
    """Serialize check-then-record for one tenant+capability.

    Without this, two concurrent requests both read `used` before either writes, both conclude
    there is room, and the tenant spends past the limit. The window is small but it is exactly
    where a determined caller aims: fire N parallel requests at a quota with 1 unit left.

    Transaction-scoped, so it releases on commit or rollback and cannot leak. Postgres only;
    SQLite serializes writers anyway, so there is nothing to guard in tests.
    """
    try:
        bind = ts.session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        from sqlalchemy import text

        await ts.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"meter:{ts.tenant_id}:{capability_id}"},
        )
    except Exception:
        # A lock we could not take must not break metering; worst case we are back to the
        # pre-existing race rather than failing the request.
        logger.warning("advisory lock failed for %s", capability_id, exc_info=True)


async def _over_burst(ts: TenantSession, capability_id: str, burst_limit: int) -> bool:
    """True when this capability was used more than ``burst_limit`` times in the last minute.

    Counts events rather than summing quantity: a burst limit is about request rate, not volume.
    Uses ix_usage_tenant_cap_time, so it is an indexed range count. Any failure returns False —
    a throttle check that errors must not become a block.
    """
    from datetime import timedelta

    from sqlalchemy import func

    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent

    try:
        since = utcnow() - timedelta(seconds=60)
        recent = await ts.session.scalar(
            select(func.count(BillingUsageEvent.id)).where(
                BillingUsageEvent.tenant_id == ts.tenant_id,
                BillingUsageEvent.capability_id == capability_id,
                BillingUsageEvent.occurred_at >= since,
            )
        )
        return int(recent or 0) >= int(burst_limit)
    except Exception:
        logger.warning("burst check failed for %s", capability_id, exc_info=True)
        return False


_MAX_DEPENDENCY_DEPTH = 4


async def _apply_dependencies(
    ts: TenantSession, ent: ResolvedEntitlement, depth: int
) -> ResolvedEntitlement:
    """Disable a capability whose required module is disabled.

    The seed is a flat two-level structure (capability -> module), so cycles are not reachable;
    the depth guard is here so a future catalog edit degrades to "allow" instead of recursing
    forever inside the metering hot path.
    """
    if not ent.depends_on or depth >= _MAX_DEPENDENCY_DEPTH:
        return ent
    if ent.source == "plan":
        # The plan named this capability explicitly, with its own mode and quota. That is a
        # deliberate commercial decision and outranks a blanket module gate — Free disables
        # module.outreach yet still sells 20 ai.email_drafts, and it means the 20. Module gates
        # are the default for capabilities a plan does NOT mention.
        return ent
    for dep in ent.depends_on:
        if dep == ent.capability_id:
            continue
        resolved = await resolve_entitlement(ts, dep, _depth=depth + 1)
        # An unknown dependency resolves to "shadow" and is deliberately not a block: unknown
        # always means allow, or cataloging a capability late could break a shipped feature.
        if resolved.mode == "disabled":
            ent.mode = "disabled"
            ent.source = "dependency"
            return ent
    return ent


async def _burn_for_overage(
    ts: TenantSession, ent: "ResolvedEntitlement", over_units: float, key: str,
    *, prior_over: float = 0.0,
) -> bool:
    """Spend credits for units beyond the plan's included quota.

    Returns True when the overage is paid for. Price precedence matches the invoice rating path
    (plan entitlement, else the global rate card), so what is spent in flight and what is billed
    at period close cannot disagree.

    ``prior_over`` is how far past the quota the tenant already was before this call. It only
    matters for a card carrying a VOLUME LADDER, where the price of a unit depends on how many
    came before it: `rating.rate_period` prices the whole period's overage in one
    `tiered_credits` call at close, so an in-flight burn has to charge the MARGINAL cost of these
    units at their real position on the ladder rather than re-pricing them from the first tier.
    Reading `credits_per_unit` flat — which is what this did — overcharges the moment a ladder
    exists: 8 units against a 5-at-2-then-1 card cost 16 in flight and 13 on the invoice.

    This is the same distinction as the caller's `over = min(quantity, ...)`: what THIS request
    adds, not the running total.

    Never raises: a burn failure degrades to "not covered", and the caller decides. Losing the
    charge on one action beats breaking the product.
    """
    from nexus.billing.credits import burn_credits
    from nexus.models.billing import BillingCreditLedger, BillingRateCard

    burn_key = f"{key}:burn"
    try:
        # A retried request re-derives the same overage. Treat an existing burn as covered,
        # otherwise the retry would be refused for an action already paid for.
        already = await ts.first(
            BillingCreditLedger, BillingCreditLedger.idempotency_key == burn_key
        )
        if already is not None:
            return True

        price = ent.overage_price_credits
        if price is not None:
            # A negotiated per-unit rate on the plan. Flat by definition — it exists so a deal
            # does not have to fork the catalog — and it outranks the card, ladder and all.
            amount = float(over_units) * float(price)
        else:
            card = await ts.session.get(BillingRateCard, ent.capability_id)
            if card is None or not card.active:
                return False        # unpriced capability: nothing to charge against
            if card.tiers:
                from nexus.billing.rating import tiered_credits

                before = tiered_credits(float(prior_over), card)
                after = tiered_credits(float(prior_over) + float(over_units), card)
                amount = after - before
            else:
                amount = float(over_units) * float(card.credits_per_unit)

        if amount <= 0:
            return False
        from nexus.billing.rollups import period_key as _period_key
        from nexus.core.db import utcnow

        return await burn_credits(
            ts, amount, reason=f"{ent.capability_id} beyond plan quota",
            idempotency_key=burn_key, capability_id=ent.capability_id,
            # Stamped with the period so rating can tell what this period's overage already
            # paid for and avoid charging it a second time on the invoice.
            period_key=_period_key(utcnow(), "period"),
        )
    except Exception:
        logger.warning("credit burn failed for %s", ent.capability_id, exc_info=True)
        return False


async def _credits_exhausted(ts, ent) -> bool:
    """Is this tenant out of credits, on a plan that is funded by them?

    False for every case that is not unambiguously "credit-funded plan, nothing left, and this call
    would cost something". The money path punishes false positives far more than false negatives: a
    wrong block is an outage for a paying customer, while a missed one costs a few credits somebody
    has already been granted.

    Never raises. A balance lookup that fails must not take down the call it was guarding — the
    same posture the rest of this module takes.
    """
    try:
        plan_id = getattr(ent, "plan_id", None)
        if not plan_id:
            return False                     # no subscription resolved: engine default is allow

        # `module.*` gates price nothing. Blocking one revokes a feature the customer still has,
        # and it is the difference between "you are out of credits" and "you lost Campaigns".
        capability_id = getattr(ent, "capability_id", "") or ""
        if capability_id.startswith("module."):
            return False

        plan = await ts.session.get(BillingPlan, plan_id)
        if plan is None or not plan.included_credits:
            # Not credit-funded. legacy-unlimited, enterprise and custom deals are invoiced on
            # other terms and must never be stopped by a balance they were never given.
            return False

        # An explicit overage price means "keep going and invoice it", which is the same rule the
        # quota branch above applies. Stopping such a tenant would contradict their own plan.
        if getattr(ent, "overage_price_credits", None) is not None:
            return False

        # Nothing to charge means nothing to run out of.
        from nexus.models.billing import BillingRateCard

        card = await ts.session.get(BillingRateCard, capability_id)
        if card is None or not card.active or not float(card.credits_per_unit or 0):
            return False

        from nexus.billing.credits import balance

        return await balance(ts) <= 0
    except Exception:  # a guard must not break the call it guards
        logger.warning("credit-floor check failed for %s", getattr(ent, "capability_id", "?"),
                       exc_info=True)
        return False


async def check_and_meter(
    ts: TenantSession,
    *,
    capability_id: str,
    quantity: float = 1,
    user_id: str | None = None,
    source: str = "api",
    idempotency_key: str | None = None,
    attrs: dict | None = None,
) -> MeterResult:
    """THE billing seam. Resolve entitlement, decide, record usage.

    Application code calls exactly this and never mentions a plan. Behavior is governed by
    ``NEXUS_BILLING_ENFORCEMENT``:
      off    -> pure passthrough (no evaluation, no recording)
      shadow -> evaluate + record, but ALWAYS allow (``would_block`` reports what would happen)
      on     -> evaluate + record + enforce

    Never raises: any internal failure degrades to allow (docs/billing/01 §6).
    """
    from nexus.billing.usage import record_usage
    from nexus.core.config import get_settings

    mode = get_settings().billing_enforcement
    if mode == "off":
        return MeterResult(allowed=True, recorded=False)

    if not _valid_quantity(quantity):
        # Do not record, do not gate — a nonsensical quantity is a caller bug or an attempt to
        # rewind the counter. Allow the action (metering never breaks the product) but bill
        # nothing, and make it visible.
        logger.warning(
            "rejecting invalid quantity %r for %s; not recorded", quantity, capability_id
        )
        return MeterResult(allowed=True, recorded=False, reason="invalid_quantity")

    try:
        _t0 = time.perf_counter()
        ent = await resolve_entitlement(ts, capability_id)
        metrics.observe_entitlement_resolve(time.perf_counter() - _t0)

        # One key for the whole decision, so the usage row and the credit burn either both
        # apply or both no-op on a retry. record_usage would otherwise mint its own.
        key = idempotency_key or f"auto:{uuid.uuid4().hex}"

        blocked_reason: str | None = None
        used = 0.0
        if ent.mode == "disabled":
            blocked_reason = "disabled" if ent.source != "dependency" else "dependency"
        elif ent.mode in ("shadow", "enabled", "unlimited"):
            blocked_reason = None            # never quota-limited
        elif ent.quota is not None:
            # Serialize before reading the counter: check-then-record is only safe if no other
            # request can slip between the two.
            await _lock_capability(ts, capability_id)
            used = await current_usage(ts, capability_id)
            limit = ent.hard_limit if ent.hard_limit is not None else ent.quota
            # The part of THIS call that falls beyond the line — not the running total by which
            # the tenant is over it. `used + quantity - limit` is the second thing, and it grows
            # by one on every subsequent call, so the Nth overage unit was charged N times its
            # price: 20 units past a quota cost 210 units' worth of credits instead of 20. Every
            # pre-existing test fired exactly one call past the quota, where the two expressions
            # coincide. Clamped to `quantity` so a call that starts inside the quota pays only
            # for the part that crosses.
            over = min(float(quantity), used + quantity - limit)
            if over > 0:
                # Past what the plan includes. Spend credits first — that is what they are for.
                # An explicit overage price means "keep going and invoice it", not "stop", so it
                # still passes when the balance cannot cover it. Otherwise this is the wall.
                # `used - limit` is how far past the line the tenant already was, which is
                # where this request's units sit on a volume ladder.
                covered = await _burn_for_overage(
                    ts, ent, over, key, prior_over=max(0.0, used - limit)
                )
                if not covered and ent.overage_price_credits is None:
                    blocked_reason = "quota_exhausted"

        # THE CREDIT FLOOR. A plan funded by credits stops when they run out.
        #
        # Per-capability quota alone does not achieve that, and the gap was total: the `free` plan
        # lists 9 capabilities, and `resolve_entitlement` falls back to permissive catalog defaults
        # for the other 61 — where `default_quota` is None for EVERY capability in the seed. So the
        # branch above is skipped (`ent.quota is not None` is False), `enabled` capabilities are
        # explicitly "never quota-limited", and a free tenant could run unlimited enrichment,
        # search and research while its 200 credits sat untouched. The credits were decorative for
        # 61 of 70 capabilities.
        #
        # Scoped tightly, because this is the money path and a false block is an outage:
        #   * only plans that ARE credit-funded (`included_credits > 0`) — legacy-unlimited,
        #     enterprise and custom deals are invoiced differently and must be untouched;
        #   * only capabilities that actually cost credits (a live rate card). A `module.*` gate
        #     prices nothing, and blocking one would revoke a feature the customer still has;
        #   * never in shadow mode — `enforced` below decides that, exactly as for quota.
        if blocked_reason is None and ent.mode not in ("disabled", "shadow"):
            if await _credits_exhausted(ts, ent):
                blocked_reason = "credits_exhausted"

        # Burst is a separate axis from quota: a tenant well inside its monthly allowance can
        # still hammer an endpoint. Only queried for capabilities that set a limit, so it costs
        # nothing on the rest.
        if blocked_reason is None and ent.burst_limit is not None:
            if await _over_burst(ts, capability_id, ent.burst_limit):
                blocked_reason = "throttled"

        enforced = mode == "on"
        allowed = True if not enforced else blocked_reason is None

        # The one number that decides whether enforcement can be switched on. In shadow mode the
        # engine computes `blocked_reason` on every call and then discards it, so without this
        # counter "what happens if we flip the switch?" can only be answered by flipping it.
        if blocked_reason is None:
            metrics.record_billing_decision(capability_id, "allowed")
        else:
            metrics.record_billing_decision(
                capability_id, "blocked" if not allowed else "would_block", blocked_reason
            )

        recorded = False
        if allowed:
            recorded = await record_usage(
                ts, capability_id=capability_id, quantity=quantity, unit=ent.unit,
                user_id=user_id, source=source, idempotency_key=key, attrs=attrs,
            )
        return MeterResult(
            allowed=allowed,
            recorded=recorded,
            reason=blocked_reason if not allowed else None,
            would_block=blocked_reason is not None,
            used=used,
            quota=ent.quota,
            entitlement=ent,
        )
    except Exception:  # the seam must never break the product
        logger.warning("check_and_meter failed for %s", capability_id, exc_info=True)
        # Counted, because "the engine is erroring and therefore allowing everything" looks
        # exactly like "nobody is hitting a limit" on every other metric.
        metrics.record_billing_decision(capability_id, "error")
        return MeterResult(allowed=True, recorded=False)
