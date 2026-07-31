"""Soft delete and CSV export for accounts and contacts.

Delete is soft on purpose. Signals, alerts, inbox tasks, cadence steps and CRM links all reference
an account or contact; removing the row would orphan every one of them and break the timeline that
explains why somebody was contacted. "Delete" therefore means "stop showing me this", and the
history stays intact and restorable.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def _account(client, token, name="Acme", domain="acme.com") -> str:
    r = await client.post(
        "/api/accounts", headers=auth(token),
        json={"name": name, "domain": domain, "industry": "Fintech"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _contact(client, token, account_id, full_name="Dana Scully") -> str:
    r = await client.post(
        f"/api/accounts/{account_id}/contacts", headers=auth(token),
        json={"full_name": full_name, "title": "VP Engineering", "email": "dana@acme.com"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


# ---- accounts -----------------------------------------------------------------------------------

async def test_deleting_an_account_hides_it_but_keeps_the_row(client):
    token = await signup(client, slug="de1", email="o@de1.com", company="DE1")
    aid = await _account(client, token)

    r = await client.delete(f"/api/accounts/{aid}", headers=auth(token))
    assert r.status_code == 200
    assert r.json()["restorable"] is True

    listed = await client.get("/api/accounts", headers=auth(token))
    assert aid not in [a["id"] for a in listed.json()]

    # ...but it is still there, which is what makes restore possible.
    still = await client.get(f"/api/accounts/{aid}", headers=auth(token))
    assert still.status_code == 200


async def test_deleting_an_account_is_idempotent(client):
    """A double-clicked button is a success, not a confusing 404."""
    token = await signup(client, slug="de2", email="o@de2.com", company="DE2")
    aid = await _account(client, token)
    assert (await client.delete(f"/api/accounts/{aid}", headers=auth(token))).status_code == 200
    assert (await client.delete(f"/api/accounts/{aid}", headers=auth(token))).status_code == 200


async def test_a_deleted_account_can_be_restored(client):
    """The whole point of not hard-deleting."""
    token = await signup(client, slug="de3", email="o@de3.com", company="DE3")
    aid = await _account(client, token)
    await client.delete(f"/api/accounts/{aid}", headers=auth(token))

    r = await client.post(f"/api/accounts/{aid}/unarchive", headers=auth(token))
    assert r.status_code == 200
    listed = await client.get("/api/accounts", headers=auth(token))
    assert aid in [a["id"] for a in listed.json()]


async def test_deleting_an_unknown_account_is_a_404(client):
    token = await signup(client, slug="de4", email="o@de4.com", company="DE4")
    r = await client.delete("/api/accounts/does-not-exist", headers=auth(token))
    assert r.status_code == 404


async def test_account_export_is_csv_and_excludes_deleted_by_default(client):
    token = await signup(client, slug="de5", email="o@de5.com", company="DE5")
    keep = await _account(client, token, name="Keep Co", domain="keep.com")
    drop = await _account(client, token, name="Drop Co", domain="drop.com")
    await client.delete(f"/api/accounts/{drop}", headers=auth(token))

    r = await client.get("/api/accounts/export/csv", headers=auth(token))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "Keep Co" in r.text
    assert "Drop Co" not in r.text
    assert keep


async def test_account_export_can_include_deleted(client):
    """An export is also how you take your data with you, so the deleted rows must be reachable."""
    token = await signup(client, slug="de6", email="o@de6.com", company="DE6")
    drop = await _account(client, token, name="Drop Co", domain="drop.com")
    await client.delete(f"/api/accounts/{drop}", headers=auth(token))

    r = await client.get("/api/accounts/export/csv?include_archived=true", headers=auth(token))
    assert "Drop Co" in r.text


async def test_the_export_route_is_not_shadowed_by_the_id_route(client):
    """`/{account_id}` would otherwise swallow `/export`, and the failure is a confusing 404 on a
    route that exists."""
    token = await signup(client, slug="de7", email="o@de7.com", company="DE7")
    r = await client.get("/api/accounts/export/csv", headers=auth(token))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")


# ---- contacts -----------------------------------------------------------------------------------

async def test_deleting_a_contact_hides_it_but_keeps_the_row(client):
    token = await signup(client, slug="de8", email="o@de8.com", company="DE8")
    aid = await _account(client, token)
    cid = await _contact(client, token, aid)

    r = await client.delete(f"/api/contacts/{cid}", headers=auth(token))
    assert r.status_code == 200

    listed = await client.get("/api/contacts", headers=auth(token))
    assert cid not in [c["id"] for c in listed.json()]

    with_deleted = await client.get("/api/contacts?include_deleted=true", headers=auth(token))
    assert cid in [c["id"] for c in with_deleted.json()]
    assert aid


async def test_a_deleted_contact_can_be_restored(client):
    token = await signup(client, slug="de9", email="o@de9.com", company="DE9")
    aid = await _account(client, token)
    cid = await _contact(client, token, aid)
    await client.delete(f"/api/contacts/{cid}", headers=auth(token))

    r = await client.post(f"/api/contacts/{cid}/restore", headers=auth(token))
    assert r.status_code == 200
    listed = await client.get("/api/contacts", headers=auth(token))
    assert cid in [c["id"] for c in listed.json()]


async def test_deleting_a_contact_is_idempotent(client):
    token = await signup(client, slug="de10", email="o@de10.com", company="DE10")
    aid = await _account(client, token)
    cid = await _contact(client, token, aid)
    assert (await client.delete(f"/api/contacts/{cid}", headers=auth(token))).status_code == 200
    assert (await client.delete(f"/api/contacts/{cid}", headers=auth(token))).status_code == 200


async def test_contact_export_matches_what_the_list_shows(client):
    """An export that disagrees with the screen is worse than none — both go through one query."""
    token = await signup(client, slug="de11", email="o@de11.com", company="DE11")
    aid = await _account(client, token)
    keep = await _contact(client, token, aid, full_name="Keep Person")
    drop = await _contact(client, token, aid, full_name="Drop Person")
    await client.delete(f"/api/contacts/{drop}", headers=auth(token))

    r = await client.get("/api/contacts/export", headers=auth(token))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "Keep Person" in r.text
    assert "Drop Person" not in r.text
    assert keep


async def test_contact_export_honours_the_same_filters(client):
    token = await signup(client, slug="de12", email="o@de12.com", company="DE12")
    aid = await _account(client, token)
    await _contact(client, token, aid, full_name="Alpha Person")
    await _contact(client, token, aid, full_name="Beta Person")

    r = await client.get("/api/contacts/export?q=alpha", headers=auth(token))
    assert "Alpha Person" in r.text
    assert "Beta Person" not in r.text


async def test_deleted_contacts_never_leak_across_tenants(client):
    """The soft-delete filter must not weaken tenant scoping."""
    a = await signup(client, slug="de13", email="o@de13.com", company="DE13")
    b = await signup(client, slug="de14", email="o@de14.com", company="DE14")
    aid = await _account(client, a)
    cid = await _contact(client, a, aid)
    await client.delete(f"/api/contacts/{cid}", headers=auth(a))

    other = await client.get("/api/contacts?include_deleted=true", headers=auth(b))
    assert cid not in [c["id"] for c in other.json()]
    assert aid
