"""Account Q&A Agent — answer any question about an account using assembled context."""
from __future__ import annotations

from nexus.agents.llm import LLMMessage
from nexus.agents.runtime import AgentContext, BaseAgent, register_agent


class QAAgent(BaseAgent):
    name = "qa"

    async def run(self, ctx: AgentContext) -> dict:
        question = (ctx.inputs.get("question") or "").strip()
        if not question:
            return {"error": "qa requires a 'question'"}

        account_name = ctx.account.name if ctx.account else "the account"
        # Assemble grounding facts the model may cite.
        facts = []
        if ctx.account:
            facts.append(
                f"{account_name}: industry={ctx.account.industry}, "
                f"employees={ctx.account.employee_count}, country={ctx.account.country}"
            )
        for s in ctx.signals[:8]:
            facts.append(f"signal[{s.kind}] {s.title}")
        for c in ctx.contacts[:8]:
            facts.append(f"contact: {c.full_name} — {c.title}")
        facts_block = "\n".join(f"- {f}" for f in facts) or "- (no facts available)"

        user = LLMMessage(
            role="user",
            content=f"Question: {question}\nKnown facts:\n{facts_block}\nAnswer concisely.",
        )
        answer = await ctx.complete(
            [ctx.system_message(), user],
            purpose="account_qa",
            variables={"account": account_name},
        )
        return {"question": question, "answer": answer, "grounded_on": len(facts)}


register_agent(QAAgent())
