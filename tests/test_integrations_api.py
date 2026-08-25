"""CRM sync and SEP push REST endpoints."""
from __future__ import annotations

import pytest

from nexus.integrations.sep import StubSEPConnector, set_sep_connector
from tests.conftest import auth, signup


@pytest.fixture(autouse=True)
def recording_sep():
    """Use a fresh recording SEP connector so pushes are observable and isolated."""
    connector = StubSEPConnector()
    set_sep_connector(connector)
    yield connector
    set_sep_connector(StubSEPConnector())


async def test_crm_sync_upserts_accounts(client):
    h = auth(await signup(client))
    payload = {
        "source": "salesforce",
        "accounts": [
            {"external_id": "001", "name": "Globex", "domain": "globex.com",
             "industry": "Software", "employee_count": 500, "country": "US"},
            {"external_id": "002", "name": "Initech", "domain": "initech.com"},
        ],
    }
    r = await client.post("/api/integrations/crm/sync", headers=h, json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["synced"] == 2

    # Idempotent: syncing the same external_ids updates, does not duplicate.
    again = await client.post("/api/integrations/crm/sync", headers=h, json=payload)
    assert again.json()["synced"] == 2

    accounts = (await client.get("/api/accounts", headers=h)).json()
    assert sorted(a["name"] for a in accounts) == ["Globex", "Initech"]


async def test_sep_push_records_contact(client, recording_sep):
    h = auth(await signup(client))
    acc = (await client.post("/api/accounts", headers=h, json={
        "name": "Globex", "domain": "globex.com"})).json()
    contact = (await client.post(
        f"/api/accounts/{acc['id']}/contacts", headers=h,
        json={"full_name": "Jane Doe", "email": "jane@globex.com"})).json()

    r = await client.post("/api/integrations/sep/push", headers=h, json={
        "sequence": "enterprise", "contact_id": contact["id"]})
    assert r.status_code == 200, r.text
    # "stub", not "outreach": an unconfigured deployment now names the stub rather than a
    # platform it never reached.
    assert r.json()["ok"] is True and r.json()["platform"] == "stub"
    assert recording_sep.pushed[0]["email"] == "jane@globex.com"
    assert recording_sep.pushed[0]["sequence"] == "enterprise"


async def test_sep_push_unknown_contact_404(client):
    h = auth(await signup(client))
    r = await client.post("/api/integrations/sep/push", headers=h, json={
        "sequence": "x", "contact_id": "nope"})
    assert r.status_code == 404


# ---- the source must not decide which constructor is called -------------------------------------
#
# `/crm/sync` looked the source up in a dict of connector CLASSES and called it with `sample=`.
# That only worked because the Salesforce connector is still a stub whose constructor happens to
# take that keyword; `HubSpotConnector.__init__` takes an access token, so choosing HubSpot in the
# Integrations screen raised TypeError and the user got a 500. Measured against the running stack
# before the fix: salesforce 200, hubspot 500.


async def test_importing_under_hubspot_does_not_500(client):
    """The exact request the Integrations screen sends when the dropdown says HubSpot."""
    h = auth(await signup(client, slug="crm1", email="o@crm1.com", company="CRM1"))
    r = await client.post(
        "/api/integrations/crm/sync",
        headers=h,
        json={
            "source": "hubspot",
            "accounts": [{"external_id": "hs-1", "name": "Hub Co", "domain": "hub.example"}],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["synced"] == 1


async def test_every_offered_source_can_be_imported_under(client):
    """Whatever the dropdown offers has to work. This is the test the dict-of-classes form could
    never pass, because it coupled the wire value to a constructor signature."""
    from nexus.api.routers.integrations import CRM_SOURCES

    for i, source in enumerate(CRM_SOURCES):
        h = auth(await signup(client, slug=f"crms{i}", email=f"o@crms{i}.com", company=f"S{i}"))
        r = await client.post(
            "/api/integrations/crm/sync",
            headers=h,
            json={
                "source": source,
                "accounts": [{"external_id": f"x-{i}", "name": f"Co {i}", "domain": f"c{i}.example"}],
            },
        )
        assert r.status_code == 200, f"{source}: {r.text}"
        assert r.json()["source"] == source


async def test_the_imported_rows_are_tagged_with_the_source_named(client):
    """`source` is provenance on this path — it decides how the row is labelled, nothing else."""
    h = auth(await signup(client, slug="crm2", email="o@crm2.com", company="CRM2"))
    await client.post(
        "/api/integrations/crm/sync",
        headers=h,
        json={
            "source": "hubspot",
            "accounts": [{"external_id": "hs-9", "name": "Tagged Co", "domain": "tag.example"}],
        },
    )
    accounts = (await client.get("/api/accounts", headers=h)).json()
    row = next(a for a in accounts if a["name"] == "Tagged Co")
    assert row["crm_source"] == "hubspot"


async def test_an_unknown_source_is_rejected_by_the_schema(client):
    """It used to reach a KeyError on the connector dict. The request schema stops it first."""
    h = auth(await signup(client, slug="crm3", email="o@crm3.com", company="CRM3"))
    r = await client.post(
        "/api/integrations/crm/sync",
        headers=h,
        json={"source": "pipedrive", "accounts": [{"external_id": "1", "name": "X"}]},
    )
    assert r.status_code == 422, r.text


def test_the_schema_pattern_and_the_source_list_agree():
    """Two places name the sellable CRM sources. If they drift, one of them is a lie: widening the
    schema without widening `CRM_SOURCES` lets a source through that the pull path cannot name."""
    import re

    from nexus.api.routers.integrations import CRM_SOURCES
    from nexus.api.schemas import CRMSyncRequest

    pattern = CRMSyncRequest.model_fields["source"].metadata[0].pattern
    assert set(re.findall(r"[a-z_]+", pattern.strip("^$()"))) == set(CRM_SOURCES)


async def test_pulling_from_a_crm_this_deployment_is_not_wired_to_says_so(client):
    """No rows posted means "pull from the CRM". Answering 0 accounts when we were never connected
    reads as "your CRM is empty", which is the wrong thing to tell someone."""
    h = auth(await signup(client, slug="crm4", email="o@crm4.com", company="CRM4"))
    r = await client.post(
        "/api/integrations/crm/sync", headers=h, json={"source": "hubspot", "accounts": []}
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"].lower()
    assert "not connected" in detail and "hubspot" in detail
