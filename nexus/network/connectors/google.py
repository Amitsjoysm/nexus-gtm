"""Real Google connector: People API contacts + Calendar meetings → graph identities/touchpoints.

Scopes (both Google "sensitive", not Gmail-grade restricted): contacts.readonly + calendar.readonly
+ openid/email (for the connected account label). Within one fetch, contacts and calendar attendees
are merged by email so each person yields ONE RawIdentity (with its meeting touchpoints), keeping the
ingest model 'one identity per person per source'.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, SourceAccountRef, Touchpoint
from nexus.network.connectors.oauthbase import OAuthConnector

_PEOPLE = "https://people.googleapis.com/v1/people/me/connections"
_CALENDAR = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_PERSON_FIELDS = "names,emailAddresses,organizations,metadata"
_MAX_PAGES = 500  # 100k contacts at pageSize 200 — bound an unbounded nextPageToken


class GoogleConnector(OAuthConnector):
    provider = "google"
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    scopes = [
        "openid",
        "email",
        "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
    ]
    extra_authorize_params = {
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }

    async def fetch(self, account: SourceAccountRef, since: str | None) -> NetworkSyncBatch:
        token = account.oauth.get("access_token", "")
        self_email = (account.external_account_id or "").lower()
        # person keyed by lowercased email (or resourceName when no email) → one RawIdentity each.
        people: dict[str, dict] = {}
        touchpoints: list[Touchpoint] = []

        def upsert(key: str, **fields) -> str:
            cur = people.setdefault(key, {"external_id": key})
            for k, v in fields.items():
                if v and not cur.get(k):
                    cur[k] = v
            return cur["external_id"]

        async with self._client() as c:
            next_cursor = await self._load_contacts(c, token, since, upsert)
            await self._load_calendar(c, token, self_email, upsert, touchpoints)

        identities = [
            RawIdentity(
                external_id=p["external_id"], email=p.get("email"), name=p.get("name"),
                title=p.get("title"), company=p.get("company"),
                relation=p.get("relation", "contact"),
            )
            for p in people.values()
        ]
        return NetworkSyncBatch(identities=identities, touchpoints=touchpoints,
                                next_cursor=next_cursor)

    async def _load_contacts(self, c: httpx.AsyncClient, token: str, since: str | None, upsert) -> str | None:
        next_cursor: str | None = None
        page: str | None = None
        for _ in range(_MAX_PAGES):
            params = {"personFields": _PERSON_FIELDS, "pageSize": 200}
            if since:
                params["syncToken"] = since
            if page:
                params["pageToken"] = page
            resp = await self._get_json(c, _PEOPLE, token=token, params=params)
            # An expired syncToken (HTTP 400) → fall back to a full resync once.
            if resp.status_code == 400 and since:
                return await self._load_contacts(c, token, None, upsert)
            resp.raise_for_status()
            data = resp.json()
            for person in data.get("connections", []):
                email = _first(person.get("emailAddresses"), "value")
                key = (email or "").lower() or person.get("resourceName", "")
                org = (person.get("organizations") or [{}])[0]
                upsert(
                    key, email=(email.lower() if email else None),
                    name=_first(person.get("names"), "displayName"),
                    company=org.get("name"), title=org.get("title"), relation="contact",
                )
            page = data.get("nextPageToken")
            if not page:
                next_cursor = data.get("nextSyncToken")
                break
        return next_cursor

    async def _load_calendar(self, c, token, self_email, upsert, touchpoints) -> None:
        time_min = (datetime.now(timezone.utc) - timedelta(days=365)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        params = {"timeMin": time_min, "singleEvents": "true", "maxResults": 250,
                  "orderBy": "startTime"}
        resp = await self._get_json(c, _CALENDAR, token=token, params=params)
        if resp.status_code >= 400:
            return  # calendar is best-effort; contacts already loaded
        for ev in resp.json().get("items", []):
            start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
            at = _parse_dt(start)
            for att in ev.get("attendees") or []:
                if att.get("self") or att.get("resource"):
                    continue
                email = (att.get("email") or "").lower()
                if not email or email == self_email:
                    continue
                ext = upsert(email, email=email, name=att.get("displayName"), relation="calendar")
                if at is not None:
                    touchpoints.append(
                        Touchpoint(person_external_id=ext, kind="meeting", at=at)
                    )


def _first(items, key):
    if items and isinstance(items, list):
        return (items[0] or {}).get(key)
    return None


def _parse_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
