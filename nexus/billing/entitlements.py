# nexus/billing/entitlements.py
"""The entitlement engine: resolve policy, decide, and meter — the ONE billing seam.

Resolution order (docs/billing/02-Entitlement-Engine.md §2):
    platform switch  ->  plan class 'unlimited'  ->  plan entitlement  ->  catalog default
    ->  unknown (allow)

The platform switch is FIRST because it is the only step that means "off for everyone". Every other
step answers "what did this customer buy?", and two of them short-circuit — an `unlimited` plan
class and a tenant with no subscription both return early — so a switch checked later would not
apply to `legacy-unlimited`, which is every pre-billing tenant.

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
from nexus.features.switches import switch_for
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
    source: str = "catalog"         # plan_class | plan | catalog | unknown | feature_switch
    # Set only when `source == "feature_switch"`. Carried on the entitlement rather than looked up
    # again by the API, because the state and the message are the ONLY things that distinguish
    # "not built yet" from "temporarily broken" from "your plan lacks this" — three sentences that
    # share one `mode="disabled"`. A UI that cannot tell them apart offers an upgrade for a feature
    # no plan sells.
    switch_state: str | None = None
    switch_message: str = ""


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
    """The subscription every entitlement decision resolves against. NEWEST FIRST.

    "One subscription per tenant" is what makes rating unambiguous, and `change_plan` switches the
    existing row rather than opening a second one precisely to keep it true. But nothing enforces
    it at the database level, and this function — which decides what EVERY request is entitled to —
    used to take `live[0]` from an unordered `ts.list`. With two live rows it would resolve against
    whichever the database happened to return first, so a tenant could be billed and gated against
    a plan they had left.

    Ordering by `created_at` descending matches `subscriptions._active`, so the two modules cannot
    disagree about which subscription is current. It does not make two rows correct; it makes the
    consequence deterministic and puts both readers on the same answer.
    """
    from sqlalchemy import select as _select

    rows = list(
        await ts.session.scalars(
            _select(BillingSubscription)
            .where(BillingSubscription.tenant_id == ts.tenant_id)
            .order_by(BillingSubscription.created_at.desc(), BillingSubscription.id.desc())
            .limit(5)
        )
    )
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
        # PLATFORM SWITCH FIRST — before the catalog lookup, before the plan, before every early
        # return below. A switch says "this feature is off for everyone", so anything that can
        # return before it is a way to keep using a feature the platform has taken down. The two
        # that would: an `unlimited` plan class bypasses module gates by definition (and that is
        # `legacy-unlimited`, i.e. every pre-billing tenant), and "no subscription -> allow".
        #
        # It is a `module.*` id by convention, but nothing here enforces that — a switch on a
        # narrower capability is a legitimate way to take one expensive action offline without
        # removing the page around it.
        try:
            sw = await switch_for(capability_id)
        except Exception:  # a switch is a restriction; failing to read one applies none
            logger.warning("feature switch lookup failed for %s", capability_id, exc_info=True)
            sw = None
        if sw is not None and sw.blocks:
            return ResolvedEntitlement(
                capability_id,
                mode="disabled",
                quota=0,
                source="feature_switch",
                # `switch_state` and `switch_message` ride along so the API can tell the customer
                # WHICH kind of off this is. "Coming soon" and "we broke it" are the same
                # entitlement and completely different sentences, and a UI that cannot distinguish
                # them shows an upgrade prompt for a feature no plan sells yet.
                switch_state=sw.state,
                switch_message=sw.message,
            )

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

        # A SWITCH ON A MODULE THIS CAPABILITY DEPENDS ON, checked HERE rather than left to
        # `_apply_dependencies` at the bottom — because three of the paths below return before
        # `_apply_dependencies` ever runs, and each was an escape:
        #
        #   * an `unlimited` plan class, which is `legacy-unlimited`, i.e. EVERY pre-billing
        #     tenant. Measured on the deployment: `module.agents` switched to maintenance, the
        #     entitlements endpoint correctly reported it locked, the sidebar hid the page, and
        #     `POST /orchestration/runs` returned 201 ten times out of ten and billed each one;
        #   * "no subscription -> allow", a regression guard that must not become a way to keep
        #     using a feature the platform has taken down;
        #   * a suspended subscription, which blocks anyway but reported `source="suspended"` —
        #     telling the customer their workspace is paused when the truth is that we took the
        #     feature offline for everyone.
        #
        # The direct check above only covers a switch on the capability ITSELF. This is what makes
        # a switch on `module.agents` actually stop the orchestration endpoints, which is the
        # difference between disabling a feature and hiding its menu item.
        dep_sw = await _switched_off_dependency(base.depends_on, capability_id)
        if dep_sw is not None:
            base.mode = "disabled"
            base.quota = 0
            base.source = "feature_switch"
            base.switch_state = dep_sw.state
            base.switch_message = dep_sw.message
            return base

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
            # Carried so the client can say WHY, rather than falling back to the generic upsell.
            # A platform switch is our decision, not a limit the customer hit, and inviting them to
            # upgrade out of our own maintenance window is the wrong instruction.
            switch_state=ent.switch_state if ent else None,
            switch_message=ent.switch_message if ent else "",
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


async def _switched_off_dependency(
    depends_on: tuple[str, ...], capability_id: str
):
    """The first blocking platform switch among this capability's dependencies, else ``None``.

    Reads switches ONLY — it deliberately does not resolve the dependency's full entitlement. A
    plan-driven module gate still belongs to `_apply_dependencies`, where the plan escape ("Free
    disables module.outreach yet still sells 20 email drafts") correctly applies. A switch is a
    different claim: it says the feature does not work for anyone, so it outranks that escape and
    every plan class.

    One level deep, matching the seed's flat capability -> module shape, and cheap: `switch_for`
    reads a process-local dict behind a 30s TTL, so this adds no query on the metering hot path.

    Never raises. A switch is a restriction, so failing to read one applies none.
    """
    try:
        for dep in depends_on:
            if dep == capability_id:
                continue
            sw = await switch_for(dep)
            if sw.blocks:
                return sw
    except Exception:
        logger.warning("dependency switch lookup failed for %s", capability_id, exc_info=True)
    return None


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

    for dep in ent.depends_on:
        if dep == ent.capability_id:
            continue
        resolved = await resolve_entitlement(ts, dep, _depth=depth + 1)
        # An unknown dependency resolves to "shadow" and is deliberately not a block: unknown
        # always means allow, or cataloging a capability late could break a shipped feature.
        if resolved.mode != "disabled":
            continue

        if resolved.source == "feature_switch":
            # A PLATFORM SWITCH BEATS THE PLAN ESCAPE BELOW. Those are different kinds of claim:
            # the plan escape says "this customer bought this specific action, so a blanket module
            # gate should not take it away", which is a statement about entitlement. A switch says
            # "this does not work right now, for anyone". Letting a plan out of it would mean
            # taking Outreach offline still left Free's 20 email drafts running against the broken
            # subsystem — the exact case the escape was written for, now pointed the wrong way.
            ent.mode = "disabled"
            ent.source = "feature_switch"
            # Carry the state and the wording down to the dependent, or an endpoint behind a
            # switched-off module 402s with a bare "not included" and the customer is told to
            # upgrade to fix our maintenance window.
            ent.switch_state = resolved.switch_state
            ent.switch_message = resolved.switch_message
            return ent

        if ent.source == "plan":
            # The plan named this capability explicitly, with its own mode and quota. That is a
            # deliberate commercial decision and outranks a blanket module gate — Free disables
            # module.outreach yet still sells 20 ai.email_drafts, and it means the 20. Module gates
            # are the default for capabilities a plan does NOT mention.
            return ent

        ent.mode = "disabled"
        ent.source = "dependency"
        return ent
    return ent


async def _is_gauge(ts: TenantSession, capability_id: str) -> bool:
    """Does this capability measure a LEVEL rather than an action?

    Gauges (`seat.member`, `platform.storage`, `network.persons`) resolve to a live count — members
    held, GB stored — so there is no "request" to price. Summing events for one would only ever
    climb, which is why a customer could never get back under a seat limit before this distinction
    existed. They keep hard caps and stay outside the credit system.

    Defaults to False on any failure: treating an action as a gauge would make it free, and a
    capability silently costing nothing is the failure mode this whole change exists to remove.
    """
    try:
        cap = await ts.session.get(BillingCapability, capability_id)
        return bool(cap is not None and cap.meter_kind == "gauge")
    except Exception:
        return False


async def _burn_for_usage(
    ts: TenantSession, ent: "ResolvedEntitlement", quantity: float, key: str
) -> bool | None:
    """Charge the rate card for a whole request. ``None`` when there was nothing to charge.

    The three outcomes are distinct and the caller depends on it:
      * ``True``  — credits were spent (or an identical charge already existed on a retry);
      * ``False`` — there is a price and the balance cannot cover it, so the call must stop;
      * ``None``  — no live rate card. Nothing to charge means nothing to run out of, and an
        unpriced capability must not become a block.

    Idempotency-keyed on the decision, so a retried request re-derives the same charge and pays
    once. Never raises: a burn failure degrades to "nothing charged" and the caller decides.
    """
    from nexus.billing.credits import burn_credits
    from nexus.models.billing import BillingCreditLedger, BillingRateCard

    burn_key = f"{key}:burn"
    try:
        already = await ts.first(
            BillingCreditLedger, BillingCreditLedger.idempotency_key == burn_key
        )
        if already is not None:
            return True

        card = await ts.session.get(BillingRateCard, ent.capability_id)
        if card is None or not card.active:
            return None

        if card.tiers:
            # A volume ladder prices a unit by how many came before it. `rating.rate_period` prices
            # the whole period in one `tiered_credits` call at close, so an in-flight burn has to
            # charge the MARGINAL cost of these units at their real position — reading
            # `credits_per_unit` flat overcharges the moment a ladder exists.
            from nexus.billing.rating import tiered_credits

            prior = await current_usage(ts, ent.capability_id)
            amount = tiered_credits(prior + quantity, card) - tiered_credits(prior, card)
        else:
            amount = quantity * float(card.credits_per_unit or 0)

        if amount <= 0:
            return None

        # `capability_id` is what makes the spend attributable. Without it every burn lands in the
        # ledger as an anonymous deduction and "where did my 2,000 credits go?" has no answer —
        # which is the whole point of charging per capability rather than per period.
        # `period_key` as well as `capability_id`. The grant carries one, so a burn without one
        # cannot be reconciled against it — the usage report would show credits granted this period
        # and no spend, which is precisely the "where did they go?" question it exists to answer.
        from nexus.billing.rollups import period_key as _period_key
        from nexus.core.db import utcnow

        return await burn_credits(
            ts, amount, reason=f"{ent.capability_id} usage", idempotency_key=burn_key,
            capability_id=ent.capability_id, period_key=_period_key(utcnow(), "period"),
        )
    except Exception:  # a charge must not break the call it is charging for
        logger.warning("usage burn failed for %s", ent.capability_id, exc_info=True)
        return None


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
            if ent.source == "feature_switch":
                blocked_reason = "feature_switch"
            elif ent.source == "dependency":
                blocked_reason = "dependency"
            else:
                blocked_reason = "disabled"
        elif ent.mode in ("shadow", "unlimited"):
            blocked_reason = None            # observe-only, or a plan class that exists not to gate
        elif await _is_gauge(ts, capability_id):
            # GAUGES KEEP HARD CAPS AND ARE NEVER CHARGED.
            #
            # `seat.member`, `platform.storage` and `network.persons` resolve to a live count —
            # members held, GB stored — not to an action somebody performed. Charging them per
            # request is meaningless (the same seat would be billed on every call that reads it),
            # and running them on credits would silently lock people out of a workspace they are
            # paying for the moment the balance ran dry. So they stay outside the credit system and
            # keep the plan limit they have always had.
            if ent.quota is not None:
                await _lock_capability(ts, capability_id)
                used = await current_usage(ts, capability_id)
                limit = ent.hard_limit if ent.hard_limit is not None else ent.quota
                if used + quantity > limit:
                    blocked_reason = "quota_exhausted"
        else:
            # CREDITS ONLY. One price per request, taken from the rate card, paid in credits.
            #
            # Two prices used to exist for the same action — the rate card and
            # `overage_price_credits` — and they disagreed on 11 plan/capability pairs in BOTH
            # directions: `verify.email` cost 0.25 in plan and 1.00 past the allowance (4x more for
            # crossing a line the customer cannot see), while `enrich.contact` on `core` cost 4.0
            # in plan and 2.00 past it, so overflowing your own quota was the rational move.
            #
            # Worse, credits were burned ONLY for the portion beyond the quota, so an in-plan
            # request cost nothing and the balance a customer was sold barely moved. "You have
            # 2,000 credits" was not the truth about anything.
            #
            # Now every metered request burns `credits_per_unit x quantity` whatever side of any
            # line it falls on, and a balance that cannot cover it stops the call. An unpriced
            # capability charges nothing and is allowed — no rate card means nothing to run out of.
            if ent.plan_id is None:
                # NO SUBSCRIPTION -> allow, and charge nothing. The engine's documented bias, and
                # the safety net under `start_subscription`, which never raises: a workspace whose
                # plan attach failed has no balance to spend, so charging it would block every
                # request and leave the customer with an account that does nothing. Same direction
                # as unknown-capability and resolve-failure — unknown means allow.
                blocked_reason = None
            else:
                covered = await _burn_for_usage(ts, ent, float(quantity), key)
                if covered is False:
                    blocked_reason = "credits_exhausted"
                elif covered is None and ent.quota is not None:
                    # NO RATE CARD, BUT A QUOTA. Fall back to enforcing the quota.
                    #
                    # Without this a capability nobody has priced becomes completely ungated:
                    # `_burn_for_usage` returns None (nothing to charge), so credits cannot limit
                    # it, and the quota branch no longer runs. The old model at least held the line
                    # at the quota. Found by two existing tests that seed plans WITHOUT rate cards
                    # and expect 999 email drafts against a quota of 20 to be refused — they were
                    # sailing through, which is exactly how an unpriced capability shipped free
                    # once before (`ai.scoring`, 4,090 runs).
                    #
                    # So: priced capabilities are limited by credits, unpriced ones by their quota,
                    # and something is always holding the line.
                    await _lock_capability(ts, capability_id)
                    used = await current_usage(ts, capability_id)
                    limit = ent.hard_limit if ent.hard_limit is not None else ent.quota
                    if used + quantity > limit:
                        blocked_reason = "quota_exhausted"

        # Burst is a separate axis from quota: a tenant well inside its monthly allowance can
        # still hammer an endpoint. Only queried for capabilities that set a limit, so it costs
        # nothing on the rest.
        if blocked_reason is None and ent.burst_limit is not None:
            if await _over_burst(ts, capability_id, ent.burst_limit):
                blocked_reason = "throttled"

        # A PLATFORM SWITCH IS ENFORCED WHATEVER THE BILLING MODE — the `off` kill switch above is
        # the single exception, and it returned long before this point.
        #
        # `shadow` is a statement about BILLING rollout: "we are not yet refusing anyone over
        # money." A feature switch is not about money. "Calling is broken, take it offline" and "we
        # have not started enforcing quotas" are unrelated decisions, and production runs `shadow`
        # by default — so riding on it would have made the whole control inert exactly where it
        # matters. The superadmin flips it, the panel reports disabled, every customer keeps using
        # the feature: the "configured and doing nothing" failure this codebase keeps diagnosing.
        #
        # `off` still wins because it is documented as a FULL kill switch for the engine. If this
        # engine misbehaves in production, "turn it all off" has to be a complete answer rather
        # than one that strands some blocks for an operator to hunt down under load.
        enforced = mode == "on" or blocked_reason == "feature_switch"
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
