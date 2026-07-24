from __future__ import annotations

import httpx


def test_authorize_url_includes_pkce_and_scopes():
    from nexus.network.connectors.google import GoogleConnector

    c = GoogleConnector(client_id="cid", client_secret="sec", redirect_uri="https://x/cb")
    url = c.authorize_url_for(state="ST", code_challenge="CH")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid" in url and "state=ST" in url
    assert "code_challenge=CH" in url and "code_challenge_method=S256" in url
    assert "access_type=offline" in url and "contacts.readonly" in url


async def test_exchange_code_posts_and_returns_tokens():
    from nexus.network.connectors.google import GoogleConnector

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://oauth2.googleapis.com/token"
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "CODE" and body["code_verifier"] == "VER"
        return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT",
                                         "expires_in": 3600})

    c = GoogleConnector(client_id="cid", client_secret="sec", redirect_uri="https://x/cb",
                        transport=httpx.MockTransport(handler))
    tokens = await c.exchange_code(code="CODE", code_verifier="VER")
    assert tokens["access_token"] == "AT" and tokens["refresh_token"] == "RT"


async def test_fetch_merges_contacts_and_calendar_by_email():
    from nexus.network.connectors.base import SourceAccountRef
    from nexus.network.connectors.google import GoogleConnector

    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "people.googleapis.com" in u:
            return httpx.Response(200, json={
                "connections": [
                    {"resourceName": "people/c1",
                     "names": [{"displayName": "Ada Okafor"}],
                     "emailAddresses": [{"value": "ada@helix.com"}],
                     "organizations": [{"name": "Helix Health", "title": "CTO"}]},
                    {"resourceName": "people/c2",
                     "names": [{"displayName": "No Email Person"}]},
                ],
                "nextSyncToken": "SYNC2",
            })
        if "calendar/v3" in u:
            return httpx.Response(200, json={
                "items": [
                    {"start": {"dateTime": "2026-06-20T10:00:00Z"},
                     "attendees": [
                         {"email": "self@me.com", "self": True},
                         {"email": "ada@helix.com", "displayName": "Ada Okafor"},
                     ]},
                ],
            })
        return httpx.Response(404)

    c = GoogleConnector(client_id="x", client_secret="y", redirect_uri="https://x/cb",
                        transport=httpx.MockTransport(handler))
    ref = SourceAccountRef(id="a", provider="google", external_account_id="self@me.com",
                           oauth={"access_token": "AT"})
    batch = await c.fetch(ref, None)

    # Ada appears once (contact + calendar merged by email), with a meeting touchpoint.
    ada = next(i for i in batch.identities if i.email == "ada@helix.com")
    assert ada.name == "Ada Okafor" and ada.title == "CTO" and ada.company == "Helix Health"
    assert {i.email for i in batch.identities} == {"ada@helix.com", None} or \
           {i.name for i in batch.identities} == {"Ada Okafor", "No Email Person"}
    meets = [t for t in batch.touchpoints if t.person_external_id == ada.external_id]
    assert len(meets) == 1 and meets[0].kind == "meeting"
    assert batch.next_cursor == "SYNC2"
