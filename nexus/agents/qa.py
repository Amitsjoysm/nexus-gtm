"""Account Q&A Agent — answer any question about an account using assembled context.

Grounding is two-layer:
  1. what the workspace already knows — firmographics, tech stack, description, signals, contacts;
  2. LIVE web research at ask time — the research provider first (budgeted/cached/circuit-broken
     via the registry, same path the Research agent uses), then raw web-search snippets biased
     toward the question itself.

Both live layers are strictly best-effort: offline (stub providers) the agent still answers from
stored facts, so tests stay deterministic and a provider outage can never break the answer.
"""
from __future__ import annotations

import logging

from nexus.agents.llm import LLMMessage
from nexus.agents.runtime import AgentContext, BaseAgent, register_agent

logger = logging.getLogger("nexus.agents.qa")

_MAX_LIVE_FACTS = 8


class QAAgent(BaseAgent):
    name = "qa"

    async def run(self, ctx: AgentContext) -> dict:
        question = (ctx.inputs.get("question") or "").strip()
        if not question:
            return {"error": "qa requires a 'question'"}

        account_name = ctx.account.name if ctx.account else "the account"
        # Layer 1 — grounding facts the workspace already holds.
        facts = []
        if ctx.account:
            a = ctx.account
            facts.append(
                f"{account_name}: industry={a.industry}, "
                f"employees={a.employee_count}, country={a.country}"
            )
            if a.tech_stack:
                facts.append("tech stack: " + ", ".join(str(t) for t in a.tech_stack[:10]))
            desc = (a.custom_fields or {}).get("description")
            if desc:
                facts.append(f"description: {str(desc)[:300]}")
        for s in ctx.signals[:8]:
            facts.append(f"signal[{s.kind}] {s.title}")
        for c in ctx.contacts[:8]:
            facts.append(f"contact: {c.full_name} — {c.title}")

        # Layer 2 — LIVE research at ask time (best-effort; the stubs are no-ops offline).
        sources: list[dict] = []
        if ctx.account is not None:
            live: list[str] = []
            try:
                if ctx.registry is not None and hasattr(ctx.registry, "research"):
                    profile = await ctx.registry.research(
                        company=ctx.account.name, domain=ctx.account.domain
                    )
                    if profile is not None and getattr(profile, "found", False):
                        for h in profile.highlights:
                            if h.strip():
                                live.append(f"web: {h.strip()}")
                        for url in profile.sources[:5]:
                            if url:
                                sources.append({"title": "", "url": url})
                # Question-biased search so the answer can cite something the stored profile
                # doesn't cover (e.g. "did they just open an office in Berlin?").
                if ctx.browser is not None and hasattr(ctx.browser, "search"):
                    hits = await ctx.browser.search(
                        f"{ctx.account.name} {question}", limit=4
                    ) or []
                    for h in hits:
                        snippet = (h.get("snippet") or h.get("title") or "").strip()
                        if snippet:
                            live.append(f"web: {snippet[:240]}")
                        if h.get("url"):
                            sources.append({"title": h.get("title", ""), "url": h["url"]})
            except Exception:  # a live-layer outage must never break the answer
                logger.warning("qa live research failed for %s", account_name, exc_info=True)
            facts.extend(live[:_MAX_LIVE_FACTS])

        facts_block = "\n".join(f"- {f}" for f in facts) or "- (no facts available)"

        user = LLMMessage(
            role="user",
            content=(
                f"Question: {question}\nKnown facts:\n{facts_block}\n"
                "Answer concisely using only the facts above; say plainly when something "
                "is not known rather than guessing."
            ),
        )
        answer = await ctx.complete(
            [ctx.system_message(), user],
            purpose="account_qa",
            variables={"account": account_name},
        )
        return {
            "question": question,
            "answer": answer,
            "grounded_on": len(facts),
            "sources": sources,
        }


register_agent(QAAgent())
