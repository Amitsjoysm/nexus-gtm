"""Real Microsoft connector (Graph): Outlook contacts + calendar events → identities/touchpoints.

Mirrors the Google connector against Microsoft Graph. Scopes: Contacts.Read + Calendars.Read +
offline_access + openid/email. Contacts and event attendees are merged by email within one fetch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, SourceAccountRef, Touchpoint
from nexus.network.connectors.oauthbase import OAuthConnector

_CONTACTS = "https://graph.microsoft.com/v1.0/me/contacts"
_EVENTS = "https://graph.microsoft.com/v1.0/me/events"


class MicrosoftConnector(OAuthConnector):
    provider = "microsoft"
    scopes = [
        "openid",
        "email",
        "offline_access",
        "https://graph.microsoft.com/Contacts.Read",
        "https://graph.microsoft.com/Calendars.Read",
    ]

    def __init__(self, *, tenant: str = "common", **kw):
        super().__init__(**kw)
        self.tenant = tenant or "common"
        self.authorize_url = (
            f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/authorize"
        )
        self.token_url = f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"

    async def fetch(self, account: SourceAccountRef, since: str | None) -> NetworkSyncBatch:
        token = account.oauth.get("access_token", "")
        self_email = (account.external_account_id or "").lower()
        people: dict[str, dict] = {}
        touchpoints: list[Touchpoint] = []

        def upsert(key: str, **fields) -> str:
            cur = people.setdefault(key, {"external_id": key})
            for k, v in fields.items():
                if v and not cur.get(k):
                    cur[k] = v
            return cur["external_id"]

        async with self._client() as c:
            resp = await self._get_json(
                c, _CONTACTS, token=token,
                params={"$select": "displayName,emailAddresses,companyName,jobTitle", "$top": 200},
            )
            if resp.status_code < 400:
                for ct in resp.json().get("value", []):
                    email = _first_email(ct.get("emailAddresses"))
                    key = (email or "").lower() or ct.get("id", "")
                    upsert(key, email=(email.lower() if email else None),
                           name=ct.get("displayName"), company=ct.get("companyName"),
                           title=ct.get("jobTitle"), relation="contact")

            time_min = (datetime.now(timezone.utc) - timedelta(days=365)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            ev_resp = await self._get_json(
                c, _EVENTS, token=token,
                params={"$select": "start,attendees", "$top": 250,
                        "$filter": f"start/dateTime ge '{time_min}'"},
            )
            if ev_resp.status_code < 400:
                for ev in ev_resp.json().get("value", []):
                    at = _parse_dt((ev.get("start") or {}).get("dateTime"))
                    for att in ev.get("attendees") or []:
                        email = ((att.get("emailAddress") or {}).get("address") or "").lower()
                        if not email or email == self_email:
                            continue
                        name = (att.get("emailAddress") or {}).get("name")
                        ext = upsert(email, email=email, name=name, relation="calendar")
                        if at is not None:
                            touchpoints.append(
                                Touchpoint(person_external_id=ext, kind="meeting", at=at)
                            )

        identities = [
            RawIdentity(
                external_id=p["external_id"], email=p.get("email"), name=p.get("name"),
                title=p.get("title"), company=p.get("company"),
                relation=p.get("relation", "contact"),
            )
            for p in people.values()
        ]
        return NetworkSyncBatch(identities=identities, touchpoints=touchpoints, next_cursor=None)


def _first_email(items):
    if items and isinstance(items, list):
        return (items[0] or {}).get("address")
    return None


def _parse_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "").split(".")[0] + "+00:00")
    except ValueError:
        return None
