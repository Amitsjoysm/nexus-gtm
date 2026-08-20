"""Messaging Agent — personalized outreach grounded in value props and the triggering signal."""
from __future__ import annotations

from nexus.agents.copy import EMAIL_RULES, OUTPUT_CONTRACT, format_pains
from nexus.agents.llm import LLMMessage
from nexus.agents.runtime import AgentContext, BaseAgent, register_agent


def _split_subject(text: str) -> tuple[str, str]:
    if text.lower().startswith("subject:"):
        head, _, rest = text.partition("\n")
        return head.split(":", 1)[1].strip(), rest.strip()
    return "", text.strip()


class MessagingAgent(BaseAgent):
    name = "messaging"

    async def run(self, ctx: AgentContext) -> dict:
        if ctx.account is None:
            return {"error": "messaging requires an account"}

        # Choose a contact (explicit > first available).
        contact = None
        cid = ctx.inputs.get("contact_id")
        if cid:
            contact = next((c for c in ctx.contacts if c.id == cid), None)
        contact = contact or (ctx.contacts[0] if ctx.contacts else None)

        # Choose the strongest recent signal as the hook.
        trigger_signal = max(ctx.signals, key=lambda s: s.strength, default=None)
        trigger = trigger_signal.title if trigger_signal else "your current priorities"

        vps = ctx.relevance_context.value_props
        vp = vps[0] if vps else {"name": "our platform", "pains_solved": []}
        # Nouns, formatted to sit inside a sentence — see nexus/agents/copy.py for the
        # garbled email this replaced.
        pains = format_pains(vp.get("pains_solved", []))

        # Person-level personalization: write to the individual (role/seniority, a signal tied to
        # them when available, and any social insights), not just the account.
        from nexus.core.config import get_settings
        from nexus.personalization.brief import build_person_brief

        brief = build_person_brief(contact, ctx.account, ctx.signals) if contact else None
        hook = brief.signal_title if (brief and brief.signal_title) else trigger

        angle = (ctx.inputs.get("angle") or "").strip()
        # Facts first, then the task, then the rules, then the output contract. The facts have to
        # precede the rules or "use only facts given above" refers to nothing.
        who = contact.full_name if contact else "the buyer"
        role = (contact.title or "").strip() if contact else ""
        content = (
            f"Write a cold email from an experienced SDR to {who}"
            f"{f', {role},' if role else ''} at {ctx.account.name}.\n"
            f"The trigger to open on: {hook}\n"
            f"The value proposition to land: {vp.get('name')}\n"
            f"The problems it removes: {pains}\n"
        )
        if brief is not None:
            content += brief.to_prompt(max_posts=get_settings().personalization_max_posts) + "\n"
        if angle:
            # Per-touch cadence angle: shape this specific touch (e.g. a follow-up nudge,
            # a case-study share) so successive touches don't repeat the same message.
            content += f"Angle for this specific touch: {angle}\n"
        guidance = (ctx.inputs.get("guidance") or "").strip()
        if guidance:
            # Reviewer redraft instructions from the approval gate take precedence — they are
            # an explicit human steer on this exact message ("make it shorter", "mention SOC2").
            content += f"Revise per the reviewer's instructions: {guidance}\n"
        # Rules last so they are the final thing before generation, and the reviewer's steer above
        # is never buried under them.
        content += f"\n{EMAIL_RULES}\n{OUTPUT_CONTRACT}"
        user = LLMMessage(role="user", content=content)
        message = await ctx.complete(
            [ctx.system_message(), user],
            purpose="outreach_message",
            variables={
                "account": ctx.account.name,
                "contact": contact.full_name if contact else "there",
                "value_prop": vp.get("name", "our platform"),
                "trigger": trigger,
                "pain": pains,
            },
        )
        subject, body = _split_subject(message)
        return {
            "contact_id": contact.id if contact else None,
            "subject": subject,
            "body": body,
            "message": message,
            "trigger_signal_id": trigger_signal.id if trigger_signal else None,
        }


register_agent(MessagingAgent())
