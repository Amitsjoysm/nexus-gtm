"""Sales Engagement Platform push (Outreach / Salesloft) behind one interface.

Two real adapters and one explicit stub. The distinction matters more here than it looks: until
this module had a named stub, ``get_sep_connector()`` returned ``OutreachConnector()`` — which was
*itself* a recording stub — so a deployment with no SEP configured reported every push as a
success that never happened. "Not configured" and "delivered" must never look the same; that is
the same rule ``nexus/integrations/apify.py`` states for unkeyed actors.

So ``StubSEPConnector`` is now the default and is named for what it is, and the two provider
classes are real HTTP clients that report a failure when they have no credentials. Per-tenant
credentials are resolved by :mod:`nexus.integrations.sep_credentials`.

Like the CRM connectors, an adapter never raises across the boundary: a flaky SEP degrades to
``SEPPushResult(ok=False)`` so a play or a cadence send is never broken by someone else's outage.
"""
from __future__ import annotations

import abc
import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("nexus.integrations.sep")

TokenProvider = Optional[Callable[[], Awaitable[str]]]


@dataclass(slots=True)
class SEPPushResult:
    ok: bool
    platform: str
    detail: dict = field(default_factory=dict)


@dataclass(slots=True)
class SEPTestResult:
    """The outcome of verifying a credential. Mirrors CRMTestResult exactly."""

    ok: bool
    label: str = ""
    detail: str = ""


class SEPConnector(abc.ABC):
    platform: str

    # Module singleton in a long-lived process: keep only recent pushes, or the buffer grows
    # without bound as campaigns/cadences send (same cap as the CRM connector).
    MAX_RECORDED_PUSHES = 1000

    def __init__(self) -> None:
        self.pushed: list[dict] = []

    def _record(self, record: dict) -> None:
        self.pushed.append(record)
        if len(self.pushed) > self.MAX_RECORDED_PUSHES:
            del self.pushed[: len(self.pushed) - self.MAX_RECORDED_PUSHES]

    @abc.abstractmethod
    async def push_contact(
        self, *, sequence: str, email: str | None, payload: dict
    ) -> SEPPushResult: ...

    async def test_connection(self) -> SEPTestResult:
        """Verify the credential. Never raises — a failure is a result, not an exception."""
        return SEPTestResult(ok=True, label=self.platform, detail="Offline stub connector.")


class StubSEPConnector(SEPConnector):
    """Zero-network default: records the push intent so plays stay testable end-to-end.

    This is what a deployment with no SEP configured gets. It is a **test double**, in the mould
    of ``nexus/network/connectors/fixture.py`` — never a stand-in for a real integration.
    """

    platform = "stub"

    async def push_contact(self, *, sequence, email, payload) -> SEPPushResult:
        record = {"sequence": sequence, "email": email, "payload": payload}
        self._record(record)
        logger.info("[%s] push to sequence %s for %s", self.platform, sequence, email)
        return SEPPushResult(ok=True, platform=self.platform, detail=record)


class _HttpSEPConnector(SEPConnector):
    """Shared JSON/HTTP plumbing for the real adapters."""

    api_base: str = ""

    def __init__(self, *, token: str = "", token_provider: TokenProvider = None):
        super().__init__()
        self._token = (token or "").strip()
        self._token_provider = token_provider
        self._refresh_lock = asyncio.Lock()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _request_blocking(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.api_base + path, data=data, method=method)
        for k, v in self._headers().items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode()
                return r.status, (json.loads(text) if text else {})
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, {"message": str(e)}

    async def _refresh_token(self) -> bool:
        """Renew the access token once, under a lock. See HubSpotConnector._refresh_token."""
        if self._token_provider is None:
            return False
        before = self._token
        async with self._refresh_lock:
            if self._token != before:
                return True
            token = await self._token_provider()
            if not token:
                return False
            self._token = token
            return True

    async def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        status, payload = await asyncio.to_thread(self._request_blocking, method, path, body)
        if status == 401 and await self._refresh_token():
            status, payload = await asyncio.to_thread(self._request_blocking, method, path, body)
        return status, payload


_SEP_TEST_ERRORS = {
    401: "Invalid or expired credentials.",
    403: "The credential is missing the scopes this integration needs.",
    429: "Rate limit reached — try again shortly.",
}


