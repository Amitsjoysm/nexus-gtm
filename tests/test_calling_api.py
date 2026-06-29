"""Cold-calling API: queue, script, disposition, isolation + phone in the Contacts list."""
from __future__ import annotations

from nexus.core.security import decode_access_token
from nexus.models.account import Account, Contact
from nexus.models.intelligence import AccountScore
from nexus.models.signal import SignalEvent
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


async def test_call_brief_returns_sourced_research(client):
    token = await signup(client, slug="callbrief", email="o@callbrief.x", company="Callbrief")
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co", industry="SaaS",
                      employee_count=200, country="US", tech_stack=["Salesforce", "Segment"])
        ts.add(acc)
        await ts.flush()
        c = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe", title="VP RevOps",
                    seniority="vp", phone="+15551234567", email="jane@acme.co",
                    email_status="valid", enrichment_source="apollo")
        ts.add(c)
        await ts.flush()
        # One signal tied to the person, one to the company — both with a source.
        ts.add(SignalEvent(tenant_id=tid, account_id=acc.id, contact_id=c.id, kind="job_switch",
                           source="LinkedIn", title="Jane started as VP RevOps", strength=0.9,
                           dedupe_key="s-personal", url="https://example.com/jane"))
        ts.add(SignalEvent(tenant_id=tid, account_id=acc.id, kind="funding", source="Crunchbase",
                           title="Acme raised $20M Series B", strength=0.8, dedupe_key="s-account"))
        ts.add(AccountScore(tenant_id=tid, account_id=acc.id, icp_fit=82, composite=82,
                            rationale="Strong size + tech-stack match."))
        await ts.flush()
        acc_id, c_id = acc.id, c.id

    h = auth(token)
    task_id = (await client.post("/api/calling/tasks", headers=h,
                                 json={"account_id": acc_id, "contact_id": c_id})).json()["id"]
    b = (await client.get(f"/api/calling/tasks/{task_id}/brief", headers=h)).json()

    # Person block: identity, a role-aware angle, and the enrichment source.
    assert b["contact"]["name"] == "Jane Doe"
    assert b["contact"]["role_angle"]
    assert b["contact"]["source"] == "apollo"
    # Company block: firmographics + ICP fit + a provenance label.
    assert b["account"]["industry"] == "SaaS" and b["account"]["fit_score"] == 82
    assert "Salesforce" in b["account"]["tech_stack"] and b["account"]["source"]
    # Signals carry source/url; the person-tied one ranks first.
    assert b["signals"][0]["is_personal"] is True and b["signals"][0]["source"] == "LinkedIn"
    assert any(s["title"].startswith("Acme raised") for s in b["signals"])
    # Talking points are grounded in the data (fit score appears).
    assert any("ICP fit 82" in tp for tp in b["talking_points"])


async def test_call_brief_is_tenant_isolated(client):
    t1, acc1, c1 = await _seed(client, "briefiso1")
    t2 = await signup(client, slug="briefiso2", email="o@briefiso2.x", company="Briefiso2")
    task_id = (await client.post("/api/calling/tasks", headers=auth(t1),
                                 json={"account_id": acc1, "contact_id": c1})).json()["id"]
    # Tenant 2 cannot read tenant 1's call brief.
    r = await client.get(f"/api/calling/tasks/{task_id}/brief", headers=auth(t2))
    assert r.status_code == 404


async def test_contacts_list_includes_phone(client):
    token, acc_id, c_id = await _seed(client, "callphone")
    rows = (await client.get("/api/contacts", headers=auth(token))).json()
    assert rows[0]["phone"] == "+15551234567"
    assert "phone_confidence" in rows[0]
