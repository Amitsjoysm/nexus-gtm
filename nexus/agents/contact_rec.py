"""Contact Recommendation Agent — ranks contacts by fit to the tenant's buying committee."""
from __future__ import annotations

from nexus.agents.llm import LLMMessage
from nexus.agents.runtime import AgentContext, BaseAgent, register_agent

SENIORITY_SCORE = {
    "c_level": 1.0, "vp": 0.85, "director": 0.7, "manager": 0.5, "ic": 0.3,
}


def _infer_seniority(contact) -> str:
    if contact.seniority:
        return contact.seniority
    title = (contact.title or "").lower()
    if any(k in title for k in ("ceo", "cfo", "cto", "cmo", "chief", "founder")):
        return "c_level"
    if "vp" in title or "vice president" in title:
        return "vp"
    if "director" in title or "head of" in title:
        return "director"
    if "manager" in title or "lead" in title:
        return "manager"
    return "ic"


class ContactRecAgent(BaseAgent):
    name = "contact_rec"

    async def run(self, ctx: AgentContext) -> dict:
        if ctx.account is None:
            return {"error": "contact_rec requires an account"}
        if not ctx.contacts:
            return {"recommendations": [], "note": "no contacts on account; enrich first"}

        # buyer_titles let a tenant bias toward their economic buyer / champion personas.
        buyer_titles = ctx.inputs.get("buyer_titles") or []

        ranked = []
        for c in ctx.contacts:
            seniority = _infer_seniority(c)
            score = SENIORITY_SCORE.get(seniority, 0.3)
            title = (c.title or "").lower()
            if buyer_titles and any(bt.lower() in title for bt in buyer_titles):
                score = min(1.0, score + 0.2)
            if c.email:  # reachable contacts rank higher
                score = min(1.0, score + 0.05)
            ranked.append((score, seniority, c))

        ranked.sort(key=lambda t: t[0], reverse=True)
        top = ranked[: ctx.inputs.get("limit", 3)]

        recommendations = []
        for score, seniority, c in top:
            why = await ctx.complete(
                [
                    ctx.system_message(),
                    LLMMessage(
                        role="user",
                        content=f"Why is {c.full_name} ({c.title}) a good first contact at "
                        f"{ctx.account.name}?",
                    ),
                ],
                purpose="contact_rationale",
                variables={
                    "account": ctx.account.name,
                    "contact": c.full_name,
                    "title": c.title or "n/a",
                },
            )
            recommendations.append(
                {
                    "contact_id": c.id,
                    "name": c.full_name,
                    "title": c.title,
                    "seniority": seniority,
                    "score": round(score, 2),
                    "reachable": bool(c.email),
                    "why": why,
                }
            )
        return {"recommendations": recommendations}


register_agent(ContactRecAgent())
