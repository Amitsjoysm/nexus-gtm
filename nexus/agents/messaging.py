"""Messaging Agent — personalized outreach grounded in value props and the triggering signal."""
from __future__ import annotations

from nexus.agents.copy import (
    OUTPUT_CONTRACT,
    account_facts,
    email_rules,
    extra_fact_lines,
    format_pains,
    pick_anchor,
    product_line,
    select_value_prop,
    sender_block,
    shape_for,
    signal_facts,
    split_ps,
    today_line,
)
from nexus.agents.email_quality import (
    check_draft,
    quoted_activity,
    unverified_in_draft,
    unverified_terms,
)
from nexus.agents.email_style import draft_options, ps_line_on, style_prompt
from nexus.agents.llm import LLMMessage
from nexus.agents.runtime import AgentContext, BaseAgent, register_agent


def _split_subject(text: str) -> tuple[str, str]:
    # Every draft passes through here, so this is where look-alike characters are removed: the
    # composer, the reply desk and the campaign review all show what is sent (2026-09-30).
    from nexus.agents.copy import tidy_text

    text = tidy_text(text or "")
    if text.lower().startswith("subject:"):
        head, _, rest = text.partition("\n")
        return head.split(":", 1)[1].strip(), rest.strip()
    return "", text.strip()


#: What this email IS, by engagement touch, for the opening line of the prompt. "Write a cold email"
#: for a reply to someone who asked a question told the model to pitch them from scratch.
_TOUCH_NOUN = {
    "first": "a cold email",
    "followup": "a follow-up email",
    "signal": "a follow-up email",
    "reengage": "an email picking the conversation back up",
    "response": "a reply",
}


def _touch(ctx) -> str:
    """The engagement kind this draft is for. Every caller outside the engine (the composer, plays,
    the orchestrator) writes a first email, so that is the default."""
    touch = str(ctx.inputs.get("touch_kind") or "").strip().lower()
    return touch if touch in _TOUCH_NOUN else "first"


def _first_name(contact) -> str:
    """What the greeting has to say. A full name in a salutation reads as a mail merge."""
    full = (getattr(contact, "full_name", "") or "").strip() if contact else ""
    return full.split()[0] if full else ""


def retry_instruction(problems: list[str], previous: str = "") -> str:
    """The corrective turn: what was wrong, in the model's own terms, and the draft it was wrong in.

    Naming the faults is the whole point. "Try again" re-rolls the same dice; "there is no greeting
    and no sign-off" is a brief. And "keep everything else the same" means nothing to a model that
    is not shown what it wrote, which it was not until 2026-10-29: the retry was a fresh roll with a
    list of complaints attached, so it fixed one thing and changed five others.
    """
    joined = "\n".join(f"- {p}" for p in problems)
    shown = (f"Your previous draft:\n-----\n{previous.strip()}\n-----\n"
             if (previous or "").strip() else "")
    return (
        f"{shown}"
        "That draft had these problems:\n"
        f"{joined}\n"
        "Rewrite the whole email, fixing every one of them and keeping everything else the same. "
        "Return it in the same format: 'Subject: ...', a blank line, then the body."
    )


async def _research_facts(ctx) -> list[str]:
    """The highlights of this account's latest research brief, which the research agent stored and
    no draft read. Never raises: a brief that cannot be read costs the extra facts, not the draft."""
    try:
        from nexus.models.intelligence import AgentRun

        ts = ctx.ts
        stmt = (ts.select(AgentRun, AgentRun.agent == "research",
                          AgentRun.account_id == ctx.account.id,
                          AgentRun.status == "completed")
                .order_by(AgentRun.created_at.desc()).limit(1))
        run = (await ts.session.scalars(stmt)).first()
        return [str(f) for f in ((run.output or {}).get("facts") or [])] if run else []
    except Exception:  # noqa: BLE001
        return []


async def _email_settings(ctx) -> dict:
    """This workspace's email settings, for the house style, with the sending mailbox's own style
    over them when the caller names one (`mailbox_id`, 2026-11-04). Never raises: a style that
    cannot be read must cost the styling, not the draft."""
    try:
        from nexus.agents.email_style import merged_settings
        from nexus.models.engagement import MailboxConnection
        from nexus.models.identity import Tenant

        ts = getattr(ctx, "ts", None) or getattr(ctx, "session", None)
        if ts is None:
            return {}
        tenant = await ts.session.get(Tenant, ts.tenant_id)
        settings = dict(getattr(tenant, "email_settings", None) or {})
        mailbox_id = ctx.inputs.get("mailbox_id") if isinstance(ctx.inputs, dict) else None
        mailbox = await ts.get(MailboxConnection, mailbox_id) if mailbox_id else None
        return merged_settings(settings, getattr(mailbox, "style", None))
    except Exception:  # noqa: BLE001
        return {}


