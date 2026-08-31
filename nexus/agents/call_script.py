"""Call Script Agent — an AI cold-call talk track grounded in the account's signals + ICP.

Sibling of the Messaging Agent, but for the phone: it returns a structured script (opener, hook,
value prop, discovery questions, objection handling, CTA, voicemail) the SDR reads live. Uses the
same LLM chain (with Groq key rotation + stub fallback), so it works offline in CI.
"""
from __future__ import annotations

import json
import re

from nexus.agents.copy import CALL_RULES, first_pain, format_pains
from nexus.agents.llm import LLMMessage
from nexus.agents.runtime import AgentContext, BaseAgent, register_agent

_KEYS = ("opener", "hook", "value_prop", "discovery_questions", "objections", "cta", "voicemail")


# The first {...} in the response. Models add a lead-in sentence and a ``` fence however firmly
# the prompt forbids them, and throwing away an otherwise-complete script over its wrapper costs
# the rep the whole call.
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _coerce(raw: str, *, account: str, contact: str) -> dict:
    """Parse the model's JSON; on any failure degrade to a minimal usable script (never raise)."""
    match = _JSON_OBJ_RE.search(raw or "")
    try:
        data = json.loads(match.group(0) if match else raw)
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
        # Deliberately NOT the raw response. This slot is read aloud, and a truncated JSON
        # document in front of a rep mid-dial is worse than a plain sentence: it cannot be
        # spoken, and it looks like the product is broken to the one person holding the phone.
        "hook": f"I wanted to reach out about what's happening at {account}.",
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

        # Same guard as the messaging agent, and it matters MORE here: a call script is SPOKEN,
        # so a rep reads placeholder framing out loud to a person who can hear the hesitation.
        # See RelevanceContext.is_configured for the production failure this prevents.
        if not ctx.relevance_context.is_configured:
            return {
                "error": "relevance_profile_not_configured",
                "detail": (
                    "This workspace has no product context or value propositions, so there is "
                    "nothing to pitch. Add them on the Relevance page — paste your website to "
                    "draft them automatically — then try again."
                ),
            }

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
        # Nouns, formatted to sit inside a sentence — see nexus/agents/copy.py for the
        # garbled email this replaced.
        pains = format_pains(vp.get("pains_solved", []))

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
        # Facts, then task, then rules, then the output contract — the same order as the email
        # agent, so "use only facts given above" has something above it to refer to.
        content = (
            f"Write a cold-call talk track for an experienced SDR calling {contact_name} "
            f"({contact.title if contact and contact.title else 'a buyer'}) at {ctx.account.name}.\n"
            f"The trigger to open on: {hook}\n"
            f"The value proposition to land: {vp.get('name')}\n"
            f"The problems it removes: {pains}\n"
            f"{person_block}\n"
            f"{CALL_RULES}\n"
            "Return ONLY a JSON object with keys: opener, hook, value_prop, "
            "discovery_questions (array of strings), objections (array of {objection, response}), "
            "cta, voicemail. No markdown fence, no commentary."
        )
        raw = await ctx.complete(
            [ctx.system_message(), LLMMessage(role="user", content=content)],
            purpose="call_script",
            # 7 keys, two of them arrays (discovery questions; objection/response pairs).
            # Measured live 2026-08-27: the inherited default of 800 stopped mid-sentence at
            # 2,483 chars and the whole script fell back to the generic stub; 1,800 closed and
            # produced 5 questions and 4 objections.
            max_tokens=1800,
            variables={
                "account": ctx.account.name,
                "contact": contact_name,
                "title": contact.title if contact and contact.title else "",
                "value_prop": vp.get("name", "our platform"),
                "trigger": trigger,
                "pain": pains,
                "one_pain": first_pain(vp.get("pains_solved", [])),
            },
        )
        script = _coerce(raw, account=ctx.account.name, contact=contact_name)
        return {
            "contact_id": contact.id if contact else None,
            "script": script,
            "trigger_signal_id": trigger_signal.id if trigger_signal else None,
        }


register_agent(CallScriptAgent())
