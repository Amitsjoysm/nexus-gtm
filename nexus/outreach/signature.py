# nexus/outreach/signature.py
"""The sign-off block that goes out under a rep's email.

Reported 2026-09-16: a rep could not add a signature anywhere, so every drafted email left without
one. The word `signature` appeared nowhere in the backend.

**A signature belongs to the mailbox.** The mailbox is what identifies the sender — a reply goes
back to whoever sent it (`nexus/outreach/send.py`) — so the name, title and number under the message
have to be the same person's. The workspace default exists for the rep who has not written one yet:
signing as the company beats signing as nobody.

Plain text only, decided with the product owner 2026-09-16. `email_sender._build_message` always
sets a text part and only adds HTML when a caller passes one; accepting markup here would send raw
tags to a buyer. The storage is a plain string, so an HTML signature later is a second field rather
than a migration.
"""
from __future__ import annotations

#: RFC 3676's sign-off marker. Mail clients hide or grey everything after it, which is what makes a
#: signature read as a signature rather than as another paragraph of the message.
DELIMITER = "\n-- \n"


def resolve_signature(tenant_settings: dict | None, mailbox: dict | None) -> str:
    """The signature for this mailbox: its own, else the workspace default, else nothing.

    Never raises. `email_settings` is a JSON blob an operator can edit by hand, and a bad shape must
    cost the signature, not the send.
    """
    try:
        own = str((mailbox or {}).get("signature") or "").strip()
        if own:
            return own
        return str((tenant_settings or {}).get("default_signature") or "").strip()
    except Exception:  # noqa: BLE001 - a malformed blob must not stop an email
        return ""


def append_signature(body: str, signature: str) -> str:
    """Add the signature under the body, once.

    Idempotent on purpose: the composer shows the signature and the send path adds it, so without
    this the buyer reads the rep's phone number twice. A rep who typed their own sign-off already
    ends the message with it, and that counts — re-adding it would correct nothing and look
    automated.
    """
    text = body or ""
    sig = (signature or "").strip()
    if not sig:
        return text
    if sig in text:
        return text
    return f"{text.rstrip()}{DELIMITER}{sig}"
