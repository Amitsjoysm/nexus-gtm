"""Research Agent — gathers account/person facts via the browser provider, then summarizes."""
from __future__ import annotations

from nexus.agents.llm import LLMMessage
from nexus.agents.runtime import AgentContext, BaseAgent, register_agent


class ResearchAgent(BaseAgent):
    name = "research"

    async def run(self, ctx: AgentContext) -> dict:
        if ctx.account is None:
            return {"error": "research requires an account"}

        account = ctx.account
        query = ctx.inputs.get("query") or f"{account.name} company funding hiring news"
        facts: list[str] = []
        sources: list[dict] = []

        # Primary grounding: the research provider (CloakBrowser+Scrapegraph in prod, the
        # deterministic stub offline). Going through the registry gives us budgets, circuit
        # breaking and caching, and keeps the agent source-agnostic.
        if ctx.registry is not None and hasattr(ctx.registry, "research"):
            profile = await ctx.registry.research(company=account.name, domain=account.domain)
            if profile is not None and getattr(profile, "found", False):
                facts.extend(h for h in profile.highlights if h.strip())
                for url in profile.sources:
                    if url:
                        sources.append({"title": "", "url": url})

        # Secondary breadth: raw web-search snippets (when a browser is wired in).
        if ctx.browser is not None and hasattr(ctx.browser, "search"):
            hits = await ctx.browser.search(query, limit=ctx.inputs.get("limit", 5))
            for h in hits:
                snippet = (h.get("snippet") or h.get("title") or "").strip()
                if snippet:
                    facts.append(snippet)
                if h.get("url"):
                    sources.append({"title": h.get("title", ""), "url": h["url"]})

        user = LLMMessage(
            role="user",
            content=(
                f"Write a concise research brief for {account.name} "
                f"(domain: {account.domain or 'unknown'}). Facts:\n"
                + ("\n".join(f"- {f}" for f in facts) or "(no external facts)")
            ),
        )
        brief = await ctx.complete(
            [ctx.system_message(), user],
            purpose="research_brief",
            variables={"account": account.name, "facts": facts},
        )
        return {"brief": brief, "facts": facts, "sources": sources}


register_agent(ResearchAgent())
