"""Call Script Agent — an AI cold-call talk track grounded in the account's signals + ICP.

Sibling of the Messaging Agent, but for the phone: it returns a structured script (opener, hook,
value prop, discovery questions, objection handling, CTA, voicemail) the SDR reads live. Uses the
same LLM chain (with Groq key rotation + stub fallback), so it works offline in CI.
"""
from __future__ import annotations

import json

from nexus.agents.llm import LLMMessage
from nexus.agents.runtime import AgentContext, BaseAgent, register_agent

_KEYS = ("opener", "hook", "value_prop", "discovery_questions", "objections", "cta", "voicemail")


def _coerce(raw: str, *, account: str, contact: str) -> dict:
    """Parse the model's JSON; on any failure degrade to a minimal usable script (never raise)."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {
                "opener": str(data.get("opener", "")),
                "hook": str(data.get("hook", "")),
                "value_prop": str(data.get("value_prop", "")),
                "discovery_questions": list(data.get("discovery_questions", []) or []),
                "objections": list(data.get("objections", []) or []),
                "cta": str(data.get("cta", "")),
                "voicemail": str(data.get("voicemail", "")),
            }
    except (ValueError, TypeError):
        pass
    return {
        "opener": f"Hi {contact}, this is <your name> from <your company> — do you have 30 seconds?",
        "hook": raw.strip()[:280],
        "value_prop": "",
        "discovery_questions": [],
        "objections": [],
        "cta": "Would you be open to a 15-minute call this week?",
        "voicemail": f"Hi {contact}, calling about {account}; I'll follow up by email.",
    }


class CallScriptAgent(BaseAgent):
    name = "call_script"

    async def run(self, ctx: AgentContext) -> dict:
        if ctx.account is None:
            return {"error": "call_script requires an account"}

        contact = None
        cid = ctx.inputs.get("contact_id")
        if cid:
            contact = next((c for c in ctx.contacts if c.id == cid), None)
        contact = contact or (ctx.contacts[0] if ctx.contacts else None)
        contact_name = contact.full_name if contact else "there"

        trigger_signal = max(ctx.signals, key=lambda s: s.strength, default=None)
        trigger = trigger_signal.title if trigger_signal else "your current priorities"

        vps = ctx.relevance_context.value_props
        vp = vps[0] if vps else {"name": "our platform", "pains_solved": []}
        pains = ", ".join(vp.get("pains_solved", [])) or "hit pipeline goals"

        # Person-level personalization: shape the talk track to this individual (role/seniority,
        # a signal tied to them when available, social insights), not just the account.
        from nexus.core.config import get_settings
        from nexus.personalization.brief import build_person_brief

        brief = build_person_brief(contact, ctx.account, ctx.signals) if contact else None
        hook = brief.signal_title if (brief and brief.signal_title) else trigger
        person_block = (
            " " + brief.to_prompt(max_posts=get_settings().personalization_max_posts)
            if brief is not None else ""
        )
        content = (
            f"Write a cold-call talk track for an SDR calling "
            f"{contact_name} ({contact.title if contact and contact.title else 'a buyer'}) "
            f"at {ctx.account.name}. Hook: {hook}. Lead value prop: '{vp.get('name')}'.{person_block} "
            "Return ONLY a JSON object with keys: opener, hook, value_prop, "
            "discovery_questions (array of strings), objections (array of {objection, response}), "
            "cta, voicemail. Keep it concise and natural to say out loud."
        )
        raw = await ctx.complete(
            [ctx.system_message(), LLMMessage(role="user", content=content)],
            purpose="call_script",
            variables={
                "account": ctx.account.name,
                "contact": contact_name,
                "title": contact.title if contact and contact.title else "",
                "value_prop": vp.get("name", "our platform"),
                "trigger": trigger,
                "pain": pains,
            },
        )
        script = _coerce(raw, account=ctx.account.name, contact=contact_name)
        return {
            "contact_id": contact.id if contact else None,
            "script": script,
            "trigger_signal_id": trigger_signal.id if trigger_signal else None,
        }


register_agent(CallScriptAgent())
