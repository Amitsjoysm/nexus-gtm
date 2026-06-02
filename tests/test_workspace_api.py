"""Workspace & member management endpoints, including last-owner protection."""
from __future__ import annotations

from tests.conftest import auth, signup


def _membership(members: list[dict], email: str) -> dict:
    return next(m for m in members if m["email"] == email)


async def test_member_lifecycle(client):
    h = auth(await signup(client, email="owner@acme.com"))

    members = (await client.get("/api/workspace/members", headers=h)).json()
    assert len(members) == 1 and members[0]["role"] == "owner"

    invited = await client.post("/api/workspace/members", headers=h, json={
        "email": "rep@acme.com", "full_name": "Rep Two", "password": "password123",
        "role": "rep"})
    assert invited.status_code == 201, invited.text
    rep_membership_id = invited.json()["membership_id"]

    members = (await client.get("/api/workspace/members", headers=h)).json()
    assert len(members) == 2

    promoted = await client.put(
        f"/api/workspace/members/{rep_membership_id}/role", headers=h,
        json={"role": "manager"})
    assert promoted.status_code == 200 and promoted.json()["role"] == "manager"

    removed = await client.delete(
        f"/api/workspace/members/{rep_membership_id}", headers=h)
    assert removed.status_code == 204
    assert len((await client.get("/api/workspace/members", headers=h)).json()) == 1


async def test_duplicate_invite_conflicts(client):
    h = auth(await signup(client, email="owner@acme.com"))
    body = {"email": "dup@acme.com", "full_name": "Dup", "password": "password123"}
    assert (await client.post("/api/workspace/members", headers=h, json=body)).status_code == 201
    assert (await client.post("/api/workspace/members", headers=h, json=body)).status_code == 409


async def test_last_owner_is_protected(client):
    h = auth(await signup(client, email="owner@acme.com"))
    members = (await client.get("/api/workspace/members", headers=h)).json()
    owner_id = _membership(members, "owner@acme.com")["membership_id"]

    demote = await client.put(
        f"/api/workspace/members/{owner_id}/role", headers=h, json={"role": "admin"})
    assert demote.status_code == 400

    remove = await client.delete(f"/api/workspace/members/{owner_id}", headers=h)
    assert remove.status_code == 400


async def test_workspace_create_and_rename(client):
    h = auth(await signup(client))
    created = await client.post("/api/workspace/workspaces", headers=h, json={"name": "EMEA"})
    assert created.status_code == 201
    ws_id = created.json()["id"]

    renamed = await client.put(
        f"/api/workspace/workspaces/{ws_id}", headers=h, json={"name": "EMEA AEs"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "EMEA AEs"


async def test_rep_cannot_manage_members(client):
    h = auth(await signup(client, email="owner@acme.com"))
    invited = await client.post("/api/workspace/members", headers=h, json={
        "email": "rep@acme.com", "full_name": "Rep", "password": "password123", "role": "rep"})
    assert invited.status_code == 201

    rep_token = (await client.post("/api/auth/login", json={
        "email": "rep@acme.com", "password": "password123"})).json()["access_token"]
    forbidden = await client.get("/api/workspace/members", headers=auth(rep_token))
    assert forbidden.status_code == 403
