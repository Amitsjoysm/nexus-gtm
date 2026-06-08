"""Triage enrichment for inbox rows.

A rep should be able to triage an inbox task without opening the account: is the buying
signal fresh, can we actually reach the buyer, and is there enough to ground a message?
:class:`TriageSummary` rolls those three glanceable cues up per row. Everything here is
pure and offline — it reads already-persisted fields (signal, account, best contact) and
never makes a network call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nexus.core.db import ensure_aware, utcnow
from nexus.models.account import Account, Contact
from nexus.models.signal import SignalEvent
from nexus.models.workflow import InboxTask

# How a verification verdict ranks when choosing the contact that best represents an
# account's reachability. A verified-deliverable contact beats an unknown one, which beats
# a never-verified one, which beats one we know is undeliverable.
_STATUS_RANK = {"valid": 3, "unknown": 2, None: 1, "invalid": 0}


@dataclass(slots=True)
class TriageSummary:
    """A glanceable, per-row triage rollup.

    All fields are optional: a task may lack a linked signal, account, or reachable
    contact, and we degrade gracefully rather than fabricate a value.
    """

    signal_kind: str | None = None
    signal_strength: float | None = None
    signal_age_hours: float | None = None   # intent recency, hours since the signal fired
    deliverability: str | None = None       # best contact's verification verdict
    email_confidence: float | None = None    # enrichment confidence the email is correct
    research_ready: bool = False             # account is groundable (has a domain to research)

    def as_dict(self) -> dict:
        return {
            "signal_kind": self.signal_kind,
            "signal_strength": self.signal_strength,
            "signal_age_hours": self.signal_age_hours,
            "deliverability": self.deliverability,
            "email_confidence": self.email_confidence,
            "research_ready": self.research_ready,
        }


def pick_contact(contacts: list[Contact]) -> Contact | None:
    """Choose the contact that best represents account reachability.

    Only contacts with an email qualify; among those, prefer the strongest verification
    verdict, then the highest enrichment confidence.
    """
    emailed = [c for c in contacts if c.email]
    if not emailed:
        return None
    return max(
        emailed,
        key=lambda c: (_STATUS_RANK.get(c.email_status, 1), c.email_confidence or 0.0),
    )


def summarize(
    task: InboxTask,
    *,
    signal: SignalEvent | None,
    account: Account | None,
    contact: Contact | None,
    now: datetime | None = None,
) -> TriageSummary:
    """Build the triage rollup for one task from its linked records."""
    now = now or utcnow()
    summary = TriageSummary()
    if signal is not None:
        summary.signal_kind = signal.kind
        summary.signal_strength = signal.strength
        occurred = ensure_aware(signal.occurred_at)
        if occurred is not None:
            summary.signal_age_hours = max((now - occurred).total_seconds() / 3600.0, 0.0)
    if account is not None:
        summary.research_ready = bool(account.domain)
    if contact is not None:
        summary.deliverability = contact.email_status
        summary.email_confidence = contact.email_confidence
    return summary
