"""Read an inbound email: who, what it answers, what it says, and whether a person wrote it (§6).

Pure: raw RFC 5322 bytes in, a `Parsed` out. The decisions that must not be left to a model live
here, because they are cheap, reliable and checkable:

* **Bounces** — a delivery status notification (``multipart/report; report-type=delivery-status``)
  or a mailer-daemon/postmaster sender. The ids of OUR messages it reports come from the embedded
  original (``message/rfc822`` or ``text/rfc822-headers``), so a bounce is tied to the exact email
  that failed, not guessed from a subject line.
* **Auto-replies** — ``Auto-Submitted`` other than ``no``, ``X-Autoreply``, ``X-Autorespond``,
  ``Precedence: auto_reply|bulk|junk``, and the subject prefixes every major client puts on an
  out-of-office. The AI never decides whether a human wrote a message.
* **The new text** — the reply with the quoted history cut off, which is what classification
  reads. The whole body is kept for the conversation timeline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as _modern
from email.utils import getaddresses, parsedate_to_datetime

_AUTO_PRECEDENCE = {"auto_reply", "bulk", "junk"}
_OOO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto(?:matic)?[- ]?reply|out of (?:the )?office|ooo\b|abwesenheit|"
    r"absence|réponse automatique|respuesta automática|risposta automatica)", re.IGNORECASE)
_MAILER = re.compile(r"^(mailer-daemon|postmaster)@", re.IGNORECASE)
_MESSAGE_ID = re.compile(r"<[^<>\s]+>")
#: Where quoted history begins, in the forms the common clients write it.
_QUOTE_MARKERS = (
    re.compile(r"^\s*On .{3,200}wrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*Am .{3,200}schrieb .{0,100}:\s*$", re.IGNORECASE),
    re.compile(r"^\s*Le .{3,200}a écrit\s*:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*_{5,}\s*$"),
    re.compile(r"^\s*From:\s.+$", re.IGNORECASE),
)


@dataclass(slots=True)
class Parsed:
    message_id: str = ""
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    from_addr: str = ""
    from_name: str = ""
    to_addrs: list[str] = field(default_factory=list)
    cc_addrs: list[str] = field(default_factory=list)
    subject: str = ""
    date: datetime | None = None
    text: str = ""          # the new part of the message, quoted history removed
    full_text: str = ""     # everything, for the timeline
    is_auto: bool = False
    auto_reason: str = ""
    is_bounce: bool = False
    bounced_message_ids: list[str] = field(default_factory=list)
    bounced_recipients: list[str] = field(default_factory=list)

    @property
    def replied_to_ids(self) -> list[str]:
        """Every id this message says it answers, nearest first."""
        ids = [self.in_reply_to] if self.in_reply_to else []
        return ids + [r for r in reversed(self.references) if r not in ids]


def _addresses(message, header: str) -> list[str]:
    return [addr.strip().lower() for _name, addr in getaddresses(message.get_all(header, []))
            if addr and "@" in addr]


def _body_text(message) -> str:
    """The plain-text body, else the HTML body reduced to text. Never raises."""
    try:
        part = message.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return content.replace("\r\n", "\n").strip()


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    import html as _html

    text = _html.unescape(text)
    return "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()


def strip_quoted(text: str) -> str:
    """The reply without the history it quotes. Keeps everything when no marker is found."""
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if index and any(pattern.match(line) for pattern in _QUOTE_MARKERS):
            lines = lines[:index]
            break
    kept = [line for line in lines if not line.lstrip().startswith(">")]
    return "\n".join(kept).strip()


def _auto_reason(message, subject: str) -> str:
    auto_submitted = (message.get("Auto-Submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return f"Auto-Submitted: {auto_submitted}"
    for header in ("X-Autoreply", "X-Autorespond", "X-Auto-Response-Suppress"):
        if message.get(header) and header != "X-Auto-Response-Suppress":
            return header
    precedence = (message.get("Precedence") or "").strip().lower()
    if precedence in _AUTO_PRECEDENCE:
        return f"Precedence: {precedence}"
    if _OOO_SUBJECT.match(subject or ""):
        return "out-of-office subject"
    return ""


def _bounce(message, from_addr: str) -> tuple[bool, list[str], list[str]]:
    content_type = message.get_content_type()
    report = (content_type == "multipart/report"
              and (message.get_param("report-type") or "").lower() == "delivery-status")
    if not (report or _MAILER.match(from_addr or "")):
        return False, [], []
    ids: list[str] = []
    recipients: list[str] = []
    for part in message.walk():
        kind = part.get_content_type()
        if kind == "message/rfc822":
            inner = part.get_payload(0) if part.is_multipart() else None
            if inner is not None and inner.get("Message-ID"):
                ids.append(inner["Message-ID"].strip())
        elif kind == "text/rfc822-headers":
            found = re.search(r"(?im)^Message-ID:\s*(<[^>]+>)", _rendered(part))
            if found:
                ids.append(found.group(1))
        elif kind == "message/delivery-status":
            # Its payload is a list of header blocks, not text: read it as rendered.
            for recipient in re.findall(r"(?im)^(?:Final|Original)-Recipient:\s*[^;]+;\s*(\S+)",
                                        _rendered(part)):
                recipients.append(recipient.strip().strip("<>").lower())
    if not ids:
        # Some mailers inline the original as text: take every Message-ID they quote.
        ids = [m for m in _MESSAGE_ID.findall(_as_text(message))
               if m != (message.get("Message-ID") or "").strip()]
    return True, list(dict.fromkeys(ids)), list(dict.fromkeys(recipients))


def _rendered(part) -> str:
    try:
        return part.as_string()
    except Exception:
        return _as_text(part)


def _as_text(part) -> str:
    try:
        if part.is_multipart():
            return "\n".join(_as_text(p) for p in part.get_payload())
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = part.get_payload()
            return payload if isinstance(payload, str) else str(payload)
        return payload.decode("utf-8", errors="replace")
    except Exception:
        return ""


def parse(raw: bytes) -> Parsed:
    message: EmailMessage = message_from_bytes(raw or b"", policy=_modern)
    from_list = getaddresses(message.get_all("From", []))
    from_name, from_addr = (from_list[0] if from_list else ("", ""))
    subject = str(message.get("Subject") or "").strip()
    try:
        date = parsedate_to_datetime(message["Date"]) if message.get("Date") else None
    except (TypeError, ValueError):
        date = None
    full = _body_text(message)
    is_bounce, bounced, recipients = _bounce(message, (from_addr or "").lower())
    reason = "" if is_bounce else _auto_reason(message, subject)
    return Parsed(
        message_id=(message.get("Message-ID") or "").strip(),
        in_reply_to=(_MESSAGE_ID.findall(message.get("In-Reply-To") or "") or [""])[0],
        references=_MESSAGE_ID.findall(message.get("References") or ""),
        from_addr=(from_addr or "").strip().lower(), from_name=(from_name or "").strip(),
        to_addrs=_addresses(message, "To"), cc_addrs=_addresses(message, "Cc"),
        subject=subject, date=date, text=strip_quoted(full), full_text=full,
        is_auto=bool(reason), auto_reason=reason, is_bounce=is_bounce,
        bounced_message_ids=bounced, bounced_recipients=recipients,
    )
