from __future__ import annotations

import httpx


def test_authorize_url_targets_graph_scopes():
    from nexus.network.connectors.microsoft import MicrosoftConnector

    c = MicrosoftConnector(client_id="cid", client_secret="s", redirect_uri="https://x/cb",
                           tenant="common")
    url = c.authorize_url_for(state="ST", code_challenge="CH")
    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    assert "Contacts.Read" in url and "Calendars.Read" in url and "offline_access" in url


async def test_fetch_maps_contacts_and_events():
    from nexus.network.connectors.base import SourceAccountRef
    from nexus.network.connectors.microsoft import MicrosoftConnector

    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if u.endswith("/me/contacts") or "/me/contacts?" in u:
            return httpx.Response(200, json={"value": [
                {"id": "ct1", "displayName": "Ada Okafor",
                 "emailAddresses": [{"address": "ada@helix.com"}],
                 "companyName": "Helix Health", "jobTitle": "CTO"},
            ]})
        if "/me/events" in u or "/me/calendarView" in u:
            return httpx.Response(200, json={"value": [
                {"start": {"dateTime": "2026-06-20T10:00:00.000000", "timeZone": "UTC"},
                 "attendees": [
                     {"emailAddress": {"address": "ada@helix.com", "name": "Ada Okafor"}},
                 ]},
            ]})
        return httpx.Response(404)

    c = MicrosoftConnector(client_id="x", client_secret="y", redirect_uri="https://x/cb",
                           tenant="common", transport=httpx.MockTransport(handler))
    ref = SourceAccountRef(id="a", provider="microsoft", external_account_id="self@me.com",
                           oauth={"access_token": "AT"})
    batch = await c.fetch(ref, None)
    ada = next(i for i in batch.identities if i.email == "ada@helix.com")
    assert ada.title == "CTO" and ada.company == "Helix Health"
    assert any(t.kind == "meeting" and t.person_external_id == ada.external_id
               for t in batch.touchpoints)
