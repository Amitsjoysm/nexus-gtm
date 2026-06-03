# nexus/api/routers/chat.py
"""Conversational orchestrator HTTP surface (`/api/orchestration/chat`).

Sessions persist per workspace and are scoped to an optional account/"client". A turn appends the
user message, runs one deterministic control-loop step, and appends the assistant reply (a
clarifying question, a confirmation, or a ``run_launched`` pointer to a discovery run). The SSE
endpoint replays + follows the session's append-only message log; the linked run streams its own
progress from ``/api/orchestration/runs/{id}/events``.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from nexus.api.deps import Principal, get_principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.chat import ChatMessage, ChatSession
from nexus.orchestration.chat_schemas import (
    ChatMessageOut,
    ChatSessionOut,
    ChatTurnResponse,
    CreateSessionRequest,
    PostMessageRequest,
    SaveIcpResponse,
)
from nexus.orchestration.chat_service import ChatService
from nexus.workers.tasks import tenant_session

router = APIRouter(prefix="/orchestration/chat", tags=["chat"])


def _turn(session: ChatSession, messages: list[ChatMessage]) -> ChatTurnResponse:
    return ChatTurnResponse(
        session=ChatSessionOut.from_model(session),
        messages=[ChatMessageOut.from_model(m) for m in messages],
    )


@router.post("/sessions", response_model=ChatTurnResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_orchestration)),
) -> ChatTurnResponse:
    session, messages = await ChatService().create_session(
        ts,
        created_by=principal.user_id,
        account_id=body.account_id,
        parent_session_id=body.parent_session_id,
        message=body.message,
    )
    return _turn(session, messages)


@router.get("/sessions", response_model=list[ChatSessionOut])
async def list_sessions(
    account_id: str | None = None,
    status_filter: str | None = None,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(get_principal),
) -> list[ChatSessionOut]:
    sessions = await ChatService().list_sessions(ts, account_id=account_id, status=status_filter)
    return [ChatSessionOut.from_model(s) for s in sessions]


@router.get("/sessions/{session_id}", response_model=ChatTurnResponse)
async def get_session(
    session_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(get_principal),
) -> ChatTurnResponse:
    session = await ts.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    messages = await ChatService().get_messages(ts, session_id)
    return _turn(session, messages)


@router.post("/sessions/{session_id}/messages", response_model=ChatTurnResponse)
async def post_message(
    session_id: str,
    body: PostMessageRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_orchestration)),
) -> ChatTurnResponse:
    session = await ts.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    appended = await ChatService().post_message(
        ts, session, body.content, created_by=principal.user_id
    )
    return _turn(session, appended)


@router.post("/sessions/{session_id}/save-icp", response_model=SaveIcpResponse)
async def save_icp(
    session_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> SaveIcpResponse:
    session = await ts.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    profile = await ChatService().save_icp(ts, session)
    return SaveIcpResponse(ok=True, icp=profile.icp or {})


def _format_sse(seq: int, type_: str, data: dict) -> str:
    return f"id: {seq}\nevent: {type_}\ndata: {json.dumps(data)}\n\n"


async def _message_stream(
    tenant_id: str, session_id: str, last_seq: int, request: Request
) -> AsyncIterator[str]:
    """Replay-then-follow the session's append-only message log as SSE (keyed on ``seq``)."""
    idle = 0
    for _ in range(120):  # ~60s ceiling
        if await request.is_disconnected():
            return
        async with tenant_session(tenant_id) as ts:
            session = await ts.get(ChatSession, session_id)
            if session is None:
                yield _format_sse(last_seq, "error", {"detail": "session not found"})
                return
            stmt = ts.select(
                ChatMessage,
                ChatMessage.session_id == session_id,
                ChatMessage.seq > last_seq,
            ).order_by(ChatMessage.seq)
            msgs = list((await ts.session.scalars(stmt)).all())
            for m in msgs:
                yield _format_sse(
                    m.seq, m.kind,
                    {"id": m.id, "role": m.role, "kind": m.kind,
                     "content": m.content, "data": m.data or {}},
                )
                last_seq = m.seq
        idle = idle + 1 if not msgs else 0
        if idle >= 4:  # ~2s quiet → close; client resumes with Last-Event-ID
            yield ": idle\n\n"
            return
        await asyncio.sleep(0.5)


@router.get("/sessions/{session_id}/stream")
async def stream_session(
    session_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: Principal = Depends(get_principal),
) -> StreamingResponse:
    try:
        last_seq = int(last_event_id) if last_event_id else 0
    except ValueError:
        last_seq = 0
    return StreamingResponse(
        _message_stream(principal.tenant_id, session_id, last_seq, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
