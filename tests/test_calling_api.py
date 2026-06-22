"""Cold-calling API: queue, script, disposition, isolation + phone in the Contacts list."""
from __future__ import annotations

from nexus.core.security import decode_access_token
from nexus.models.account import Account, Contact
from tests.conftest import auth, signup, tenant_session


async def _seed(client, slug):
    token = await signup(client, slug=slug, email=f"o@{slug}.x", company=slug.title())
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
        ts.add(acc)
        await ts.flush()
        c = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe",
                    title="VP Sales", phone="+15551234567")
        ts.add(c)
        await ts.flush()
        return token, acc.id, c.id


async def test_call_queue_flow(client):
    token, acc_id, c_id = await _seed(client, "callflow")
    h = auth(token)

    # Queue a call.
    r = await client.post("/api/calling/tasks", headers=h,
                          json={"account_id": acc_id, "contact_id": c_id, "reason": "hot signal"})
    assert r.status_code == 201
    task_id = r.json()["id"]
    assert r.json()["phone"] == "+15551234567"

    # It shows in the queue with contact context.
    q = (await client.get("/api/calling/queue", headers=h)).json()
    assert [t["id"] for t in q] == [task_id]
    assert q[0]["contact_name"] == "Jane Doe" and q[0]["phone"] == "+15551234567"

    # Generate the AI script.
    s = (await client.post(f"/api/calling/tasks/{task_id}/script", headers=h)).json()
    assert s["opener"] and isinstance(s["discovery_questions"], list)

    # Log a terminal disposition -> task closes, activity recorded.
    d = await client.post(f"/api/calling/tasks/{task_id}/disposition", headers=h,
                          json={"disposition": "meeting_booked", "next_step": "demo Tue"})
    assert d.status_code == 200 and d.json()["disposition"] == "meeting_booked"
    assert (await client.get("/api/calling/queue", headers=h)).json() == []  # closed
    acts = (await client.get(f"/api/calling/contacts/{c_id}/activities", headers=h)).json()
    assert len(acts) == 1 and acts[0]["next_step"] == "demo Tue"


async def test_disposition_rejects_unknown_value(client):
    token, acc_id, c_id = await _seed(client, "calldisp")
    h = auth(token)
    task_id = (await client.post("/api/calling/tasks", headers=h,
                                 json={"account_id": acc_id, "contact_id": c_id})).json()["id"]
    r = await client.post(f"/api/calling/tasks/{task_id}/disposition", headers=h,
                          json={"disposition": "nonsense"})
    assert r.status_code == 422


async def test_call_queue_is_tenant_isolated(client):
    t1, acc1, c1 = await _seed(client, "calliso1")
    t2, acc2, c2 = await _seed(client, "calliso2")
    await client.post("/api/calling/tasks", headers=auth(t1),
                      json={"account_id": acc1, "contact_id": c1})
    # Tenant 2's queue must not see tenant 1's task.
    assert (await client.get("/api/calling/queue", headers=auth(t2))).json() == []


async def test_contacts_list_includes_phone(client):
    token, acc_id, c_id = await _seed(client, "callphone")
    rows = (await client.get("/api/contacts", headers=auth(token))).json()
    assert rows[0]["phone"] == "+15551234567"
    assert "phone_confidence" in rows[0]
