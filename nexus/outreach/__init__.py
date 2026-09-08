"""One-off outbound email: a rep sends the draft they just read, from their own mailbox."""

from nexus.outreach.send import (
    MailboxNotConnected,
    SendRefused,
    resolve_rep_mailbox,
    send_to_contact,
)

__all__ = [
    "MailboxNotConnected",
    "SendRefused",
    "resolve_rep_mailbox",
    "send_to_contact",
]
