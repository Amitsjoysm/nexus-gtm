# tests/test_chat_models.py
"""Chat + custom-field models persist under tenant scoping with a monotonic seq."""
from __future__ import annotations

import pytest

from nexus.models.chat import ChatMessage, ChatSession, CustomFieldDef
from tests.conftest import make_tenant, tenant_session


async def test_chat_session_and_messages_persist():
    tid = await make_tenant("t-chat")
    async with tenant_session(tid) as ts:
        session = ChatSession(tenant_id=tid, title="Find fintech", target="companies",
                              icp_state={"industries": ["fintech"]})
        ts.add(session)
        await ts.flush()
        ts.add(ChatMessage(tenant_id=tid, session_id=session.id, seq=1, role="user",
                           kind="text", content="find fintech in the US"))
        ts.add(ChatMessage(tenant_id=tid, session_id=session.id, seq=2, role="assistant",
                           kind="clarifying_question", content="What size?",
                           data={"slot": "company_size"}))
        await ts.flush()
        rows = await ts.list(ChatMessage, ChatMessage.session_id == session.id)
        assert sorted(m.seq for m in rows) == [1, 2]
        assert session.status == "active"
        assert session.icp_state == {"industries": ["fintech"]}


async def test_chat_message_seq_unique_per_session():
    tid = await make_tenant("t-seq")
    async with tenant_session(tid) as ts:
        session = ChatSession(tenant_id=tid, title="s")
        ts.add(session)
        await ts.flush()
        ts.add(ChatMessage(tenant_id=tid, session_id=session.id, seq=1, role="user", content="a"))
        await ts.flush()
        # Contain the deliberate IntegrityError in a savepoint so the outer transaction
        # (which tenant_session commits on exit) stays usable.
        with pytest.raises(Exception):
            async with ts.session.begin_nested():
                ts.add(ChatMessage(tenant_id=tid, session_id=session.id, seq=1, role="user", content="b"))
                await ts.session.flush()


async def test_custom_field_def_unique_key():
    tid = await make_tenant("t-cf")
    async with tenant_session(tid) as ts:
        ts.add(CustomFieldDef(tenant_id=tid, entity="account", key="arr", label="ARR", kind="number"))
        await ts.flush()
        with pytest.raises(Exception):
            async with ts.session.begin_nested():
                ts.add(CustomFieldDef(tenant_id=tid, entity="account", key="arr", label="ARR2", kind="number"))
                await ts.session.flush()
