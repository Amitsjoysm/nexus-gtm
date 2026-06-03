# nexus/orchestration/chat_service.py
"""ChatService — drives the conversational orchestrator over the durable chat model.

Persists the append-only message log, runs the deterministic :class:`IntakeController` per user
turn, and — when the ICP is complete and the rep says go — launches a read-only ``discover`` run
inline (snappy feedback; discovery never parks at an approval). The control loop's *decisioning* is
pure Python (unit-tested in ``test_intake``); this layer owns persistence, sequencing, and the
run handoff. ICP seeding/saving maps between the conversational slot shape (``geo``,
``company_size``) and the saved ``RelevanceProfile.icp`` shape (``countries``, ``employee_min/max``).
"""
from __future__ import annotations

from sqlalchemy import func, select

from nexus.core.config import get_settings
from nexus.core.tenancy import TenantSession
from nexus.models.chat import (
    KIND_RUN_LAUNCHED,
    ChatMessage,
    ChatSession,
)
from nexus.models.relevance import RelevanceProfile
from nexus.orchestration.engine import OrchestrationEngine, get_orchestration_engine
from nexus.orchestration.intake import IntakeController, missing_required
from nexus.relevance.engine import get_or_create_profile, get_profile


def _seed_icp_from_profile(profile: RelevanceProfile | None) -> dict:
    """Map saved profile.icp → conversational slot shape (the inverse of save-icp)."""
    if profile is None or not profile.icp:
        return {}
    icp = profile.icp
    out: dict = {}
    if icp.get("industries"):
        out["industries"] = list(icp["industries"])
    if icp.get("countries"):
        out["geo"] = list(icp["countries"])
    if icp.get("employee_min") is not None or icp.get("employee_max") is not None:
        out["company_size"] = {"min": icp.get("employee_min"), "max": icp.get("employee_max")}
    if icp.get("required_tech"):
        out["required_tech"] = list(icp["required_tech"])
    return out


def _icp_state_to_profile(icp_state: dict) -> dict:
    """Map conversational slots → profile.icp shape for persistence."""
    size = icp_state.get("company_size") or {}
    out: dict = {
        "industries": list(icp_state.get("industries", [])),
        "countries": list(icp_state.get("geo", [])),
        "required_tech": list(icp_state.get("required_tech", [])),
    }
    if size.get("min") is not None:
        out["employee_min"] = size["min"]
    if size.get("max") is not None:
        out["employee_max"] = size["max"]
    return out


class ChatService:
    def __init__(
        self,
        controller: IntakeController | None = None,
        engine: OrchestrationEngine | None = None,
    ) -> None:
        self.controller = controller or IntakeController()
        self.engine = engine or get_orchestration_engine()

    async def create_session(
        self,
        ts: TenantSession,
        *,
        created_by: str | None,
        account_id: str | None = None,
        parent_session_id: str | None = None,
        message: str | None = None,
    ) -> tuple[ChatSession, list[ChatMessage]]:
        icp_state: dict = {}
        summary = ""
        if parent_session_id:
            parent = await ts.get(ChatSession, parent_session_id)
            if parent is not None:
                icp_state = dict(parent.icp_state or {})
                summary = parent.context_summary or ""
        else:
            icp_state = _seed_icp_from_profile(await get_profile(ts))

        session = ChatSession(
            tenant_id=ts.tenant_id,
            created_by=created_by,
            account_id=account_id,
            parent_session_id=parent_session_id,
            icp_state=icp_state,
            missing_slots=missing_required(icp_state, None),
            context_summary=summary,
        )
        ts.add(session)
        await ts.flush()

        if message:
            await self.post_message(ts, session, message, created_by=created_by)
        return session, await self.get_messages(ts, session.id)

    async def list_sessions(
        self, ts: TenantSession, *, account_id: str | None = None, status: str | None = None
    ) -> list[ChatSession]:
        where = []
        if account_id:
            where.append(ChatSession.account_id == account_id)
        if status:
            where.append(ChatSession.status == status)
        stmt = ts.select(ChatSession, *where).order_by(ChatSession.created_at.desc()).limit(100)
        return list((await ts.session.scalars(stmt)).all())

    async def get_messages(self, ts: TenantSession, session_id: str) -> list[ChatMessage]:
        stmt = ts.select(ChatMessage, ChatMessage.session_id == session_id).order_by(
            ChatMessage.seq
        )
        return list((await ts.session.scalars(stmt)).all())

    async def post_message(
        self, ts: TenantSession, session: ChatSession, content: str, *, created_by: str | None
    ) -> list[ChatMessage]:
        """Append the user message, run one control-loop turn, append assistant message(s)."""
        seq = await self._next_seq(ts, session.id)
        ts.add(ChatMessage(tenant_id=ts.tenant_id, session_id=session.id, seq=seq,
                           role="user", kind="text", content=content))
        await ts.flush()
        if not session.title or session.title == "New conversation":
            session.title = content[:160]

        decision = await self.controller.advance(
            icp_state=session.icp_state or {},
            target=session.target,
            missing_slots=session.missing_slots or [],
            context_summary=session.context_summary or "",
            user_text=content,
            is_first_turn=(seq == 1),
        )
        session.icp_state = decision.icp_state
        session.missing_slots = decision.missing_slots
        session.target = decision.target
        session.context_summary = decision.summary
        await ts.flush()

        appended: list[ChatMessage] = []
        if decision.action == "launch":
            run = await self.engine.create_run(
                ts,
                "discover",
                goal_input={
                    "target": decision.target,
                    "icp": decision.icp_state,
                    "max_candidates": get_settings().discovery_max_candidates,
                    "chat_session_id": session.id,
                },
                account_id=session.account_id,
                created_by=created_by,
            )
            run.chat_session_id = session.id
            await ts.flush()
            await self.engine.execute_run(ts, run)
            appended.append(
                await self._assistant(ts, session, KIND_RUN_LAUNCHED, decision.assistant_text,
                                      {"run_id": run.id, "goal": "discover"})
            )
        else:
            appended.append(
                await self._assistant(ts, session, decision.assistant_kind,
                                      decision.assistant_text, decision.data)
            )
        return appended

    async def save_icp(self, ts: TenantSession, session: ChatSession) -> RelevanceProfile:
        profile = await get_or_create_profile(ts)
        profile.icp = _icp_state_to_profile(session.icp_state or {})
        await ts.flush()
        return profile

    async def _assistant(
        self, ts: TenantSession, session: ChatSession, kind: str, content: str, data: dict
    ) -> ChatMessage:
        seq = await self._next_seq(ts, session.id)
        msg = ChatMessage(tenant_id=ts.tenant_id, session_id=session.id, seq=seq,
                          role="assistant", kind=kind, content=content, data=data)
        ts.add(msg)
        await ts.flush()
        return msg

    async def _next_seq(self, ts: TenantSession, session_id: str) -> int:
        cur = await ts.session.scalar(
            select(func.max(ChatMessage.seq)).where(
                ChatMessage.session_id == session_id,
                ChatMessage.tenant_id == ts.tenant_id,
            )
        )
        return int(cur or 0) + 1
