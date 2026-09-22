"""Which found email addresses may be SAVED on a contact.

Decided with the product owner 2026-09-22, after customers reported major deliverability failures:

* **valid** — saved.
* **catch-all**, or **risky from a mailbox check** — saved and labelled; the user decides whether to
  send (the existing "Send anyway" and the campaign's "Send to risky addresses").
* **invalid** — never kept. Remembered on the contact so it is never guessed again.
* **unknown**, or **risky from DNS alone** — not saved. A DNS grade only proves the DOMAIN takes mail;
  it cannot tell jane.doe@ from j.doe@, so every pattern ties and first.last wins by default — the
  guess behind the bounces.

The strict rule applies only when a real verifier is configured. The `stub` verifier means "none is
configured here" (dev, tests, offline), like `DemoSignalSource`; being strict against something that
checks nothing would hide every email and prove nothing, so it keeps today's behaviour — except that
an invalid address is never kept anywhere.
"""
from __future__ import annotations

from nexus.verification import (
    STATUS_CATCH_ALL,
    STATUS_INVALID,
    STATUS_RISKY,
    STATUS_VALID,
)

#: Verifiers that talk to the recipient's mail server about the specific MAILBOX.
MAILBOX_VERIFIERS = frozenset({"reacher"})
#: How many removed addresses a contact remembers, so none of them is guessed again.
MAX_REJECTED = 20


def verification_configured() -> bool:
    """Whether a real verifier is configured (anything other than the offline stub)."""
    from nexus.core.config import get_settings

    provider = (get_settings().email_verify_provider or "").strip().lower()
    return provider not in ("", "stub")


def check_level(source: str | None) -> str:
    """What a verdict's source actually examined: ``mailbox``, ``domain``, or nothing.

    A composite verifier is named ``reacher+dns``; whichever part answered is what counts, and a
    verdict carries the name of the part that produced it.
    """
    parts = {p.strip() for p in (source or "").lower().split("+") if p.strip()}
    if parts & MAILBOX_VERIFIERS:
        return "mailbox"
    if "dns" in parts:
        return "domain"
    return ""


def keep_address(status: str | None, check: str) -> bool:
    """Whether an address with this verdict may be written onto a contact."""
    if status == STATUS_INVALID:
        return False
    if not verification_configured():
        return True
    if status in (STATUS_VALID, STATUS_CATCH_ALL):
        return True
    if status == STATUS_RISKY:
        return check == "mailbox"
    return False


def rejected_emails(contact) -> list[str]:
    return list((getattr(contact, "custom_fields", None) or {}).get("rejected_emails") or [])


def remember_rejected(contact, emails) -> None:
    """Record addresses proven invalid for this person, newest first, bounded."""
    fresh = [e.strip().lower() for e in emails if e and e.strip()]
    if not fresh:
        return
    known = rejected_emails(contact)
    merged = list(dict.fromkeys(fresh + [e for e in known if e not in fresh]))[:MAX_REJECTED]
    if merged != known:
        fields = dict(contact.custom_fields or {})
        fields["rejected_emails"] = merged
        # Reassigned, not mutated, so SQLAlchemy sees the JSON column change.
        contact.custom_fields = fields


def forget_email(contact) -> None:
    """Remove the contact's address entirely — an invalid address is not kept, even labelled."""
    contact.email = None
    contact.email_status = None
    contact.email_confidence = 0.0
