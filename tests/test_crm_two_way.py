# tests/test_crm_two_way.py
"""Connecting a CRM has to mean data moves both ways, or say why it does not.

Reported 2026-09-16: "individual CRM integration to HubSpot or Salesforce not working, two-way
syncs are failing". Both adapters are real and implement both directions. What was broken sat
either side of them:

* **Salesforce could not be connected at all.** REST is addressed at the org's own host, which only
  the token response carries. The connect form collected a token and an optional API base, so
  `build_tenant_connector` returned None...
* **...and a credential we cannot honour fell back to the deployment connector silently.** That is
  the right posture for a background sweep (never stop collection) and exactly wrong for a person
  pressing Sync: the stub accepts everything, so the screen reported success while nothing reached
  Salesforce.
* **Nothing pulled.** `imports/crm_pull.py` reads accounts and contacts from both CRMs and the
  Integrations screen called neither; its only Sync button imported hand-typed rows.
"""
from __future__ import annotations

import pathlib

import pytest

from tests.conftest import auth, signup


# ---- Salesforce needs its instance URL ----------------------------------------------------------

def test_salesforce_builds_once_it_has_an_instance_url():
    from nexus.ingestion.crm_credentials import build_tenant_connector

    built = build_tenant_connector(
        "salesforce", {"access_token": "tok"}, "https://acme.my.salesforce.com"
    )
    assert built is not None
    assert built.source == "salesforce"


def test_salesforce_without_an_instance_url_cannot_be_built():
    from nexus.ingestion.crm_credentials import build_tenant_connector

    assert build_tenant_connector("salesforce", {"access_token": "tok"}, "") is None


async def test_connecting_salesforce_without_an_instance_url_is_refused_at_the_door(client):
    """Storing it would leave a row that reads "connected" and can reach nothing."""
    token = await signup(client, slug="crm1", email="admin@crm1.com", company="CRM One")
    r = await client.put(
        "/api/integrations/crm/connection", headers=auth(token),
        json={"provider": "salesforce", "access_token": "tok", "api_base": ""},
    )
    assert r.status_code == 400, r.text
    assert "instance url" in r.text.lower()


async def test_connecting_salesforce_with_an_instance_url_is_stored(client):
    token = await signup(client, slug="crm2", email="admin@crm2.com", company="CRM Two")
    r = await client.put(
        "/api/integrations/crm/connection", headers=auth(token),
        json={"provider": "salesforce", "access_token": "tok",
              "api_base": "https://acme.my.salesforce.com"},
    )
    assert r.status_code == 200, r.text
    conn = (await client.get("/api/integrations/crm/connection", headers=auth(token))).json()
    assert conn["provider"] == "salesforce"
    assert conn["has_credentials"] is True


# ---- an unusable credential is never silently swapped for another CRM ----------------------------

async def test_a_stored_credential_we_cannot_honour_is_reported_not_hidden(client, monkeypatch):
    """The failure this test exists for: Sync reported success against the deployment stub while
    the customer's own CRM received nothing."""
    from nexus.ingestion import crm_credentials

    token = await signup(client, slug="crm3", email="admin@crm3.com", company="CRM Three")
    await client.put(
        "/api/integrations/crm/connection", headers=auth(token),
        json={"provider": "hubspot", "access_token": "tok"},
    )
    # The secret no longer decrypts (a rotated key), so the row cannot build a connector.
    monkeypatch.setattr(crm_credentials, "build_tenant_connector", lambda *a, **k: None)

    r = await client.post(
        "/api/integrations/crm/sync", headers=auth(token), json={"source": "hubspot", "accounts": []},
    )
    assert r.status_code == 400, r.text
    assert "reconnect" in r.text.lower()


async def test_syncing_a_provider_the_workspace_is_not_connected_to_says_which_one_it_is(client):
    token = await signup(client, slug="crm4", email="admin@crm4.com", company="CRM Four")
    await client.put(
        "/api/integrations/crm/connection", headers=auth(token),
        json={"provider": "hubspot", "access_token": "tok"},
    )
    r = await client.post(
        "/api/integrations/crm/sync", headers=auth(token),
        json={"source": "salesforce", "accounts": []},
    )
    assert r.status_code == 400
    assert "hubspot" in r.text.lower()


# ---- both directions are reachable --------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/imports/accounts/crm", "/api/imports/contacts/crm"])
async def test_the_pull_endpoints_exist_for_a_connected_workspace(client, path, monkeypatch):
    """Pull is the half a customer notices first: they expect their book to appear."""
    from nexus.ingestion.crm import CRMAccount, CRMContact, StubCRMConnector

    class _Loaded(StubCRMConnector):
        source = "hubspot"

        async def fetch_accounts(self):
            return [CRMAccount(external_id="1", name="Marketjoy", domain="marketjoy.com")]

        async def fetch_contacts(self, *, limit: int = 200):
            return [CRMContact(external_id="9", full_name="Curtis Bent",
                               email="curtis@marketjoy.com", account_name="Marketjoy")]

    from nexus.ingestion import crm_credentials

    async def fake_resolve(ts):
        return _Loaded()

    # `_connector_or_400` imports this inside the function, so the source module is the seam.
    monkeypatch.setattr(crm_credentials, "resolve_crm_connector", fake_resolve)

    slug = "crm5acc" if "accounts" in path else "crm5con"
    token = await signup(client, slug=slug, email=f"admin@{slug}.com", company="CRM Five")
    await client.put(
        "/api/integrations/crm/connection", headers=auth(token),
        json={"provider": "hubspot", "access_token": "tok"},
    )
    r = await client.post(path, headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["created"] >= 1


def test_the_integrations_screen_drives_both_directions():
    src = pathlib.Path("frontend/src/pages/IntegrationsPage.tsx").read_text(encoding="utf-8")
    assert "pullAccountsFromCrm" in src, "no way to pull accounts from the CRM"
    assert "pullContactsFromCrm" in src, "no way to pull contacts from the CRM"
    # Salesforce cannot be connected without its host, so the form has to ask for it.
    assert "instance" in src.lower(), "the connect form never asks for the Salesforce instance URL"


def test_the_salesforce_hint_is_shown_only_for_salesforce():
    """A HubSpot admin must not be asked for an instance URL they do not have."""
    src = pathlib.Path("frontend/src/pages/IntegrationsPage.tsx").read_text(encoding="utf-8")
    assert 'provider === "salesforce"' in src
