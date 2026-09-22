"""GraphProvider: one connected Microsoft 365 / Outlook mailbox through Microsoft Graph v1.0.

Scopes (spec §12): ``Mail.ReadWrite`` (read replies, create drafts), ``Mail.Send``, ``User.Read``,
``offline_access``.

**Why messages are imported as MIME rather than built with Graph's JSON.** The unsubscribe headers
(``List-Unsubscribe``, ``List-Unsubscribe-Post``, D11) are not ``X-`` headers, and Graph's JSON API
only accepts custom ``X-`` headers. So every send creates a draft from the complete RFC 5322 message
(``POST /me/messages`` with a base64 MIME body) and then sends that draft. Creating it first also
gives us the message id, conversation id and Message-ID, which ``sendMail`` never returns.

**Immutable ids.** Every call carries ``Prefer: IdType="ImmutableId"``: a draft moves to Sent Items
when it is sent, and a regular Graph id changes when a message changes folder. The immutable id the
draft had is the id the sent message keeps.

**Staying in the same conversation (D16).** References/In-Reply-To keep the RECIPIENT's thread. The
SDR's own mailbox groups a conversation by its conversation index, so a follow-up copies the parent's
``Thread-Index`` and appends a child block (MS-OXOMSG 2.2.1.3); without it Exchange starts a new
conversation. ``tests_live/engagement/test_mailboxes_live.py`` asserts the conversation id holds.

**Changes are a timestamp window**, not a delta token: ``receivedDateTime ge <cursor>`` across all
folders, excluding drafts and our own sends. It survives folder moves and rules, never expires, and
the boundary duplicate it returns is rejected by the unique message index.
"""
from __future__ import annotations

import base64
import os
import uuid
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser

from nexus.engagement.mailboxes import transport
from nexus.engagement.mailboxes.provider import (
    ChangeBatch,
    InboundMessage,
    MailboxProfile,
    SentRef,
    ThreadRef,
)
from nexus.engagement.subjects import normalize_subject

GRAPH = "https://graph.microsoft.com/v1.0"
IMMUTABLE = {"Prefer": 'IdType="ImmutableId"'}
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_MAX_PAGES = 50


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filetime(moment: datetime) -> int:
    return int((moment - _FILETIME_EPOCH).total_seconds() * 10_000_000)


def new_thread_index(now: datetime) -> str:
    """A conversation header block: 6 bytes of FILETIME (whose first byte is the reserved 0x01 for
    any date this century) followed by a random 16-byte GUID."""
    header = _filetime(now).to_bytes(8, "big")[:6] + uuid.uuid4().bytes
    return base64.b64encode(header).decode("ascii")


def child_thread_index(parent: str | None, now: datetime) -> str:
    """The parent's index plus one 5-byte child block. A missing or malformed parent starts a new
    conversation rather than raising."""
    try:
        raw = base64.b64decode(parent or "", validate=True)
    except (ValueError, TypeError):
        raw = b""
    if len(raw) < 22 or (len(raw) - 22) % 5:
        return new_thread_index(now)
    header_time = int.from_bytes(raw[:6] + b"\x00\x00", "big")
    delta = max(0, _filetime(now) - header_time)
    if delta < (1 << 49):
        block = (delta >> 18) & 0x7FFFFFFF
    else:
        block = (1 << 31) | ((delta >> 23) & 0x7FFFFFFF)
    child = block.to_bytes(4, "big") + os.urandom(1)
    return base64.b64encode(raw + child).decode("ascii")


def with_header(mime: bytes, name: str, value: str) -> bytes:
    message = BytesParser(policy=policy.SMTP).parsebytes(mime)
    del message[name]
    message[name] = value
    return message.as_bytes(policy=policy.SMTP)