class MessagingAgent(BaseAgent):
    name = "messaging"

    async def run(self, ctx: AgentContext) -> dict:
        if ctx.account is None:
            return {"error": "messaging requires an account"}
        # Only events inside the platform window may be cited (`agents.facts.citable_signals`):
        # a 0.4 mention is a company's own blog post, not a reason to write.
        from nexus.agents.facts import citable_signals

        signals = citable_signals(ctx.signals)

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

        # ONE ANCHOR, PICKED ONCE (content writers, 2026-10-29). It drives the hook, the value prop
        # and `trigger_signal_id`; they were three separate picks, so one email could open on a
        # funding round, pitch the hiring value prop and log a third signal as its reason.
        anchor = pick_anchor(signals, contact)
        trigger = anchor.title if anchor else "your current priorities"

        # Person-level personalization: write to the individual (role and function, the same
        # anchor, and any social insights), not just the account.
        from nexus.core.config import get_settings
        from nexus.personalization.brief import build_person_brief, role_words

        brief = build_person_brief(contact, ctx.account, signals, anchor=anchor) \
            if contact else None
        hook = trigger

        vps = ctx.relevance_context.value_props
        # MATCHED to the anchor (its words and its kind), with the person's function as the
        # tie-break, not always the first one. Pitching value_props[0] at every account regardless
        # of the trigger is the mail-merge failure in its purest form.
        vp = select_value_prop(vps, [anchor] if anchor else [],
                               role=role_words(contact.title, contact.seniority) if contact else "")
        # Nouns, formatted to sit inside a sentence — see nexus/agents/copy.py for the
        # garbled email this replaced.
        pains = format_pains(vp.get("pains_solved", []))
        what = product_line(vp)

        angle = (ctx.inputs.get("angle") or "").strip()
        touch = _touch(ctx)
        # This contact's seeded opener, ask and subject style (`copy.shape_for`): stable for them,
        # varied across a list, and logged on the message.
        shape = shape_for(contact.id if contact else ctx.account.id)
        # Facts first, then the task, then the rules, then the output contract. The facts have to
        # precede the rules or "use only facts given above" refers to nothing.
        who = contact.full_name if contact else "the buyer"
        role = (contact.title or "").strip() if contact else ""
        # Everything we already know about the company. Until this existed the prompt carried
        # `account.name` and nothing else, so the model could not tell a 40-person fintech from a
        # 6,000-person manufacturer and wrote copy that fitted neither.
        facts = account_facts(ctx.account, ctx.relevance_context.icp)

        content = (
            # First, because everything after it is dated relative to now: the signal ages, and the
            # specific day the CTA is told to propose.
            f"{today_line()}\n\n"
            f"Write {_TOUCH_NOUN[touch]} from an experienced SDR to {who}"
            f"{f', {role},' if role else ''} at {ctx.account.name}.\n\n"
            f"WHAT WE KNOW ABOUT THEM:\n{facts}\n"
        )
        # The anchor WITH its body. Only `title` used to reach the model: we crawl "raised $40M led
        # by Sequoia to expand European operations", store it, bill for it, and then sent the
        # headline. The other signals are background, labelled so, and left out when the context
        # pack already lists them.
        context_pack = (ctx.inputs.get("context_pack") or "").strip()
        if anchor is not None:
            content += f"\nTHE TRIGGER (the one signal this email is about):\n" \
                       f"{signal_facts([anchor], limit=1)}\n"
            others = [s for s in signals if s is not anchor]
            if others and "LIVE SIGNALS" not in context_pack:
                content += ("\nOTHER SIGNALS (background only; do not open on them):\n"
                            f"{signal_facts(others)}\n")
        extra = extra_fact_lines(research=await _research_facts(ctx),
                                 fit=ctx.relevance_context.account_fit)
        if extra:
            content += f"\n{extra}\n"
        # The engagement engine's context pack (spec §7): the date in the buyer's timezone, the
        # whole conversation so far and every other conversation with this person. Placed with
        # the facts, before the rules, so "use only facts given above" covers it too.
        if context_pack:
            content += f"\n{context_pack}\n"
        # Who is sending, for callers without a context pack (the contact composer passes the
        # signed-in rep). Without it the model invents a name to sign with ("Best, Alex").
        sender_name = str(ctx.inputs.get("sender_name") or "")
        if "YOU (THE SENDER)" not in context_pack:
            sender = sender_block(sender_name)
            if sender:
                content += f"\n{sender}"
        facts_to_use = [f for f in (ctx.inputs.get("personal_facts") or []) if f]
        content += (
            f"\nThe trigger to open on: {hook}\n"
            f"The value proposition to land: {vp.get('name')}\n"
            f"The problems it removes: {pains}\n"
        )
        if what:
            # What the product IS, for the one plain sentence `STRUCTURE_RULE` asks for. Drafts
            # described the problem and never said what was on offer.
            content += f"What we do (say it in one plain sentence, in a buyer's words): {what}\n"
        if brief is not None:
            content += brief.to_prompt(max_posts=get_settings().personalization_max_posts) + "\n"
        if angle:
            # Per-touch cadence angle: shape this specific touch (e.g. a follow-up nudge,
            # a case-study share) so successive touches don't repeat the same message.
            content += f"Angle for this specific touch: {angle}\n"
        # What the verifier accepts as given: the system grounding and every fact above. NOT the
        # house-style samples below: a sample's customer and metric belong to another deal.
        grounding = f"{ctx.system_message().content}\n{content}"
        # The workspace's own voice: tone, length and its sample emails, offered as STRUCTURE only.
        # Placed after the facts and before the rules, so the rules — including "use only facts
        # given above" — are still the last word on what may be claimed.
        email_settings = await _email_settings(ctx)
        # The workspace's target length and first-email ask, read once so the rules below and the
        # check after generation hold the draft to the same thing.
        options = draft_options(email_settings)
        style = style_prompt(email_settings)
        if style:
            content += f"\n{style}"
        guidance = (ctx.inputs.get("guidance") or "").strip()
        if guidance:
            # Reviewer redraft instructions from the approval gate take precedence — they are
            # an explicit human steer on this exact message ("make it shorter", "mention SOC2").
            content += f"Revise per the reviewer's instructions: {guidance}\n"
            grounding += f"\n{guidance}"
        # Rules last so they are the final thing before generation, and the reviewer's steer above
        # is never buried under them.
        # The rules for THIS touch (`copy.email_rules`): a reply offers times, an unanswered email
        # asks one interest question, a threaded one keeps the thread's subject.
        # A reply the AI sends by itself asks for a day instead of offering times (`auto_reply`).
        automatic = bool(ctx.inputs.get("automatic_reply"))
        # The optional P.S. only on a first email, which a person always reviews (D4), and only
        # when the workspace switched it on.
        wants_ps = ps_line_on(email_settings) and touch == "first" and not automatic
        content += (f"\n{email_rules(touch, automatic=automatic, shape=shape, ps=wants_ps, **options)}"
                    f"\n{OUTPUT_CONTRACT}")
        allow = [n for n in (who if contact else "", ctx.account.name, sender_name) if n]
        posts = brief.posts if brief is not None else []

        def checked(subject: str, body: str) -> list[str]:
            return check_draft(
                subject=subject, body=body, first_name=_first_name(contact),
                facts=facts_to_use, company_name=ctx.account.name or "", touch=touch,
                context=grounding, allow=allow, activity=posts, **options,
            )

        variables = {
            "account": ctx.account.name,
            "contact": contact.full_name if contact else "there",
            "value_prop": vp.get("name", "our platform"),
            "trigger": trigger,
            "pain": pains,
            "what": what,
            "shape": shape,
        }
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
            variables=variables,
        )
        subject, body = _split_subject(message)
        # A P.S. never rides in the body: it is a separate field a person confirms on Review
        # (`copy.PS_RULE`), so one a model added unasked is taken off and dropped.
        body, ps = split_ps(body)
        # CHECK IT, THEN FIX IT ONCE. A rule in a prompt is a request: the same instruction is
        # followed on one generation and dropped on the next, which is how drafts reached reps with
        # no greeting at all (reported 2026-09-16). One retry, carrying the specific complaints and
        # the draft they are about — a second blind attempt would be a coin flip, and more than one
        # turns every draft into three completions nobody asked to pay for.
        problems = checked(subject, body)
        if problems:
            retry = LLMMessage(
                role="user",
                content=f"{content}\n\n"
                        f"{retry_instruction(problems, previous=f'Subject: {subject}{chr(10)*2}{body}')}",
            )
            second = await ctx.complete(
                [ctx.system_message(), retry], purpose="outreach_message", max_tokens=1500,
                variables=variables,
            )
            retry_subject, retry_body = _split_subject(second)
            retry_body, retry_ps = split_ps(retry_body)
            # Keep the retry only when it is genuinely better: a second attempt that is empty, or
            # that breaks MORE rules than the first, is not an improvement worth showing.
            if retry_body.strip() and len(checked(retry_subject, retry_body)) < len(problems):
                message, subject, body, ps = second, retry_subject, retry_body, retry_ps
        # A P.S. is kept only when asked for, and only when it says nothing the verifier cannot
        # find and quotes nothing: it sits below the signature with a person's word on it.
        if not wants_ps or unverified_terms(ps, grounding, allow=allow, strict=True) \
                or quoted_activity(ps, posts):
            ps = ""
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
            "trigger_signal_id": anchor.id if anchor is not None else None,
            "value_prop": vp.get("name"),
            "shape": shape,
            "ps": ps,
            # What the fact verifier could not find in the grounding, for the drafter: it holds an
            # automatic follow-up whose body still names one (`drafting/drafter.py`).
            "unverified": unverified_in_draft(subject=subject, body=body, context=grounding,
                                              allow=allow),
        }


register_agent(MessagingAgent())
