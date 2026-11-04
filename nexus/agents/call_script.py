"""Call Script Agent — an AI cold-call talk track grounded in the account's signals + ICP.

Sibling of the Messaging Agent, but for the phone: it returns a structured script (opener, hook,
value prop, discovery questions, objection handling, first reactions, CTA, voicemail) the SDR reads
live. Uses the same LLM chain (with Groq key rotation + stub fallback), so it works offline in CI.

Since 2026-10-29 (content writers) it is checked like an email: `email_quality.check_call_script`
reads the parts a rep notices while saying them, and one regeneration carries the complaints and the
script they are about.
"""
from __future__ import annotations

import json
import re

from nexus.agents.copy import (
    CALL_RULES,
    FIRST_REACTIONS,
    account_facts,
    caller_block,
    extra_fact_lines,
    first_pain,
    format_pains,
    pick_anchor,
    product_line,
    select_value_prop,
    signal_facts,
    today_line,
)
from nexus.agents.email_quality import check_call_script
from nexus.agents.llm import LLMMessage
from nexus.agents.runtime import AgentContext, BaseAgent, register_agent

_KEYS = ("opener", "hook", "value_prop", "discovery_questions", "objections", "first_reactions",
         "cta", "voicemail")


# The first {...} in the response. Models add a lead-in sentence and a ``` fence however firmly
# the prompt forbids them, and throwing away an otherwise-complete script over its wrapper costs
# the rep the whole call.
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)

#: The placeholders a model writes when it does not know who is calling.
_NAME_SLOT = re.compile(r"[<\[{]\s*(?:your|rep|caller)(?:'s)?\s+(?:first\s+)?name\s*[>\]}]", re.I)
_COMPANY_SLOT = re.compile(r"[<\[{]\s*(?:your\s+)?company(?:\s+name)?\s*[>\]}]", re.I)


def _fill(value: str, caller: str, company: str) -> str:
    """Put the caller's real first name and company where the model left a slot for them. A slot
    with nothing known to fill it stays, and `check_call_script` names it."""
    if caller:
        value = _NAME_SLOT.sub(caller.split()[0], value)
    if company:
        value = _COMPANY_SLOT.sub(company, value)
    return value


def _pairs(raw, first: str) -> list[dict]:
    out = []
    for item in (raw or []):
        if isinstance(item, dict):
            out.append({first: str(item.get(first) or ""), "response": str(item.get("response") or "")})
    return out


def _fallback(*, account: str, contact: str, caller: str, company: str, pain: str) -> dict:
    """A script a rep can say when the model's could not be read: no placeholders, no stock lines.

    It used to open "this is <your name> from <your company>" and hook on "I wanted to reach out
    about...": a rep read the brackets aloud, or a banned line, to the one person on the phone."""
    first = (contact or "there").split()[0]
    me = caller.split()[0] if caller else ""
    who = f"it's {me} from {company}" if me and company else (f"it's {me}" if me else
                                                            (f"I'm calling from {company}"
                                                             if company else ""))
    intro = f"Hi {first}, {who}." if who else f"Hi {first}."
    problem = pain or "the usual pipeline bottlenecks"
    return {
        "opener": f"{intro} This is a cold call. Can I take thirty seconds to say why I called?",
        # Deliberately NOT the raw response. This slot is read aloud, and a truncated JSON
        # document in front of a rep mid-dial is worse than a plain sentence: it cannot be
        # spoken, and it looks like the product is broken to the one person holding the phone.
        "hook": f"Teams like the one at {account} usually find {problem} gets harder as they grow.",
        "value_prop": "",
        "discovery_questions": [f"How are you handling {problem} today?"],
        "objections": [],
        "first_reactions": [
            {"reaction": FIRST_REACTIONS[0],
             "response": f"{(who[:1].upper() + who[1:]) if who else 'A cold call'}, about {problem}. "
                         "Thirty seconds and you can tell me if it's worth more."},
            {"reaction": FIRST_REACTIONS[1],
             "response": f"Happy to. So it's useful: is {problem} something you deal with?"},
            {"reaction": FIRST_REACTIONS[2],
             "response": f"Understood. One question then: is {problem} on your list this quarter?"},
        ],
        "cta": "Could we find 15 minutes later this week to see whether it fits?",
        "voicemail": (f"Hi {first}, {who + ', ' if who else ''}calling about {problem} at "
                      f"{account}. I'll send a short email so you have my details."),
    }


