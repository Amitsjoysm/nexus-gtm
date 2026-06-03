# tests/test_chat_api.py
"""Chat HTTP surface: sessions, the turn loop, SSE replay, save-icp, RBAC + tenant scoping."""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


@pytest.mark.asyncio
async def test_create_list_get_and_post_turn(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    r = await client.post(
        "/api/orchestration/chat/sessions",
        json={"message": "find fintech companies"},
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    sid = body["session"]["id"]
    assert any(m["kind"] == "clarifying_question" for m in body["messages"])

    r = await client.get("/api/orchestration/chat/sessions", headers=auth(token))
    assert r.status_code == 200
    assert any(s["id"] == sid for s in r.json())

    r = await client.post(
        f"/api/orchestration/chat/sessions/{sid}/messages",
        json={"content": "United States"},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text

    r = await client.get(f"/api/orchestration/chat/sessions/{sid}", headers=auth(token))
    assert r.json()["session"]["id"] == sid
    assert len(r.json()["messages"]) >= 3


@pytest.mark.asyncio
async def test_stream_replays_session_messages(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    r = await client.post(
        "/api/orchestration/chat/sessions",
        json={"message": "find fintech companies"},
        headers=auth(token),
    )
    sid = r.json()["session"]["id"]
    r = await client.get(
        f"/api/orchestration/chat/sessions/{sid}/stream",
        headers={**auth(token), "Last-Event-ID": "0"},
    )
    assert r.status_code == 200
    assert "data:" in r.text  # SSE frames for the persisted messages


@pytest.mark.asyncio
async def test_save_icp_endpoint(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    r = await client.post(
        "/api/orchestration/chat/sessions",
        json={"message": "Find Fintech companies in the US with 200-5000 employees"},
        headers=auth(token),
    )
    sid = r.json()["session"]["id"]
    r = await client.post(
        f"/api/orchestration/chat/sessions/{sid}/save-icp", headers=auth(token)
    )
    assert r.status_code == 200, r.text
    assert "Fintech" in r.json()["icp"]["industries"]


@pytest.mark.asyncio
async def test_chat_requires_auth(client):
    r = await client.get("/api/orchestration/chat/sessions")
    assert r.status_code in (401, 403)
