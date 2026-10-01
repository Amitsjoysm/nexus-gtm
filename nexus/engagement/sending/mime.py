"""The MIME message we hand to Gmail or Graph (spec §5).

Both providers are sent **raw MIME** rather than their JSON send APIs, for one reason: the
unsubscribe headers. Graph's `sendMail` JSON cannot set `List-Unsubscribe`, and RFC 8058 one-click
is what keeps bulk outreach out of spam folders and gives a recipient a way out that is not a reply
(D11).

**How it reads, fixed 2026-09-30** after a real Outlook delivery arrived as "Scaling pro=uct
development ... resource co=straints ... a 15-minute cal= Thursday", signed "Best, Alex" above the
rep's own "Kind Regards, Amit Singh", with a raw unsubscribe URL under it:

* **CRLF on the wire** (``to_bytes`` uses ``policy.SMTP``). The body is quoted-printable, whose soft
  line break is ``=`` + CRLF. Serialised with bare LF, Exchange read ``=`` + LF + the next character
  as a broken escape, kept the ``=`` and dropped that character, once per ~76. Gmail tolerated it;
  Graph's MIME import did not.
* **Plain text plus HTML** (multipart/alternative, text first). The HTML carries the same words as
  paragraphs with no template, so it reads like an email a person wrote, and the opt-out line is
  small grey print with "unsubscribe" as the link. The text part keeps the full URL, which is all a
  text-only client can show.
* **One sign-off, the real name.** The model is told to close with "Best," and a first name it was
  never given, so it invented one; the mailbox signature then added its own closing underneath. A
  signature now replaces the model's closing (keeping the model's "Best," only when the signature
  has none), and with no signature the closing names the sender.
* **No look-alike characters** (``tidy_text``): non-breaking hyphens and spaces, zero-width marks.

Five headers carry the whole threading and compliance story:

* ``Message-ID`` — ours, generated and stored BEFORE the send, so a reply's ``In-Reply-To`` can be
  matched to the message it answers even if the provider call times out.
* ``In-Reply-To`` / ``References`` — the latest message in the conversation and the whole chain, so
  every mail client shows one thread (D16).
* ``List-Unsubscribe`` / ``List-Unsubscribe-Post`` — the signed one-click link and a mailto.
* ``X-Nexus-Ref`` — a ULID used only to find a message in Sent when we are not sure it was sent.
"""
from __future__ import annotations

import html
import re
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from nexus.agents.copy import tidy_text  # noqa: F401  (re-exported for callers)

MAX_REFERENCES = 20

#: A line that is only a closing ("Best,", "Kind regards,"). Checked on whole lines.
_CLOSING = re.compile(
    r"^(best|best regards|best wishes|regards|kind regards|warm regards|warmly|thanks|thank you|"
    r"many thanks|thanks again|cheers|all the best|sincerely|talk soon|speak soon)\s*[,.!]?$",
    re.IGNORECASE)
#: A line that is only a first name or a short full name, as a model signs off.
_NAME_LINE = re.compile(r"^[A-Z][A-Za-z'.\-]*( [A-Z][A-Za-z'.\-]*){0,2}$")
_OPT_OUT = re.compile(r'^Not relevant\? Just reply "no", or unsubscribe:.*$', re.MULTILINE)
_URL = re.compile(r"https?://[^\s<>\"']+")


def split_sign_off(body: str) -> tuple[str, str]:
    """``(body without its closing, the closing line)``; ``(body, "")`` when it has none.

    Recognises a closing line alone, or a closing line followed by one short name line, at the end.
    Anything else is left alone: stripping a real sentence is worse than a doubled sign-off."""
    lines = (body or "").rstrip().split("\n")
    if len(lines) >= 2 and _NAME_LINE.match(lines[-1].strip()) and _CLOSING.match(lines[-2].strip()):
        return "\n".join(lines[:-2]).rstrip(), lines[-2].strip()
    if lines and _CLOSING.match(lines[-1].strip()):
        return "\n".join(lines[:-1]).rstrip(), lines[-1].strip()
    return (body or "").rstrip(), ""


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


