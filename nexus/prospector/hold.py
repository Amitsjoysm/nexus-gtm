"""The campaign hold on Prospector emails (decision D19).

Prospector keeps the provider's email as delivered, unverified. The engagement engine has no send
gate on verification, so without this a bulk campaign would send to every unchecked vendor address,
which is the bounce problem ``enrichment/policy.py`` exists to stop. A Prospector contact is held out
of campaign enrollment until a verifier has called its address deliverable (valid, catch-all, or
risky at the mailbox), which the Verify button does. A one-off email from the contact page is not
held: a rep writing to one person is a decision, not a bulk send.

A leaf module (no imports from the engine), so ``engagement`` can read it without a cycle.
"""
from __future__ import annotations

SOURCE = "sourcing:prospector"
RELEASED = frozenset({"valid", "catch_all", "risky"})
HOLD_REASON = "unverified_prospector_email"


def held_until_verified(contact) -> bool:
    if (getattr(contact, "enrichment_source", None) or "") != SOURCE:
        return False
    return (getattr(contact, "email_status", None) or "") not in RELEASED
