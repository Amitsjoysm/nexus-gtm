"""The checks that run immediately before every send (spec §5).

**At send time, not at scheduling time.** A step is scheduled days ahead; between then and the send
the person can unsubscribe, reply, be paused by a colleague, or the mailbox can lose its grant. Each
of those makes the send wrong, and only the last check before the provider call can see them.

The outcome is one of three words, and the difference between them is what happens next:

* ``send`` — go ahead.
* ``hold`` — do not send, leave the step due. The condition passes: a paused mailbox reconnects, a
  provider limit resets, a colleague resumes. Nothing is lost.
* ``stop`` — do not send, and this enrollment is over for this contact: they unsubscribed, they
  replied, or they are on the do-not-contact list.

Everything here is a decision ABOUT already-loaded rows, so the ordering is cheap to state: the
stops come first, because a workspace that has been told to stop must stop even if its mailbox is
also broken.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

SEND = "send"
HOLD = "hold"
STOP = "stop"


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: str
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == SEND


GO = Decision(SEND)


def decide(
    *,
    suppressed_reason: str = "",
    inbound_since_scheduled: bool = False,
    enrollment_status: str = "active",
    paused_by_colleague: bool = False,
    mailbox_status: str = "connected",
    mailbox_paused_until: datetime | None = None,
    quality_problems: list[str] | None = None,
    is_first_touch: bool = False,
    credits_ok: bool = True,
    now: datetime | None = None,
) -> Decision:
    """Pure. One call per send, with rows the caller has already read."""
    if suppressed_reason:
        return Decision(STOP, f"the address is on the do-not-contact list ({suppressed_reason})")
    if inbound_since_scheduled:
        # They answered. Whatever this step was going to say, it is now the wrong thing to say.
        return Decision(STOP, "the contact replied after this step was scheduled")
    if enrollment_status in ("stopped", "completed"):
        return Decision(STOP, f"the enrollment is {enrollment_status}")
    if paused_by_colleague or enrollment_status == "paused":
        return Decision(HOLD, "a colleague paused this enrollment")
    if enrollment_status == "snoozed":
        return Decision(HOLD, "the enrollment is snoozed")
    if mailbox_status != "connected":
        return Decision(HOLD, f"the mailbox is {mailbox_status}")
    if mailbox_paused_until and now and mailbox_paused_until > now:
        return Decision(HOLD, "the mailbox is paused until its provider limit resets")
    problems = [p for p in (quality_problems or []) if p]
    if problems and not is_first_touch:
        # A first email is reviewed by a person before it goes; a follow-up is drafted just in time
        # and nobody is watching, so a failing one waits for review rather than reaching a buyer
        # (D17).
        return Decision(HOLD, f"the draft needs review: {', '.join(problems[:3])}")
    if not credits_ok:
        return Decision(HOLD, "the workspace cannot cover this send")
    return GO