def _coerce(raw: str, *, account: str, contact: str, caller: str = "", company: str = "",
            pain: str = "") -> dict:
    """Parse the model's JSON; on any failure degrade to a usable script (never raise)."""
    match = _JSON_OBJ_RE.search(raw or "")
    try:
        data = json.loads(match.group(0) if match else raw)
        if isinstance(data, dict):
            script = {
                "opener": str(data.get("opener", "")),
                "hook": str(data.get("hook", "")),
                "value_prop": str(data.get("value_prop", "")),
                "discovery_questions": [str(q) for q in (data.get("discovery_questions") or [])],
                "objections": _pairs(data.get("objections"), "objection"),
                "first_reactions": _pairs(data.get("first_reactions"), "reaction"),
                "cta": str(data.get("cta", "")),
                "voicemail": str(data.get("voicemail", "")),
            }
            for key in ("opener", "hook", "value_prop", "cta", "voicemail"):
                script[key] = _fill(script[key], caller, company)
            for key in ("objections", "first_reactions"):
                for item in script[key]:
                    item["response"] = _fill(item["response"], caller, company)
            return script
    except (ValueError, TypeError):
        pass
    return _fallback(account=account, contact=contact, caller=caller, company=company, pain=pain)


async def _caller(ctx) -> tuple[str, str]:
    """Who is calling and from where: ``(name, company)``. The name is the caller the task belongs
    to (`caller_user_id`), else whoever added the account; the company is the workspace's. Never
    raises: an unknown caller leaves the script to say less, never to invent a name."""
    name, company = str(ctx.inputs.get("caller_name") or ""), ""
    try:
        from nexus.models.identity import Tenant, User

        ts = ctx.ts
        user_id = ctx.inputs.get("caller_user_id") or getattr(ctx.account, "owner_user_id", None)
        if not name and user_id:
            user = await ts.session.get(User, user_id)
            name = ((user.full_name if user else "") or "").strip()
        tenant = await ts.session.get(Tenant, ts.tenant_id)
        company = ((tenant.name if tenant else "") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return name, company


def _retry_text(problems: list[str], script: dict) -> str:
    joined = "\n".join(f"- {p}" for p in problems)
    return (
        "Your previous script:\n-----\n"
        f"{json.dumps(script, ensure_ascii=False)}\n-----\n"
        f"It had these problems:\n{joined}\n"
        "Rewrite the whole script, fixing every one of them and keeping everything else the same. "
        "Return ONLY the JSON object, with the same keys."
    )


class CallScriptAgent(BaseAgent):
    name = "call_script"

    async def run(self, ctx: AgentContext) -> dict:
        if ctx.account is None:
            return {"error": "call_script requires an account"}
        # Only events inside the platform window may be cited (`agents.facts.citable_signals`),
        # which matters most here: a weak mention read out loud is the worst opener there is.
        from nexus.agents.facts import citable_signals

        signals = citable_signals(ctx.signals)

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

        # One anchor, the email's rule (`copy.pick_anchor`): it drives the hook, the value prop and
        # `trigger_signal_id`, which used to be three separate picks.
        anchor = pick_anchor(signals, contact)
        trigger = anchor.title if anchor else "your current priorities"

        from nexus.core.config import get_settings
        from nexus.personalization.brief import build_person_brief, role_words

        vps = ctx.relevance_context.value_props
        # Matched to the anchor rather than always the first — same reasoning as messaging.py:
        # pitching value_props[0] regardless of the trigger is the mail-merge failure.
        vp = select_value_prop(vps, [anchor] if anchor else [],
                               role=role_words(contact.title, contact.seniority) if contact else "")
        # Nouns, formatted to sit inside a sentence — see nexus/agents/copy.py for the
        # garbled email this replaced.
        pains = format_pains(vp.get("pains_solved", []))
        one_pain = first_pain(vp.get("pains_solved", []))
        what = product_line(vp)

        # Person-level personalization: shape the talk track to this individual (role and
        # function, the same anchor, social insights), not just the account.
        brief = build_person_brief(contact, ctx.account, signals, anchor=anchor) \
            if contact else None
        hook = trigger
        person_block = (
            " " + brief.to_prompt(max_posts=get_settings().personalization_max_posts, channel="call")
            if brief is not None else ""
        )
        caller, company = await _caller(ctx)
        # Facts, then task, then rules, then the output contract — the same order as the email
        # agent, so "use only facts given above" has something above it to refer to.
        # The firmographics and the anchor's BODY. A talk track is spoken live, so the rep needs
        # the specifics — the amount raised, the roles being hired — not a headline they then have
        # to bluff around the moment the buyer asks a follow-up question.
        facts = account_facts(ctx.account, ctx.relevance_context.icp)
        signals_block = ""
        if anchor is not None:
            signals_block = ("\nTHE TRIGGER (the one signal this call is about):\n"
                             f"{signal_facts([anchor], limit=1)}\n")
            others = [s for s in signals if s is not anchor]
            if others:
                signals_block += ("\nOTHER SIGNALS (background only; do not open on them):\n"
                                  f"{signal_facts(others)}\n")
        extra = extra_fact_lines(research=None, fit=ctx.relevance_context.account_fit)
        caller_lines = caller_block(caller, company)

        content = (
            # Same anchor as the email agent: the `cta` field is told to name a specific weekday,
            # and a model with no clock either invents one or hedges back to a vague close.
            f"{today_line()}\n\n"
            f"Write a cold-call talk track for an experienced SDR calling {contact_name} "
            f"({contact.title if contact and contact.title else 'a buyer'}) at {ctx.account.name}.\n\n"
            f"WHAT WE KNOW ABOUT THEM:\n{facts}\n"
            f"{signals_block}"
            f"{f'{chr(10)}{extra}{chr(10)}' if extra else ''}"
            f"{f'{chr(10)}{caller_lines}' if caller_lines else ''}"
            f"\nThe trigger to open on: {hook}\n"
            f"The value proposition to land: {vp.get('name')}\n"
            f"The problems it removes: {pains}\n"
            f"{f'What we do (one plain spoken sentence): {what}{chr(10)}' if what else ''}"
            f"{person_block}\n"
        )
        grounding = f"{ctx.system_message().content}\n{content}"
        content += (
            f"{CALL_RULES}\n"
            "Return ONLY a JSON object with keys: opener, hook, value_prop, "
            "discovery_questions (array of strings), objections (array of {objection, response}), "
            "first_reactions (array of {reaction, response}), cta, voicemail. "
            "No markdown fence, no commentary."
        )
        allow = [n for n in (contact_name if contact else "", ctx.account.name, caller, company)
                 if n]
        posts = brief.posts if brief is not None else []
        variables = {
            "account": ctx.account.name,
            "contact": contact_name,
            "title": contact.title if contact and contact.title else "",
            "value_prop": vp.get("name", "our platform"),
            "trigger": trigger,
            "pain": pains,
            "one_pain": one_pain,
            "caller": caller,
            "company": company,
        }

        def coerce(raw: str) -> dict:
            return _coerce(raw, account=ctx.account.name, contact=contact_name, caller=caller,
                           company=company, pain=one_pain)

        raw = await ctx.complete(
            [ctx.system_message(), LLMMessage(role="user", content=content)],
            purpose="call_script",
            # 8 keys, three of them arrays (discovery questions; objection/response pairs; the
            # first reactions). Measured live 2026-08-27: the inherited default of 800 stopped
            # mid-sentence at 2,483 chars and the whole script fell back to the generic stub; 1,800
            # closed and produced 5 questions and 4 objections. 2,200 since the third array.
            max_tokens=2200,
            variables=variables,
        )
        script = coerce(raw)
        problems = check_call_script(script, context=grounding, allow=allow, activity=posts)
        if problems:
            # One regeneration with the complaints and the script they are about, kept only when it
            # is better: the email agent's rule, for the same reasons.
            second = coerce(await ctx.complete(
                [ctx.system_message(),
                 LLMMessage(role="user", content=f"{content}\n\n{_retry_text(problems, script)}")],
                purpose="call_script", max_tokens=2200, variables=variables,
            ))
            second_problems = check_call_script(second, context=grounding, allow=allow,
                                                activity=posts)
            if len(second_problems) < len(problems):
                script, problems = second, second_problems
        return {
            "contact_id": contact.id if contact else None,
            "script": script,
            "problems": problems,
            "trigger_signal_id": anchor.id if anchor is not None else None,
        }


register_agent(CallScriptAgent())
