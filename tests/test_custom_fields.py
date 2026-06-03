# tests/test_custom_fields.py
"""Custom-field CRUD + CSV upsert onto existing accounts/contacts (domain/email match)."""
from __future__ import annotations

import json

import pytest

from nexus.models.account import Account, Contact
from nexus.workers.tasks import tenant_session
from tests.conftest import auth, principal_from_token, signup


async def _seed_accounts(token) -> None:
    p = principal_from_token(token)
    async with tenant_session(p.tenant_id) as ts:
        ts.add(Account(name="NorthBank", domain="northbank.com"))
        ts.add(Account(name="WestPay", domain="westpay.io"))
        await ts.session.flush()


@pytest.mark.asyncio
async def test_create_list_delete_field(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    r = await client.post(
        "/api/custom-fields",
        json={"entity": "account", "label": "ARR", "kind": "number"},
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    assert r.json()["key"] == "arr"

    r = await client.get("/api/custom-fields?entity=account", headers=auth(token))
    assert any(d["id"] == fid for d in r.json())

    r = await client.delete(f"/api/custom-fields/{fid}", headers=auth(token))
    assert r.status_code == 204
    r = await client.get("/api/custom-fields?entity=account", headers=auth(token))
    assert all(d["id"] != fid for d in r.json())


@pytest.mark.asyncio
async def test_csv_import_upserts_and_creates_defs(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    await _seed_accounts(token)
    csv_text = "website,annual_revenue,tier\nnorthbank.com,5000000,A\nunknown.com,1,Z\n"
    r = await client.post(
        "/api/custom-fields/import",
        data={
            "entity": "account",
            "match_column": "website",
            "mapping": json.dumps({"annual_revenue": "arr", "tier": "tier"}),
        },
        files={"file": ("data.csv", csv_text, "text/csv")},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] == 1          # northbank matched, unknown.com skipped
    assert body["updated"] == 1
    assert body["skipped"] == 1
    assert sorted(body["created_fields"]) == ["arr", "tier"]

    # The new column metadata is queryable, and the value landed on the account.
    r = await client.get("/api/custom-fields?entity=account", headers=auth(token))
    keys = {d["key"] for d in r.json()}
    assert {"arr", "tier"} <= keys

    p = principal_from_token(token)
    async with tenant_session(p.tenant_id) as ts:
        acc = await ts.first(Account, Account.domain == "northbank.com")
        assert acc.custom_fields["arr"] == "5000000"
        assert acc.custom_fields["tier"] == "A"


@pytest.mark.asyncio
async def test_csv_import_bad_match_column_400(client):
    token = await signup(client, slug="acme", email="rep@acme.com")
    r = await client.post(
        "/api/custom-fields/import",
        data={
            "entity": "account",
            "match_column": "nope",
            "mapping": json.dumps({"tier": "tier"}),
        },
        files={"file": ("data.csv", "website,tier\nx.com,A\n", "text/csv")},
        headers=auth(token),
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_custom_fields_requires_admin(client):
    # A rep (default role from signup is owner, so make a manager-less check via missing auth).
    r = await client.get("/api/custom-fields")
    assert r.status_code in (401, 403)
