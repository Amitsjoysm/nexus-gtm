"""Messaging Agent — personalized outreach grounded in value props and the triggering signal."""
from __future__ import annotations

from nexus.agents.copy import (
    EMAIL_RULES,
    OUTPUT_CONTRACT,
    account_facts,
    format_pains,
    select_value_prop,
    signal_facts,
    today_line,
)
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

        # REFUSE TO PITCH A PRODUCT WE HAVE NOT BEEN TOLD ABOUT.
        #
        # Without this the run continues with the placeholder value prop below ("our platform")
        # and a prompt reading "PRODUCT CONTEXT: (none provided)". The output is fluent and looks
        # finished, so nothing reads as broken — but it pitches whatever else is in context.
        # Measured 2026-08-31: an account with a `hiring` signal produced a job application.
        #
        # Returning `error` rather than raising is deliberate and follows the convention the other
        # five agents already use for "cannot run on this input". Per workers/durability.py, a
        # returned `error` key is a NORMAL TERMINAL OUTCOME — it is not marked JOB_FAILED, so it
        # is not retried and not dead-lettered. A raise here would turn every unconfigured
        # workspace into a stream of dead letters for a state the operator must fix in the UI.
        #
        # This is the same bias as Settings._reject_synthetic_signals_in_production: silently
        # producing content nobody can tell is wrong is worse than a clear refusal, because
        # everything these agents generate is sent to a real buyer.
        if not ctx.relevance_context.is_configured:
            return {
                "error": "relevance_profile_not_configured",
                "detail": (
                    "This workspace has no product context or value propositions, so there is "
                    "nothing to pitch. Add them on the Relevance page — paste your website to "
                    "draft them automatically — then try again."
                ),
            }

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
        # MATCHED to what triggered the outreach, not always the first one. Pitching value_props[0]
        # at every account regardless of the trigger is the mail-merge failure in its purest form:
        # a hiring signal should pull the value prop about ramping new hires.
        vp = select_value_prop(vps, ctx.signals)
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
        # Everything we already know about the company. Until this existed the prompt carried
        # `account.name` and nothing else, so the model could not tell a 40-person fintech from a
        # 6,000-person manufacturer and wrote copy that fitted neither.
        facts = account_facts(ctx.account)
        # The signals WITH their bodies. Only `title` used to reach the model: we crawl "raised
        # $40M led by Sequoia to expand European operations", store it, bill for it, and then sent
        # the headline "Acme raises Series B". The specifics a rep opens on were being discarded.
        recent = signal_facts(ctx.signals)

        content = (
            # First, because everything after it is dated relative to now: the signal ages, and the
            # specific day the CTA is told to propose.
            f"{today_line()}\n\n"
            f"Write a cold email from an experienced SDR to {who}"
            f"{f', {role},' if role else ''} at {ctx.account.name}.\n\n"
            f"WHAT WE KNOW ABOUT THEM:\n{facts}\n"
        )
        if recent:
            content += f"\nRECENT SIGNALS (strongest first):\n{recent}\n"
        content += (
            f"\nThe trigger to open on: {hook}\n"
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
            # THE DEFAULT 800 IS NOT ENOUGH FOR A REASONING MODEL, and this deployment runs one
            # (`openai/gpt-oss-120b`). Reasoning tokens are charged against `max_tokens` alongside
            # the answer, so the budget buys the model's thinking first and the email second.
            #
            # Measured 2026-09-09 by replaying the real 5,146-character prompt: reasoning alone came
            # to 1,923 characters — roughly 550 tokens — leaving about a hundred for a 90-word
            # email. It fits when the model thinks briefly and overruns when it does not, so the
            # failure is INTERMITTENT: five live attempts returned four empty drafts and one good
            # one. An overrun ends with `finish_reason: length` and EMPTY content, which is exactly
            # what the blank-draft guard below started catching.
            #
            # The sibling agent already learned this: `call_script` carries 1,800 for the same
            # reason and has never shown the fault. Sized to the same shape rather than to the
            # email's own length, because what needs the headroom is the thinking, not the output.
            max_tokens=1500,
            variables={
                "account": ctx.account.name,
                "contact": contact.full_name if contact else "there",
                "value_prop": vp.get("name", "our platform"),
                "trigger": trigger,
                "pain": pains,
            },
        )
        subject, body = _split_subject(message)
        # A BLANK COMPLETION IS NOT A DRAFT. Observed live 2026-09-08: the provider returned an
        # empty string, `_split_subject("")` yielded two empty strings, and the run reported
        # `status: completed` with an empty subject and an empty body — which reaches the approval
        # queue looking like the product wrote nothing to say.
        #
        # The sibling agent already refuses to do this: `call_script._coerce` degrades to a usable
        # script rather than passing an unusable one on. There is no equivalent degraded email —
        # half a cold email is worse than none — so this reports instead, in the same shape
        # `call_script` uses for its own refusals.
        if not body.strip():
            return {
                "error": "empty_completion",
                "detail": (
                    "The model returned nothing for this draft. Nothing was saved. This is "
                    "usually transient — try again; if it persists, check the LLM provider key "
                    "and model on the Control plane."
                ),
                "contact_id": contact.id if contact else None,
            }
        return {
            "contact_id": contact.id if contact else None,
            "subject": subject,
            "body": body,
            "message": message,
            "trigger_signal_id": trigger_signal.id if trigger_signal else None,
        }


register_agent(MessagingAgent())
