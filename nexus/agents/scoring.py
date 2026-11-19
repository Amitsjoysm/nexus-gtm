"""Scoring Agent — deterministic ICP-fit / intent / health, with an LLM-written rationale.

The score is deterministic; only the two-sentence rationale costs a model call, and the call
console's brief is the one screen that shows it. It ran on every scheduled refresh of every
account: measured on the local stack 2026-11-19, 1,000-1,500 calls a day, 75% of them for a score
identical to the account's previous one, sent to Claude first wherever an Anthropic key existed.
Owner decision, the same day:

* **An unchanged score keeps its rationale** (`_previous`): no call when ICP fit, intent, health and
  the composite are what they were.
* **The rationale is asked of Groq only** (`llm.GROQ_ONLY_PURPOSES`).
* **On the Free plan it is written when it is first read** (`ensure_rationale`, called by the call
  brief), never on a refresh: a free workspace's accounts are refreshed whether or not anyone opens
  them.
"""
from __future__ import annotations


from nexus.agents.llm import LLMMessage
from nexus.agents.runtime import AgentContext, BaseAgent, register_agent
from nexus.core.db import ensure_aware, utcnow
from nexus.models.intelligence import AccountScore

# Relative pull of each signal kind on the *intent* score.
INTENT_WEIGHTS = {
    "g2_intent": 1.0,
    "web_visit": 0.9,
    "job_posting": 0.6,
    "funding": 0.7,
    "news": 0.4,
    "tech_install": 0.6,
    "hiring": 0.5,
    "job_switch": 0.8,
}
HEALTH_KINDS = {"product_usage", "call"}


def _recency_factor(occurred_at, now) -> float:
    """1.0 today, decaying to ~0 over 90 days."""
    if occurred_at is None:
        return 0.5
    # SQLite (and any non-tz column) hands back naive datetimes, while utcnow() is
    # tz-aware. Normalize so the subtraction never raises.
    occurred_at = ensure_aware(occurred_at)
    age_days = max((now - occurred_at).total_seconds() / 86400.0, 0.0)
    return max(0.0, 1.0 - age_days / 90.0)


class ScoringAgent(BaseAgent):
    name = "scoring"

    async def run(self, ctx: AgentContext) -> dict:
        if ctx.account is None:
            return {"error": "scoring requires an account"}

        now = utcnow()
        fit = ctx.relevance_context.account_fit
        icp_fit = fit.score if fit else 50

        # Intent from recent third-/first-party signals.
        intent_raw = 0.0
        for s in ctx.signals:
            w = INTENT_WEIGHTS.get(s.kind, 0.3)
            intent_raw += s.strength * w * _recency_factor(s.occurred_at, now)
        intent = min(100, round(intent_raw * 25))

        # Health from engagement signals; neutral if none.
        health_signals = [s for s in ctx.signals if s.kind in HEALTH_KINDS]
        if health_signals:
            health = min(
                100,
                round(
                    sum(s.strength * _recency_factor(s.occurred_at, now) for s in health_signals)
                    / len(health_signals)
                    * 100
                ),
            )
        else:
            health = 50

        composite = round(icp_fit * 0.4 + intent * 0.4 + health * 0.2)
        drivers = "; ".join((fit.reasons if fit else [])[:3])
        numbers = {"icp_fit": icp_fit, "intent": intent, "health": health, "composite": composite}

        previous = await _previous(ctx.ts, ctx.account.id)
        if previous is not None and _same(previous, numbers) and (previous.rationale or "").strip():
            rationale = previous.rationale               # nothing moved: nothing to re-explain
        elif await on_free_plan(ctx.ts):
            rationale = ""                               # written when someone opens the brief
        else:
            rationale = await _write(ctx, drivers=drivers, **numbers)

        score = AccountScore(
            tenant_id=ctx.ts.tenant_id,
            account_id=ctx.account.id,
            icp_fit=icp_fit,
            intent=intent,
            health=health,
            composite=composite,
            rationale=rationale,
        )
        ctx.ts.add(score)
        await ctx.ts.flush()
        # And against every other service the workspace sells (W8), on the same refresh.
        from nexus.relevance.services import score_services

        await score_services(ctx.ts, [ctx.account])
        from nexus.engagement.ledger.emit import emit

        await emit(ctx.ts, "account.scored",
                   refs={"account_id": ctx.account.id, "score_id": score.id,
                         "company_id": ctx.account.company_id},
                   payload={"icp_fit": icp_fit, "intent": intent, "health": health,
                            "composite": composite, "rationale": rationale})

        return {
            "score_id": score.id,
            "icp_fit": icp_fit,
            "intent": intent,
            "health": health,
            "composite": composite,
            "rationale": rationale,
        }


def _same(score: AccountScore, numbers: dict) -> bool:
    return all(getattr(score, key) == value for key, value in numbers.items())


async def _previous(ts, account_id: str) -> AccountScore | None:
    """The account's newest score, before this one is added."""
    return (await ts.session.scalars(
        ts.select(AccountScore, AccountScore.account_id == account_id)
        .order_by(AccountScore.computed_at.desc()).limit(1))).first()


async def on_free_plan(ts) -> bool:
    """Whether the workspace is on the Free plan. Read once per session; a failure reads as not
    free, which keeps the pre-existing behaviour (a rationale is written)."""
    cache_key = "nexus_scoring_free_plan"
    cached = ts.session.info.get(cache_key)
    if cached is not None:
        return cached
    free = False
    try:
        from nexus.models.billing import BillingPlan, BillingSubscription

        sub = await ts.first(BillingSubscription)
        if sub is not None and sub.plan_id:
            plan = await ts.session.get(BillingPlan, sub.plan_id)
            free = plan is not None and plan.plan_class == "free"
    except Exception:  # a billing read must never break scoring
        free = False
    ts.session.info[cache_key] = free
    return free


async def _write(ctx: AgentContext, *, icp_fit: int, intent: int, health: int, composite: int,
                 drivers: str) -> str:
    user = LLMMessage(
        role="user",
        content=(
            f"Explain in 2 sentences why {ctx.account.name} scores "
            f"ICP {icp_fit}, intent {intent}, health {health} (composite {composite})."
        ),
    )
    return await ctx.complete(
        [ctx.system_message(), user],
        purpose="scoring_rationale",
        variables={
            "account": ctx.account.name,
            "icp_fit": icp_fit,
            "intent": intent,
            "health": health,
            "composite": composite,
            "drivers": drivers,
        },
    )


async def ensure_rationale(ts, score: AccountScore | None) -> str:
    """The score's rationale, written now if it has none (a Free workspace's scores, or one
    whose rationale call failed). Stored on the score row, so it is written once. Never raises:
    a failed call costs the rationale, not the screen that wanted it."""
    if score is None:
        return ""
    if (score.rationale or "").strip():
        return score.rationale
    try:
        from nexus.agents.runtime import get_agent_runtime

        ctx = await get_agent_runtime()._load_context(ts, score.account_id, {})
        if ctx.account is None:
            return ""
        fit = ctx.relevance_context.account_fit
        text = await _write(ctx, icp_fit=score.icp_fit, intent=score.intent, health=score.health,
                            composite=score.composite,
                            drivers="; ".join((fit.reasons if fit else [])[:3]))
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("nexus.agents.scoring").warning(
            "could not write a score rationale for %s", score.account_id, exc_info=True)
        return ""
    score.rationale = text
    await ts.flush()
    return text


register_agent(ScoringAgent())
