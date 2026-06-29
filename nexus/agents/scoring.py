"""Scoring Agent — deterministic ICP-fit / intent / health, with an LLM-written rationale."""
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

        user = LLMMessage(
            role="user",
            content=(
                f"Explain in 2 sentences why {ctx.account.name} scores "
                f"ICP {icp_fit}, intent {intent}, health {health} (composite {composite})."
            ),
        )
        rationale = await ctx.complete(
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

        return {
            "score_id": score.id,
            "icp_fit": icp_fit,
            "intent": intent,
            "health": health,
            "composite": composite,
            "rationale": rationale,
        }


register_agent(ScoringAgent())