class GraphProvider:
    provider = "microsoft"

    def __init__(self, *, access_token: str, email: str):
        self._token = access_token
        self.email = (email or "").lower()

    async def _get(self, url: str, params: dict | None = None) -> dict:
        full = url if url.startswith("https://") else f"{GRAPH}{url}"
        resp = await transport.request("GET", full, token=self._token, params=params,
                                       headers=IMMUTABLE)
        return resp.json()

    async def profile(self) -> MailboxProfile:
        data = await self._get("/me", {"$select": "mail,userPrincipalName,displayName"})
        email = data.get("mail") or data.get("userPrincipalName") or ""
        return MailboxProfile(email=str(email).lower(), display_name=data.get("displayName") or "")

    async def _thread_index_of(self, message_id: str) -> str | None:
        data = await self._get(f"/me/messages/{message_id}", {"$select": "internetMessageHeaders"})
        for header in data.get("internetMessageHeaders") or []:
            if str(header.get("name", "")).lower() == "thread-index":
                return header.get("value")
        return None

    async def _import(self, mime: bytes, thread: ThreadRef | None) -> dict:
        if thread and thread.reply_to_provider_message_id:
            parent = await self._thread_index_of(thread.reply_to_provider_message_id)
            mime = with_header(mime, "Thread-Index",
                               child_thread_index(parent, datetime.now(timezone.utc)))
        resp = await transport.request(
            "POST", f"{GRAPH}/me/messages", token=self._token,
            content=base64.b64encode(mime), headers={**IMMUTABLE, "Content-Type": "text/plain"},
        )
        return resp.json()

    async def send(self, mime: bytes, *, thread: ThreadRef | None) -> SentRef:
        draft = await self._import(mime, thread)
        await transport.request("POST", f"{GRAPH}/me/messages/{draft['id']}/send",
                                token=self._token, headers=IMMUTABLE)
        return SentRef(
            provider_message_id=draft["id"], provider_thread_id=draft.get("conversationId", ""),
            rfc_message_id=draft.get("internetMessageId", ""),
        )

    async def create_draft(self, mime: bytes, *, thread: ThreadRef | None) -> str:
        draft = await self._import(mime, thread)
        return str(draft.get("id", ""))

    async def _window(self, since: str) -> ChangeBatch:
        ids: list[str] = []
        latest = since
        url: str | None = f"{GRAPH}/me/messages"
        params: dict | None = {
            "$filter": f"receivedDateTime ge {since} and isDraft eq false",
            "$orderby": "receivedDateTime asc",
            "$select": "id,receivedDateTime,from",
            "$top": "50",
        }
        for _ in range(_MAX_PAGES):
            if url is None:
                break
            data = await self._get(url, params)
            for message in data.get("value", []) or []:
                sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address", "")
                latest = message.get("receivedDateTime") or latest
                if str(sender).lower() == self.email:
                    continue
                ids.append(message["id"])
            url, params = data.get("@odata.nextLink"), None
        return ChangeBatch(message_ids=ids, next_cursor=latest)

    async def fetch_changes(self, cursor: str | None) -> ChangeBatch:
        if not cursor:
            return ChangeBatch(message_ids=[], next_cursor=_iso(datetime.now(timezone.utc)))
        return await self._window(cursor)

    async def resync(self, since: datetime) -> ChangeBatch:
        return await self._window(_iso(since))

    async def get_message(self, provider_message_id: str) -> InboundMessage:
        meta = await self._get(f"/me/messages/{provider_message_id}",
                               {"$select": "id,conversationId,receivedDateTime,from"})
        raw = await transport.request("GET", f"{GRAPH}/me/messages/{provider_message_id}/$value",
                                      token=self._token, headers=IMMUTABLE)
        sender = ((meta.get("from") or {}).get("emailAddress") or {}).get("address", "")
        received = datetime.fromisoformat(
            str(meta.get("receivedDateTime", "1970-01-01T00:00:00Z")).replace("Z", "+00:00")
        )
        return InboundMessage(
            provider_message_id=meta["id"], provider_thread_id=meta.get("conversationId", ""),
            raw=raw.content, received_at=received, outgoing=str(sender).lower() == self.email,
        )

    async def _sent_window(self, start: datetime, end: datetime, select: str) -> list[dict]:
        data = await self._get(
            f"{GRAPH}/me/mailFolders/sentitems/messages",
            {"$filter": f"sentDateTime ge {_iso(start)} and sentDateTime le {_iso(end)}",
             "$select": select, "$top": "50"},
        )
        return data.get("value", []) or []

    @staticmethod
    def _addressed_to(message: dict, to: str) -> bool:
        wanted = (to or "").lower()
        return any(
            ((r.get("emailAddress") or {}).get("address", "")).lower() == wanted
            for r in message.get("toRecipients") or []
        )

    async def find_sent(self, *, ref_header: str, to: str, around: datetime) -> SentRef | None:
        select = "id,conversationId,internetMessageId,internetMessageHeaders,toRecipients"
        for message in await self._sent_window(around - timedelta(days=2),
                                               around + timedelta(days=1), select):
            if not self._addressed_to(message, to):
                continue
            for header in message.get("internetMessageHeaders") or []:
                if (str(header.get("name", "")).lower() == "x-nexus-ref"
                        and str(header.get("value", "")).strip() == ref_header):
                    return SentRef(message["id"], message.get("conversationId", ""),
                                   message.get("internetMessageId", ""))
        return None

    async def search_sent(self, *, to: str, subject: str, around: datetime) -> SentRef | None:
        wanted = normalize_subject(subject).lower()
        select = "id,conversationId,internetMessageId,subject,toRecipients,sentDateTime"
        best: tuple[float, SentRef] | None = None
        for message in await self._sent_window(around - timedelta(days=3),
                                               around + timedelta(days=3), select):
            if not self._addressed_to(message, to):
                continue
            if normalize_subject(message.get("subject", "")).lower() != wanted:
                continue
            sent_at = datetime.fromisoformat(str(message["sentDateTime"]).replace("Z", "+00:00"))
            distance = abs((sent_at - around).total_seconds())
            ref = SentRef(message["id"], message.get("conversationId", ""),
                          message.get("internetMessageId", ""))
            if best is None or distance < best[0]:
                best = (distance, ref)
        return best[1] if best else None
