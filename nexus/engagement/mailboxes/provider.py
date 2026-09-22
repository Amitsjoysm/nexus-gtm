"""The contract every mailbox provider implements (spec §3), and the errors it may raise.

A provider instance is bound to ONE connected mailbox and one live access token; the caller gets it
from ``registry.provider_for`` after ``tokens.fresh_access_token``. Methods take and return plain
values, so the engine above never sees Gmail's or Graph's shapes.

There are exactly two implementations, ``GmailProvider`` and ``GraphProvider``, and deliberately no
third, fake one (D21): the engine's decisions are pure functions tested offline, and these adapters
are exercised against real mailboxes in ``tests_live/engagement``.

The error classes are the vocabulary the sending and sync code branch on, so they carry what the
caller needs to decide, not what the provider said:

* ``AuthExpired`` — the refresh token is revoked or expired. The mailbox needs the SDR to reconnect;
  retrying cannot help.
* ``ProviderLimit`` — a quota or rate limit. ``retry_at`` is when to try again (spec §5: the mailbox
  pauses until then; nothing is lost or duplicated).
* ``CursorExpired`` — the stored sync cursor is too old; resynchronise from a recent window.
* ``NotFound`` — the message or thread no longer exists (deleted by the SDR).
* ``TransientError`` — a 5xx or a network failure. Safe to retry the SAME operation later; for a
  send, reconcile first (spec §5 idempotency).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class ProviderError(RuntimeError):
    """Base class. ``detail`` is the provider's own words, trimmed; never a token."""

    def __init__(self, detail: str = "", *, status: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status = status


class AuthExpired(ProviderError):
    pass


class ProviderLimit(ProviderError):
    def __init__(self, detail: str = "", *, status: int | None = None,
                 retry_at: datetime | None = None):
        super().__init__(detail, status=status)
        self.retry_at = retry_at


class CursorExpired(ProviderError):
    pass


class NotFound(ProviderError):
    pass


class TransientError(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class MailboxProfile:
    email: str
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class ThreadRef:
    """Where a reply or follow-up goes (D16): the provider's thread, and the message it answers."""

    provider_thread_id: str
    #: The latest message in the conversation, by the provider's id (Graph needs it for the
    #: conversation index) and by its RFC Message-ID (In-Reply-To).
    reply_to_provider_message_id: str = ""
    in_reply_to: str = ""
    references: str = ""


@dataclass(frozen=True, slots=True)
class SentRef:
    provider_message_id: str
    provider_thread_id: str
    rfc_message_id: str = ""


@dataclass(frozen=True, slots=True)
class InboundMessage:
    provider_message_id: str
    provider_thread_id: str
    raw: bytes
    received_at: datetime
    #: True when the mailbox itself sent it (Gmail SENT label; Graph from == mailbox address).
    outgoing: bool = False


@dataclass(frozen=True, slots=True)
class ChangeBatch:
    """Provider message ids that appeared since ``cursor``, oldest first, and the next cursor."""

    message_ids: list[str] = field(default_factory=list)
    next_cursor: str = ""


class MailProvider(Protocol):
    provider: str

    async def profile(self) -> MailboxProfile: ...

    async def send(self, mime: bytes, *, thread: ThreadRef | None) -> SentRef: ...

    async def create_draft(self, mime: bytes, *, thread: ThreadRef | None) -> str: ...

    async def fetch_changes(self, cursor: str | None) -> ChangeBatch: ...

    async def get_message(self, provider_message_id: str) -> InboundMessage: ...

    async def find_sent(
        self, *, ref_header: str, to: str, around: datetime
    ) -> SentRef | None: ...

    async def search_sent(
        self, *, to: str, subject: str, around: datetime
    ) -> SentRef | None: ...