class SalesloftConnector(_HttpSEPConnector):
    """Real Salesloft connector (API key).

    Salesloft authenticates with a long-lived API key rather than OAuth, which is why this adapter
    needs no ``token_provider``: there is nothing to refresh.
    """

    platform = "salesloft"
    api_base = "https://api.salesloft.com"

    async def push_contact(self, *, sequence, email, payload) -> SEPPushResult:
        if not self._token:
            return SEPPushResult(ok=False, platform=self.platform,
                                 detail={"error": "no API key configured"})
        if not email:
            return SEPPushResult(ok=False, platform=self.platform,
                                 detail={"error": "no email for contact"})
        try:
            person_id = await self._upsert_person(email, payload)
            if not person_id:
                return SEPPushResult(ok=False, platform=self.platform,
                                     detail={"error": "person upsert failed"})
            cadence_id = await self._find_cadence(sequence)
            if not cadence_id:
                # A named cadence that does not exist is a user-fixable mistake, and saying so
                # beats a generic failure that sends them to check their API key.
                return SEPPushResult(
                    ok=False, platform=self.platform,
                    detail={"error": f"no cadence named {sequence!r} in Salesloft"},
                )
            st, body = await self._request("POST", "/v2/cadence_memberships.json", {
                "person_id": person_id, "cadence_id": cadence_id,
            })
            ok = st in (200, 201)
            record = {"sequence": sequence, "email": email, "payload": payload,
                      "person_id": person_id, "cadence_id": cadence_id}
            self._record(record)
            return SEPPushResult(ok=ok, platform=self.platform,
                                 detail=record if ok else {"error": f"HTTP {st}"})
        except Exception as exc:
            logger.warning("[salesloft] push_contact failed: %r", exc)
            return SEPPushResult(ok=False, platform=self.platform, detail={"error": str(exc)})

    async def _upsert_person(self, email: str, payload: dict) -> int | None:
        st, body = await self._request(
            "GET", f"/v2/people.json?email_addresses={urllib.parse.quote(email)}"
        )
        if st == 200 and body.get("data"):
            return body["data"][0].get("id")
        fields = {"email_address": email}
        if payload.get("first_name"):
            fields["first_name"] = payload["first_name"]
        if payload.get("last_name"):
            fields["last_name"] = payload["last_name"]
        if payload.get("account"):
            fields["company_name"] = payload["account"]
        st, body = await self._request("POST", "/v2/people.json", fields)
        return body.get("data", {}).get("id") if st in (200, 201) else None

    async def _find_cadence(self, name: str) -> int | None:
        st, body = await self._request(
            "GET", f"/v2/cadences.json?name={urllib.parse.quote(name)}"
        )
        if st == 200 and body.get("data"):
            return body["data"][0].get("id")
        return None

    async def test_connection(self) -> SEPTestResult:
        if not self._token:
            return SEPTestResult(ok=False, label="Salesloft", detail="No API key configured.")
        try:
            st, body = await self._request("GET", "/v2/me.json")
            if st == 200:
                who = (body.get("data") or {}).get("email") or "Salesloft"
                return SEPTestResult(ok=True, label=f"Salesloft · {who}", detail="Connected.")
            return SEPTestResult(ok=False, label="Salesloft",
                                 detail=_SEP_TEST_ERRORS.get(st, f"Salesloft returned HTTP {st}."))
        except Exception as exc:
            logger.warning("[salesloft] test_connection failed: %r", exc)
            return SEPTestResult(ok=False, label="Salesloft",
                                 detail="Could not reach Salesloft. Try again shortly.")


