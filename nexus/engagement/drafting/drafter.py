"""Write one engagement email with the existing drafting pipeline (spec §7, D5, D17).

The model, the prompt rules, the quality check and its one regeneration are `MessagingAgent`'s — a
second drafting path would drift, and the first thing to drift would be the rules that keep invented
facts out of a buyer's inbox. What this adds is the context pack (the conversation, the date, the
history) and the personalisation rule, both passed in as agent inputs.

Each outbound draft is charged as `ai.email_draft`, the capability the composer's drafts already
use, so a campaign's drafts and a one-off draft cost the same. A response the reply desk suggests
is `ai.reply_draft`: it reads the whole conversation as well as writing one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: What a draft is charged as. Named here, beside the meter, so the capability a caller passes and
#: the call that charges it are one module (``tests/test_billing_metering_coverage.py``).
EMAIL_DRAFT = "ai.email_draft"
REPLY_DRAFT = "ai.reply_draft"


@dataclass(slots=True)
class Draft:
    subject: str = ""
    body: str = ""
    problems: list[str] = field(default_factory=list)
    context_pack: str = ""
    facts: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.body.strip())


async def draft(ts, *, enrollment, contact, account, mailbox, step=None, kind: str = "first",
                thread=None, user_id: str | None = None, now=None,
                capability: str = EMAIL_DRAFT) -> Draft:
    """One draft. Never raises: a draft that cannot be written reports why.

    ``capability`` is what the draft is charged as. An outbound step is ``ai.email_draft``; a reply
    the desk suggests is ``ai.reply_draft``, priced separately because it reads the conversation as
    well as writing one — and because a workspace that answers every reply by hand should not be
    paying the same line as one that drafts every send.
    """
    from nexus.agents.email_quality import check_draft
    from nexus.agents.messaging import _first_name
    from nexus.agents.runtime import get_agent_runtime
    from nexus.billing.errors import QuotaExceeded
    from nexus.billing.meter import metered
    from nexus.engagement.drafting.context import build_context
    from nexus.engagement.subjects import reply_subject

    pack = await build_context(ts, enrollment=enrollment, contact=contact, account=account,
                               mailbox=mailbox, step=step, kind=kind, now=now)
    try:
        async with metered(ts, capability, user_id=user_id, source="engagement"):
            result = await get_agent_runtime().run(
                "messaging", ts, account_id=account.id, contact_id=contact.id,
                angle=getattr(step, "angle", "") or "", context_pack=pack.text,
                personal_facts=pack.facts,
            )
    except QuotaExceeded:
        return Draft(context_pack=pack.text, facts=pack.facts, error="out_of_credits")
    except Exception as exc:  # a failed draft is a state to show, not a crash
        return Draft(context_pack=pack.text, facts=pack.facts,
                     error=f"{type(exc).__name__}: {exc}"[:300])

    output = result.output or {}
    if result.status != "completed" or output.get("error"):
        return Draft(context_pack=pack.text, facts=pack.facts,
                     error=str(output.get("error") or result.error or "draft_failed"))

    subject = (output.get("subject") or "").strip()
    body = (output.get("body") or "").strip()
    if kind in ("followup", "reengage", "response") and thread is not None:
        # Exactly one "Re:", on the thread's own subject (D16): whatever the model wrote on the
        # subject line, a follow-up in a thread keeps the thread's subject.
        subject = reply_subject(thread.base_subject or subject)
    problems = check_draft(subject=subject, body=body, first_name=_first_name(contact),
                           facts=pack.facts, company_name=account.name or "")
    return Draft(subject=subject, body=body, problems=problems, context_pack=pack.text,
                 facts=pack.facts)