def compose_message_text(body: str, signature: str, sender_name: str = "") -> str:
    """Body then signature, with exactly one sign-off, and no opt-out line.

    A signature that opens with its own closing ("Kind Regards,") replaces the model's; one that does
    not keeps the model's closing word ("Best,") above it. Either way the model's name line goes: it
    is a guess, and the signature carries the real name. With no signature, the closing is kept and
    signed with the sender's first name. Idempotent: text that already holds the signature is left
    as it is, so a draft shown in the composer and then sent gains nothing."""
    text = _OPT_OUT.sub("", tidy_text(body or "")).rstrip()
    signature = tidy_text(signature or "").strip()
    if signature:
        if signature in text:
            return text
        rest, closing = split_sign_off(text)
        first_sig_line = signature.split("\n", 1)[0].strip()
        if closing and not _CLOSING.match(first_sig_line):
            return f"{rest}\n\n{closing}\n{signature}"
        return f"{rest}\n\n{signature}"
    first = (sender_name or "").strip().split(" ", 1)[0]
    rest, closing = split_sign_off(text)
    if closing and first:
        return f"{rest}\n\n{closing}\n{first}"
    return text


def compose_body(body: str, signature: str, unsubscribe_url: str, sender_name: str = "") -> str:
    """The plain-text part: body → signature → opt-out line, each separated by a blank line.

    Idempotent about the opt-out line: a draft that was shown with its footer must not gain a second
    one when it is sent."""
    text = compose_message_text(body, signature, sender_name)
    if unsubscribe_url:
        text = f"{text}\n\n{opt_out_line(unsubscribe_url)}"
    return text + "\n"


def compose_html(body: str, signature: str, unsubscribe_url: str, sender_name: str = "") -> str:
    """The HTML part: the same words as paragraphs, then the opt-out line in small grey print.

    No template, no colours, no images: a cold email that looks designed reads as marketing and is
    filtered as marketing. Paragraphs come from blank lines and line breaks from single newlines, so
    the text and HTML parts cannot say different things. Web addresses in the body are linked; the
    unsubscribe address appears only as the target of the word "unsubscribe"."""
    text = compose_message_text(body, signature, sender_name)
    paragraphs = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [_link(html.escape(line)) for line in block.split("\n")]
        paragraphs.append(f'<p style="margin:0 0 1em 0">{"<br>".join(lines)}</p>')
    footer = ""
    if unsubscribe_url:
        href = html.escape(unsubscribe_url, quote=True)
        footer = (
            '<p style="margin:1.5em 0 0 0;font-size:11px;line-height:1.4;color:#6b7280">'
            'Not relevant? Just reply &quot;no&quot;, or '
            f'<a href="{href}" style="color:#6b7280;text-decoration:underline">unsubscribe</a>.</p>'
        )
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
            '<div style="font-family:Calibri,Arial,Helvetica,sans-serif;font-size:14px;'
            f'line-height:1.5;color:#1f2937">{"".join(paragraphs)}{footer}</div></body></html>')


def _link(escaped_line: str) -> str:
    return _URL.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped_line)


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
    # Text first, HTML last: a client shows the LAST alternative it can render (RFC 2046), and a
    # gateway that strips HTML still delivers the text.
    message.set_content(compose_body(body, signature, unsubscribe_url, from_name))
    message.add_alternative(compose_html(body, signature, unsubscribe_url, from_name),
                            subtype="html")
    return message


def to_bytes(message: EmailMessage) -> bytes:
    """The wire form: CRLF line endings, which quoted-printable's soft breaks require (see top)."""
    return message.as_bytes(policy=policy.SMTP)


def to_text(message: EmailMessage) -> str:
    return message.as_string(policy=policy.SMTP)
