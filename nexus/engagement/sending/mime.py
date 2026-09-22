"""The MIME message we hand to Gmail or Graph (spec §5).

Both providers are sent **raw MIME** rather than their JSON send APIs, for one reason: the
unsubscribe headers. Graph's `sendMail` JSON cannot set `List-Unsubscribe`, and RFC 8058 one-click
is what keeps bulk outreach out of spam folders and gives a recipient a way out that is not a reply
(D11). Plain text only, matching the existing composer (D9).

Five headers carry the whole threading and compliance story:

* ``Message-ID`` — ours, generated and stored BEFORE the send, so a reply's ``In-Reply-To`` can be
  matched to the message it answers even if the provider call times out.
* ``In-Reply-To`` / ``References`` — the latest message in the conversation and the whole chain, so
  every mail client shows one thread (D16).
* ``List-Unsubscribe`` / ``List-Unsubscribe-Post`` — the signed one-click link and a mailto.
* ``X-Nexus-Ref`` — a ULID used only to find a message in Sent when we are not sure it was sent.
"""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import formatdate, make_msgid

MAX_REFERENCES = 20


def new_rfc_message_id(domain: str = "") -> str:
    """A globally unique ``Message-ID``. The domain is cosmetic; uniqueness comes from the UUID."""
    return make_msgid(domain=(domain or "").strip() or None)


def references_for(parent_references: str, parent_message_id: str) -> str:
    """The References chain for a reply: the parent's chain plus the parent itself.

    Trimmed to the most recent ``MAX_REFERENCES``, keeping the FIRST id: clients use the head to
    identify the thread's root, and an unbounded chain is a header that grows with every follow-up.
    """
    ids = [part for part in (parent_references or "").split() if part]
    if parent_message_id and parent_message_id not in ids:
        ids.append(parent_message_id)
    if len(ids) > MAX_REFERENCES:
        ids = ids[:1] + ids[-(MAX_REFERENCES - 1):]
    return " ".join(ids)


def opt_out_line(unsubscribe_url: str) -> str:
    """One plain line under the signature (D11). A reply of "no" is offered first, because for a
    real prospect that is the outcome an SDR wants to hear about."""
    return f'Not relevant? Just reply "no", or unsubscribe: {unsubscribe_url}'


def compose_body(body: str, signature: str, unsubscribe_url: str) -> str:
    """Body → signature → opt-out line, each separated by a blank line.

    Idempotent about the opt-out line: `outreach/signature.append_signature` already tolerates a rep
    who typed their own sign-off, and a draft that was shown with its footer must not gain a second
    one when it is sent."""
    text = (body or "").rstrip()
    signature = (signature or "").strip()
    if signature and signature not in text:
        text = f"{text}\n\n{signature}"
    line = opt_out_line(unsubscribe_url)
    if unsubscribe_url and unsubscribe_url not in text:
        text = f"{text}\n\n{line}"
    return text + "\n"


def build_message(
    *,
    from_addr: str,
    from_name: str = "",
    to_addr: str,
    subject: str,
    body: str,
    message_id: str,
    ref: str,
    unsubscribe_url: str = "",
    unsubscribe_mailto: str = "",
    signature: str = "",
    in_reply_to: str = "",
    references: str = "",
    cc: list[str] | None = None,
) -> EmailMessage:
    """The message, ready to serialise. Pure: same inputs, same bytes."""
    message = EmailMessage()
    message["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    message["To"] = to_addr
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = formatdate(localtime=True)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    if unsubscribe_url:
        targets = [f"<{unsubscribe_url}>"]
        if unsubscribe_mailto:
            targets.append(f"<mailto:{unsubscribe_mailto}>")
        message["List-Unsubscribe"] = ", ".join(targets)
        # RFC 8058: without this header a scanner that follows the link cannot unsubscribe anyone,
        # and with it the client shows its own Unsubscribe button instead of a spam report.
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message["X-Nexus-Ref"] = ref
    message.set_content(compose_body(body, signature, unsubscribe_url))
    return message


def to_bytes(message: EmailMessage) -> bytes:
    return message.as_bytes()


def to_text(message: EmailMessage) -> str:
    return message.as_string()
