"""A HubSpot token pasted into Integrations is tested as itself, and a refusal says why (2026-10-02).

Reported from gtm.infojoy.com: a token created minutes earlier was reported "Invalid or expired".
HubSpot answers 401 to anything it cannot use as a bearer token, and the screen turned every 401
into that one sentence, which reads as "wait for a new one" when the token is the wrong KIND:

* **A Personal Access Key is not an API token.** HubSpot moved private apps under Development as
  "Legacy apps", and the key that is easy to find there is the Personal Access Key, which is for the
  HubSpot CLI. A private app's access token starts with ``pat-``; a 401 on anything else now names
  that difference and where the right token is.
* **What a paste adds is removed**: a ``Bearer `` prefix and line breaks, which made a good token
  fail as "Bearer pat-..." or two halves.
* **HubSpot's app address is not its API.** An "API base" of ``app.hubspot.com`` (the address bar)
  sent every request to the web app; refused with the address to use.
* **Test never tests somebody else's CRM.** When this workspace's stored credential could not be
  built, resolution fell back to the deployment's own connector, so Test reported on THAT token.
  Sync already refused this; Test now does too.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, principal_from_token, signup, tenant_session


@pytest.fixture(autouse=True)
def no_connector_override():
    from nexus.ingestion.crm import set_crm_connector

    set_crm_connector(None)
    yield
    set_crm_connector(None)


def _fixed(status, body=None):
    async def request(method, path, body_=None):
        return status, body or {}
    return request


async def _stored_token(token):
    from nexus.ingestion.crm_credentials import get_connection
    from nexus.integrations import connections

    async with tenant_session(principal_from_token(token).tenant_id) as ts:
        return connections.secret_bundle(await get_connection(ts)).get("access_token")


async def test_a_pasted_bearer_prefix_and_line_break_are_removed(client):
    token = await signup(client, slug="paste1", email="rep@paste1.io")
    r = await client.put("/api/integrations/crm/connection", headers=auth(token), json={
        "provider": "hubspot", "access_token": "  Bearer pat-na1-1234\n-abcd  "})
    assert r.status_code == 200, r.text
    assert await _stored_token(token) == "pat-na1-1234-abcd"


async def test_a_personal_access_key_is_named_for_what_it_is():
    from nexus.ingestion.crm import HubSpotConnector

    conn = HubSpotConnector(access_token="cli-personal-access-key")
    conn._request = _fixed(401)  # type: ignore[method-assign]
    detail = (await conn.test_connection()).detail
    assert "Personal Access Key" in detail and "pat-" in detail
    assert "Private" in detail or "private app" in detail


async def test_a_private_app_token_that_is_refused_says_to_copy_it_again():
    from nexus.ingestion.crm import HubSpotConnector

    conn = HubSpotConnector(access_token="pat-na1-0000")
    conn._request = _fixed(401)  # type: ignore[method-assign]
    detail = (await conn.test_connection()).detail
    assert detail.startswith("Invalid or expired access token")
    assert "Personal Access Key" not in detail


@pytest.mark.parametrize("base", ["https://app.hubspot.com",
                                  "https://app-eu1.hubspot.com/contacts/1234"])
async def test_hubspots_app_address_is_not_accepted_as_the_api(client, base):
    token = await signup(client, slug="base1", email="rep@base1.io")
    r = await client.put("/api/integrations/crm/connection", headers=auth(token), json={
        "provider": "hubspot", "access_token": "pat-na1-1", "api_base": base})
    assert r.status_code == 400 and "api.hubapi.com" in r.json()["detail"]


async def test_a_regional_api_host_is_still_accepted(client):
    token = await signup(client, slug="base2", email="rep@base2.io")
    r = await client.put("/api/integrations/crm/connection", headers=auth(token), json={
        "provider": "hubspot", "access_token": "pat-eu1-1", "api_base": "https://api-eu1.hubapi.com"})
    assert r.status_code == 200, r.text


async def test_a_test_with_no_workspace_token_says_it_tested_the_deployment_default(
        client, monkeypatch):
    """With no token of its own, a workspace's Test reaches the deployment's connector. That is the
    right thing to test, and the wrong thing to report as if it were the workspace's: a stale
    deployment token read as "the token I just made is invalid"."""
    from nexus.ingestion import crm

    class EnvConnector(crm.StubCRMConnector):
        async def test_connection(self):
            return crm.CRMTestResult(ok=False, label="HubSpot",
                                     detail="Invalid or expired access token.")

    monkeypatch.setattr("nexus.ingestion.crm_credentials.get_crm_connector",
                        lambda: EnvConnector())
    token = await signup(client, slug="envonly", email="rep@envonly.io")
    r = await client.post("/api/integrations/crm/connection/test", headers=auth(token))
    assert r.json()["ok"] is False
    assert "deployment's default" in r.json()["detail"]
    assert "Invalid or expired" in r.json()["detail"]


def test_test_checks_the_token_in_the_box_not_only_the_saved_one():
    """Typing a token and pressing Test used to test whatever was saved before (or the deployment
    default), so a correct new token read as invalid. Test now saves a typed token first."""
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1]
            / "frontend/src/pages/IntegrationsPage.tsx").read_text(encoding="utf-8")
    on_test = page[page.index("async function onTest"):page.index("async function onClear")]
    assert "token.trim()" in on_test and "setCrmConnection" in on_test
    # "A token is saved" only when the workspace has one: the deployment's is not theirs.
    assert 'conn.source === "tenant" && conn.has_credentials' in page


def test_the_form_says_where_the_right_token_is_and_which_one_does_not_work():
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1]
            / "frontend/src/pages/IntegrationsPage.tsx").read_text(encoding="utf-8")
    assert "starts with pat-" in page and "Personal Access Key will not work" in page


async def test_test_never_reports_on_the_deployments_own_crm(client, monkeypatch):
    from nexus.ingestion import crm
    from nexus.models.integration import IntegrationConnection

    tested = []

    class EnvConnector(crm.StubCRMConnector):
        async def test_connection(self):
            tested.append("env")
            return crm.CRMTestResult(ok=False, label="HubSpot",
                                     detail="Invalid or expired access token.")

    monkeypatch.setattr(crm, "get_crm_connector", lambda: EnvConnector())
    monkeypatch.setattr("nexus.ingestion.crm_credentials.get_crm_connector",
                        lambda: EnvConnector())
    token = await signup(client, slug="unusable", email="rep@unusable.io")
    await client.put("/api/integrations/crm/connection", headers=auth(token), json={
        "provider": "hubspot", "access_token": "pat-na1-1"})
    async with tenant_session(principal_from_token(token).tenant_id) as ts:
        row = await ts.first(IntegrationConnection)
        row.secret = {"enc": "sealed-with-a-key-this-server-no-longer-has"}

    r = await client.post("/api/integrations/crm/connection/test", headers=auth(token))
    assert r.status_code == 200
    assert r.json()["ok"] is False and "could not be read" in r.json()["detail"]
    assert tested == []
