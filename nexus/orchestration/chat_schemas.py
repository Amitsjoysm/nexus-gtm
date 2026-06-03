# nexus/orchestration/chat_schemas.py
"""Wire contracts for the conversational orchestrator. Projections only — no raw ORM rows."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from nexus.models.chat import ChatMessage, ChatSession


class CreateSessionRequest(BaseModel):
    account_id: str | None = None
    parent_session_id: str | None = None
    message: str | None = Field(default=None, max_length=4000)


class PostMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)


class ChatMessageOut(BaseModel):
    id: str
    seq: int
    role: str
    kind: str
    content: str
    data: dict = Field(default_factory=dict)
    created_at: datetime

    @classmethod
    def from_model(cls, m: ChatMessage) -> "ChatMessageOut":
        return cls(id=m.id, seq=m.seq, role=m.role, kind=m.kind, content=m.content,
                   data=m.data or {}, created_at=m.created_at)


class ChatSessionOut(BaseModel):
    id: str
    title: str
    status: str
    target: str | None = None
    account_id: str | None = None
    icp_state: dict = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)
    context_summary: str = ""
    created_at: datetime

    @classmethod
    def from_model(cls, s: ChatSession) -> "ChatSessionOut":
        return cls(id=s.id, title=s.title, status=s.status, target=s.target,
                   account_id=s.account_id, icp_state=s.icp_state or {},
                   missing_slots=list(s.missing_slots or []),
                   context_summary=s.context_summary or "", created_at=s.created_at)


class ChatTurnResponse(BaseModel):
    """A session + the messages to render (full list on create, appended on post)."""
    session: ChatSessionOut
    messages: list[ChatMessageOut] = Field(default_factory=list)


class SaveIcpResponse(BaseModel):
    ok: bool = True
    icp: dict = Field(default_factory=dict)
