"""GmailProvider: one connected Gmail mailbox through the Gmail REST API v1.

Scopes (spec §12): ``gmail.readonly`` for history, messages and watch; ``gmail.compose`` for
``messages.send`` and drafts. Both are restricted scopes (Google verification + CASA).

Four behaviours worth knowing:

* **The Message-ID is read back after sending.** Gmail may rewrite the Message-ID of a message sent
  through the API; the id stored for threading and reply matching is the one Gmail actually sent,
  read from the sent message's headers.
* **Sent and draft messages are not "changes".** ``history.list`` reports our own sends as
  ``messageAdded`` with the SENT label; reply detection wants what arrived, so SENT, DRAFT and CHAT
  are skipped.
* **The first sync starts now.** With no cursor, the current ``historyId`` becomes the cursor and no
  messages are returned: the mailbox's past is not read, only what arrives after it is connected.
* **A cursor Gmail has forgotten is ``CursorExpired``** (``history.list`` answers 404 once a
  ``historyId`` falls out of its retention); the sync resynchronises from a recent window.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from nexus.engagement.mailboxes import transport
from nexus.engagement.mailboxes.provider import (
    ChangeBatch,
    CursorExpired,
    InboundMessage,
    MailboxProfile,
    NotFound,
    SentRef,
    ThreadRef,
)
from nexus.engagement.subjects import normalize_subject

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_SKIP_LABELS = {"SENT", "DRAFT", "CHAT"}
_MAX_PAGES = 50


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def from_b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


class GmailProvider:
    provider = "google"

    def __init__(self, *, access_token: str, email: str):
        self._token = access_token
        self.email = (email or "").lower()

    async def _get(self, path: str, params=None) -> dict:
        resp = await transport.request("GET", f"{BASE}{path}", token=self._token, params=params)
        return resp.json()

    async def _post(self, path: str, body: dict) -> dict:
        resp = await transport.request("POST", f"{BASE}{path}", token=self._token, json=body)
        return resp.json() if resp.content else {}

    async def _headers(self, message_id: str, names: tuple[str, ...]) -> tuple[dict, dict]:
        params = [("format", "metadata")] + [("metadataHeaders", n) for n in names]
        data = await self._get(f"/messages/{message_id}", params=params)
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in (data.get("payload") or {}).get("headers", [])
        }
        return data, headers

    async def profile(self) -> MailboxProfile:
        data = await self._get("/profile")
        return MailboxProfile(email=str(data.get("emailAddress", "")).lower())

    async def send(self, mime: bytes, *, thread: ThreadRef | None) -> SentRef:
        body: dict = {"raw": b64url(mime)}
        if thread and thread.provider_thread_id:
            body["threadId"] = thread.provider_thread_id
        sent = await self._post("/messages/send", body)
        _data, headers = await self._headers(sent["id"], ("Message-ID",))
        return SentRef(
            provider_message_id=sent["id"], provider_thread_id=sent.get("threadId", ""),
            rfc_message_id=headers.get("message-id", ""),
        )

    async def create_draft(self, mime: bytes, *, thread: ThreadRef | None) -> str:
        message: dict = {"raw": b64url(mime)}
        if thread and thread.provider_thread_id:
            message["threadId"] = thread.provider_thread_id
        draft = await self._post("/drafts", {"message": message})
        return str(draft.get("id", ""))

    async def fetch_changes(self, cursor: str | None) -> ChangeBatch:
        if not cursor:
            profile = await self._get("/profile")
            return ChangeBatch(message_ids=[], next_cursor=str(profile.get("historyId", "")))
        ids: list[str] = []
        seen: set[str] = set()
        latest = cursor
        page = None
        for _ in range(_MAX_PAGES):
            params = {"startHistoryId": cursor, "historyTypes": "messageAdded", "maxResults": 500}
            if page:
                params["pageToken"] = page
            try:
                data = await self._get("/history", params=params)
            except NotFound as exc:
                raise CursorExpired(exc.detail, status=exc.status) from exc
            for entry in data.get("history", []) or []:
                for added in entry.get("messagesAdded", []) or []:
                    message = added.get("message") or {}
                    labels = set(message.get("labelIds") or [])
                    mid = message.get("id")
                    if not mid or mid in seen or labels & _SKIP_LABELS:
                        continue
                    seen.add(mid)
                    ids.append(mid)
            latest = str(data.get("historyId") or latest)
            page = data.get("nextPageToken")
            if not page:
                break
        return ChangeBatch(message_ids=ids, next_cursor=latest)

    async def watch(self, *, topic: str, **_ignored) -> tuple[str, datetime]:
        """``users.watch`` on the inbox: Gmail publishes each change to ``topic`` for seven days.
        Returns ``("", expires_at)`` — a Gmail watch has no id; re-watching replaces it."""
        data = await self._post("/watch", {"topicName": topic, "labelIds": ["INBOX"],
                                           "labelFilterBehavior": "include"})
        expires_ms = int(data.get("expiration") or 0)
        return "", datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc)

    async def resync(self, since: datetime) -> ChangeBatch:
        """Messages received after ``since`` (bounded), and a fresh cursor. Used after
        ``CursorExpired``; duplicates are harmless because messages are unique per mailbox."""
        profile = await self._get("/profile")
        query = f"after:{_epoch(since)} -in:sent -in:drafts -in:chats"
        data = await self._get("/messages", params={"q": query, "maxResults": 200})
        ids = [m["id"] for m in reversed(data.get("messages", []) or []) if m.get("id")]
        return ChangeBatch(message_ids=ids, next_cursor=str(profile.get("historyId", "")))

    async def get_message(self, provider_message_id: str) -> InboundMessage:
        data = await self._get(f"/messages/{provider_message_id}", params={"format": "raw"})
        received = datetime.fromtimestamp(int(data.get("internalDate", "0")) / 1000, tz=timezone.utc)
        return InboundMessage(
            provider_message_id=data["id"], provider_thread_id=data.get("threadId", ""),
            raw=from_b64url(data.get("raw", "")), received_at=received,
            outgoing="SENT" in set(data.get("labelIds") or []),
        )

    async def _sent_candidates(self, *, to: str, start: datetime, end: datetime) -> list[dict]:
        query = f"in:sent to:{to} after:{_epoch(start)} before:{_epoch(end)}"
        data = await self._get("/messages", params={"q": query, "maxResults": 25})
        return data.get("messages", []) or []

    async def find_sent(self, *, ref_header: str, to: str, around: datetime) -> SentRef | None:
        start, end = around - timedelta(days=2), around + timedelta(days=1)
        for candidate in await self._sent_candidates(to=to, start=start, end=end):
            _data, headers = await self._headers(candidate["id"], ("X-Nexus-Ref", "Message-ID"))
            if headers.get("x-nexus-ref", "").strip() == ref_header:
                return SentRef(candidate["id"], candidate.get("threadId", ""),
                               headers.get("message-id", ""))
        return None

    async def search_sent(self, *, to: str, subject: str, around: datetime) -> SentRef | None:
        wanted = normalize_subject(subject).lower()
        best: tuple[float, SentRef] | None = None
        start, end = around - timedelta(days=3), around + timedelta(days=3)
        for candidate in await self._sent_candidates(to=to, start=start, end=end):
            data, headers = await self._headers(candidate["id"], ("Subject", "Message-ID"))
            if normalize_subject(headers.get("subject", "")).lower() != wanted:
                continue
            sent_at = datetime.fromtimestamp(int(data.get("internalDate", "0")) / 1000,
                                             tz=timezone.utc)
            distance = abs((sent_at - around).total_seconds())
            ref = SentRef(candidate["id"], candidate.get("threadId", ""),
                          headers.get("message-id", ""))
            if best is None or distance < best[0]:
                best = (distance, ref)
        return best[1] if best else None
