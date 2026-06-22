"""Messaging Agent — personalized outreach grounded in value props and the triggering signal."""
from __future__ import annotations

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
        pains = ", ".join(vp.get("pains_solved", [])) or "hit pipeline goals"

        # Person-level personalization: write to the individual (role/seniority, a signal tied to
        # them when available, and any social insights), not just the account.
        from nexus.core.config import get_settings
        from nexus.personalization.brief import build_person_brief

        brief = build_person_brief(contact, ctx.account, ctx.signals) if contact else None
        hook = brief.signal_title if (brief and brief.signal_title) else trigger

        angle = (ctx.inputs.get("angle") or "").strip()
        content = (
            f"Write a short, personalized cold email to "
            f"{contact.full_name if contact else 'the buyer'} at {ctx.account.name}. "
            f"Hook: {hook}. Lead with value prop '{vp.get('name')}'."
        )
        if brief is not None:
            content += " " + brief.to_prompt(max_posts=get_settings().personalization_max_posts)
        if angle:
            # Per-touch cadence angle: shape this specific touch (e.g. a follow-up nudge,
            # a case-study share) so successive touches don't repeat the same message.
            content += f" Angle for this touch: {angle}."
        guidance = (ctx.inputs.get("guidance") or "").strip()
        if guidance:
            # Reviewer redraft instructions from the approval gate take precedence — they are
            # an explicit human steer on this exact message ("make it shorter", "mention SOC2").
            content += f" Revise per the reviewer's instructions: {guidance}."
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