class OutreachConnector(_HttpSEPConnector):
    """Real Outreach connector (OAuth2).

    Outreach has no API-key path, so this adapter is only usable after the OAuth flow and always
    carries a ``token_provider`` for the 401 refresh.
    """

    platform = "outreach"
    api_base = "https://api.outreach.io"

    def _headers(self) -> dict:
        # Outreach is a JSON:API service and rejects a plain application/json content type.
        return {"Authorization": f"Bearer {self._token}",
                "Content-Type": "application/vnd.api+json"}

    async def push_contact(self, *, sequence, email, payload) -> SEPPushResult:
        if not self._token:
            return SEPPushResult(ok=False, platform=self.platform,
                                 detail={"error": "not connected to Outreach"})
        if not email:
            return SEPPushResult(ok=False, platform=self.platform,
                                 detail={"error": "no email for contact"})
        try:
            prospect_id = await self._upsert_prospect(email, payload)
            if not prospect_id:
                return SEPPushResult(ok=False, platform=self.platform,
                                     detail={"error": "prospect upsert failed"})
            sequence_id = await self._find_sequence(sequence)
            if not sequence_id:
                return SEPPushResult(
                    ok=False, platform=self.platform,
                    detail={"error": f"no sequence named {sequence!r} in Outreach"},
                )
            st, _ = await self._request("POST", "/api/v2/sequenceStates", {
                "data": {
                    "type": "sequenceState",
                    "relationships": {
                        "prospect": {"data": {"type": "prospect", "id": prospect_id}},
                        "sequence": {"data": {"type": "sequence", "id": sequence_id}},
                    },
                }
            })
            ok = st in (200, 201)
            record = {"sequence": sequence, "email": email, "payload": payload,
                      "prospect_id": prospect_id, "sequence_id": sequence_id}
            self._record(record)
            return SEPPushResult(ok=ok, platform=self.platform,
                                 detail=record if ok else {"error": f"HTTP {st}"})
        except Exception as exc:
            logger.warning("[outreach] push_contact failed: %r", exc)
            return SEPPushResult(ok=False, platform=self.platform, detail={"error": str(exc)})

    async def _upsert_prospect(self, email: str, payload: dict) -> str | None:
        st, body = await self._request(
            "GET", f"/api/v2/prospects?filter[emails]={urllib.parse.quote(email)}"
        )
        if st == 200 and body.get("data"):
            return str(body["data"][0].get("id"))
        attributes = {"emails": [email]}
        if payload.get("first_name"):
            attributes["firstName"] = payload["first_name"]
        if payload.get("last_name"):
            attributes["lastName"] = payload["last_name"]
        if payload.get("account"):
            attributes["company"] = payload["account"]
        st, body = await self._request("POST", "/api/v2/prospects", {
            "data": {"type": "prospect", "attributes": attributes}
        })
        return str(body.get("data", {}).get("id")) if st in (200, 201) else None

    async def _find_sequence(self, name: str) -> str | None:
        st, body = await self._request(
            "GET", f"/api/v2/sequences?filter[name]={urllib.parse.quote(name)}"
        )
        if st == 200 and body.get("data"):
            return str(body["data"][0].get("id"))
        return None

    async def test_connection(self) -> SEPTestResult:
        if not self._token:
            return SEPTestResult(ok=False, label="Outreach", detail="Not connected to Outreach.")
        try:
            st, _ = await self._request("GET", "/api/v2/prospects?page[limit]=1")
            if st == 200:
                return SEPTestResult(ok=True, label="Outreach", detail="Connected.")
            return SEPTestResult(ok=False, label="Outreach",
                                 detail=_SEP_TEST_ERRORS.get(st, f"Outreach returned HTTP {st}."))
        except Exception as exc:
            logger.warning("[outreach] test_connection failed: %r", exc)
            return SEPTestResult(ok=False, label="Outreach",
                                 detail="Could not reach Outreach. Try again shortly.")


# Two separate globals, for the same reason as nexus/ingestion/crm.py: one variable could not
# distinguish "a test installed this" from "we memoized the default", and per-tenant resolution
# needs that distinction or it would skip every stored credential.
_connector: SEPConnector | None = None
_override: SEPConnector | None = None


def build_sep_connector_from_settings() -> SEPConnector:
    """The deployment-wide default.

    There are no SEP settings, so this is the stub — deliberately, and named as such. Per-tenant
    credentials are how a workspace gets a real SEP, via ``sep_credentials.resolve_sep_connector``.
    """
    return StubSEPConnector()


def get_sep_connector() -> SEPConnector:
    """The deployment-wide connector: an installed override, else the default (memoized)."""
    global _connector
    if _connector is None:
        _connector = build_sep_connector_from_settings()
    return _connector


def set_sep_connector(connector: SEPConnector | None) -> None:
    """Install (or clear) an explicit connector — the test seam for a recording stub."""
    global _connector, _override
    _connector = connector
    _override = connector


def get_sep_connector_override() -> SEPConnector | None:
    """The deliberately installed connector, or ``None`` when merely memoized."""
    return _override
