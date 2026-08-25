"""CRM connectors (1st-party data) — bi-directional. Interface + Salesforce/HubSpot adapters.

The connector is the single seam between NEXUS and the customer's system of record. It moves data
both ways:

  * **Inbound** — :meth:`fetch_accounts` / :meth:`sync_accounts` pull CRM accounts and upsert them,
    keyed by ``(tenant, crm_source, crm_id)``. (Shipped.)
  * **Outbound** — :meth:`push_account` writes NEXUS-enriched firmographics + contacts back, and
    :meth:`push_activity` logs an engagement event (outreach sent, signal detected). Offline these
    *record* the intent (like the SEP stub) so the round-trip is provable with zero network; real
    adapters swap the recording body for an authenticated API call without changing this surface.
  * **Routing** — :meth:`fetch_owners` pulls account→owner mappings so leads route to the right rep.
    Interface declared now; the routing application (owner→user mapping) lands in a later phase.

Adapters never raise across the boundary on the outbound path: a failed push is reported, never
surfaced, so a flaky CRM can't break a play or a send.
"""
from __future__ import annotations

import abc
import asyncio
import json
import logging
import urllib.error
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact

logger = logging.getLogger("nexus.integrations.crm")

# An async callback returning a fresh access token, or "" when refresh is impossible. Supplied by
# the per-tenant resolver for OAuth-backed connections; ``None`` for a pasted token.
TokenProvider = Optional[Callable[[], Awaitable[str]]]


@dataclass(slots=True)
class CRMAccount:
    external_id: str
    name: str
    domain: str | None = None
    industry: str | None = None
    employee_count: int | None = None
    country: str | None = None


@dataclass(slots=True)
class CRMPushResult:
    ok: bool
    source: str
    external_id: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass(slots=True)
class CRMTestResult:
    """The outcome of verifying a credential. ``label`` is a friendly identity for the UI
    ("HubSpot portal 12345678"); ``detail`` is a human-readable reason, safe to show a user."""

    ok: bool
    label: str = ""
    detail: str = ""


@dataclass(slots=True)
class CRMOwner:
    """An account→owner mapping pulled from the CRM, used for lead routing."""

    external_id: str
    owner_email: str
    owner_name: str | None = None


def _contact_payload(contact: Contact) -> dict:
    return {
        "full_name": contact.full_name,
        "title": contact.title,
        "email": contact.email,
        "email_status": contact.email_status,
        "email_confidence": contact.email_confidence,
    }


class CRMConnector(abc.ABC):
    source: str

    # The connector is a long-lived module singleton, and with auto-sync on it pushes
    # continuously — unbounded recording buffers would grow for the life of the worker
    # process. Keep only the most recent pushes (plain lists, trimmed in place, so tests
    # can keep asserting list equality).
    MAX_RECORDED_PUSHES = 1000

    def __init__(self) -> None:
        # Recording buffers make the outbound half provable offline (mirrors the SEP stub).
        # Real adapters still populate these for observability before/after the API call.
        self.pushed_accounts: list[dict] = []
        self.pushed_activities: list[dict] = []

    def _record(self, buffer: list[dict], record: dict) -> None:
        buffer.append(record)
        if len(buffer) > self.MAX_RECORDED_PUSHES:
            del buffer[: len(buffer) - self.MAX_RECORDED_PUSHES]

    @abc.abstractmethod
    async def fetch_accounts(self) -> list[CRMAccount]: ...

    async def sync_accounts(self, ts: TenantSession) -> list[Account]:
        """Upsert CRM accounts into NEXUS, keyed by (tenant, crm_source, crm_id)."""
        remote = await self.fetch_accounts()
        existing = {
            a.crm_id: a
            for a in await ts.list(Account, Account.crm_source == self.source)
            if a.crm_id
        }
        out: list[Account] = []
        for r in remote:
            acc = existing.get(r.external_id)
            if acc is None:
                acc = Account(tenant_id=ts.tenant_id, name=r.name, crm_id=r.external_id,
                              crm_source=self.source)
                ts.add(acc)
            acc.name = r.name
            acc.domain = r.domain or acc.domain
            acc.industry = r.industry or acc.industry
            acc.employee_count = r.employee_count or acc.employee_count
            acc.country = r.country or acc.country
            out.append(acc)
        await ts.flush()
        return out

    # -- outbound ---------------------------------------------------------------------
    async def push_account(
        self, account: Account, *, contacts: list[Contact] | None = None
    ) -> CRMPushResult:
        """Write NEXUS-enriched firmographics + contacts back to the CRM."""
        record = {
            "account_id": account.id,
            "crm_id": account.crm_id,
            "name": account.name,
            "domain": account.domain,
            "industry": account.industry,
            "employee_count": account.employee_count,
            "country": account.country,
            "contacts": [_contact_payload(c) for c in (contacts or [])],
        }
        self._record(self.pushed_accounts, record)
        logger.info("[%s] push account %s (%s)", self.source, account.name, account.id)
        return CRMPushResult(
            ok=True, source=self.source, external_id=account.crm_id, detail=record
        )

    async def push_activity(
        self, *, account_id: str | None, kind: str, detail: dict | None = None
    ) -> CRMPushResult:
        """Log an engagement event (e.g. ``outreach_sent``, ``signal``) against an account."""
        record = {"account_id": account_id, "kind": kind, "detail": detail or {}}
        self._record(self.pushed_activities, record)
        logger.info("[%s] push activity %s for account %s", self.source, kind, account_id)
        return CRMPushResult(
            ok=True, source=self.source, external_id=account_id, detail=record
        )

    # -- connection health -------------------------------------------------------------
    async def test_connection(self) -> CRMTestResult:
        """Verify the credential works. Like every connector method, never raises across the
        boundary — a failure is a result the caller can render, not an exception."""
        return CRMTestResult(ok=True, label=self.source, detail="Offline stub connector.")

    # -- routing ----------------------------------------------------------------------
    async def fetch_owners(self) -> list[CRMOwner]:
        """Pull account→owner mappings for routing. Real adapters override; stub: none."""
        return []


class StubCRMConnector(CRMConnector):
    """Zero-network default: no inbound accounts, recording outbound. For tests/CI."""

    source = "stub"

    async def fetch_accounts(self) -> list[CRMAccount]:
        return []


_SF_API_VERSION = "v59.0"

_SALESFORCE_TEST_ERRORS = {
    401: "Session expired or invalid — reconnect Salesforce.",
    403: "The connected app is missing the 'api' scope, or the user lacks API access.",
    429: "Salesforce API request limit reached — try again shortly.",
}


def _domain_from_website(website: str | None) -> str | None:
    """Salesforce stores a full URL in ``Website``; NEXUS keys accounts on a bare domain."""
    if not website:
        return None
    raw = website.strip()
    if not raw:
        return None
    if "//" not in raw:
        raw = "https://" + raw
    host = (urllib.parse.urlparse(raw).hostname or "").lower()
    return host[4:] if host.startswith("www.") else (host or None)


def _soql_escape(value: str) -> str:
    """Escape a SOQL string literal.

    Not decorative: an account domain is customer-supplied text that ends up inside a quoted SOQL
    literal, and a stray quote there is injection into a query against the customer's own CRM.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _soql_like(value: str) -> str:
    """Escape a value that will sit inside a SOQL LIKE pattern.

    :func:`_soql_escape` handles the quote, which is the injection. This handles the two characters
    that are *wildcards* inside LIKE: ``%`` matches any run and ``_`` matches any single character.
    Neither is dangerous, but both are wrong — a domain containing an underscore silently matches
    accounts it should not, and the caller is resolving which of the customer's accounts to write
    to. Matching the wrong one pushes a contact onto somebody else's record.

    Carried over from the deployment-global Salesforce connector that was not merged; the hardening
    was the one thing that branch had and this one did not.
    """
    return _soql_escape(value).replace("%", "\\%").replace("_", "\\_")


class SalesforceConnector(CRMConnector):
    """Real Salesforce CRM connector (OAuth2 + REST).

    Addressed at the org's own ``instance_url``, which only the token response carries — there is
    no single host for Salesforce the way there is for HubSpot, so a connector without it cannot
    send a request anywhere. Mirrors HubSpot's posture exactly: idempotent on natural keys
    (Account by ``Website`` domain, Contact by ``Email``), and never raises across the boundary.

    ``sample`` keeps the historical injected-rows behaviour for the offline ``/crm/sync`` path and
    the tests that predate a real adapter; with a token it is ignored in favour of live data.
    """

    source = "salesforce"

    def __init__(
        self,
        sample: list[CRMAccount] | None = None,
        *,
        access_token: str = "",
        instance_url: str = "",
        token_provider: "TokenProvider" = None,
    ):
        super().__init__()
        self._sample = sample or []
        self._token = (access_token or "").strip()
        self._base = (instance_url or "").rstrip("/")
        self._token_provider = token_provider
        self._refresh_lock = asyncio.Lock()

    @property
    def _live(self) -> bool:
        return bool(self._token and self._base)

    # -- HTTP -------------------------------------------------------------------------
    def _request_blocking(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode()
                return r.status, (json.loads(text) if text else {})
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode())
            except Exception:
                return e.code, {"message": str(e)}
            # Salesforce returns a LIST of errors; unwrap so callers see a dict like every
            # other adapter rather than having to special-case this one.
            if isinstance(payload, list):
                payload = payload[0] if payload else {}
            return e.code, payload

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

    def _data(self, path: str) -> str:
        return f"/services/data/{_SF_API_VERSION}{path}"

    async def _query(self, soql: str) -> tuple[int, dict]:
        return await self._request(
            "GET", self._data("/query/?" + urllib.parse.urlencode({"q": soql}))
        )

    # -- inbound ----------------------------------------------------------------------
    async def fetch_accounts(self) -> list[CRMAccount]:
        if not self._live:
            return self._sample
        out: list[CRMAccount] = []
        try:
            st, body = await self._query(
                "SELECT Id, Name, Website, Industry, NumberOfEmployees, BillingCountry "
                "FROM Account ORDER BY LastModifiedDate DESC LIMIT 200"
            )
            if st != 200:
                logger.warning("[salesforce] fetch_accounts HTTP %s", st)
                return []
            for row in body.get("records", []):
                if not row.get("Name"):
                    continue
                out.append(CRMAccount(
                    external_id=row.get("Id"),
                    name=row["Name"],
                    domain=_domain_from_website(row.get("Website")),
                    industry=row.get("Industry"),
                    country=row.get("BillingCountry"),
                    employee_count=(
                        int(row["NumberOfEmployees"]) if row.get("NumberOfEmployees") else None
                    ),
                ))
        except Exception as exc:
            logger.warning("[salesforce] fetch_accounts failed: %r", exc)
        return out

    # -- account resolution ------------------------------------------------------------
    async def _find_account_by_domain(self, domain: str) -> str | None:
        st, body = await self._query(
            "SELECT Id FROM Account WHERE Website LIKE "
            f"'%{_soql_like(domain)}%' LIMIT 1"
        )
        if st == 200 and body.get("records"):
            return body["records"][0].get("Id")
        return None

    def _account_fields(self, account: Account) -> dict:
        fields = {
            "Name": account.name,
            "Website": account.domain or "",
            "Industry": account.industry or "",
            "BillingCountry": account.country or "",
        }
        if account.employee_count:
            fields["NumberOfEmployees"] = int(account.employee_count)
        return {k: v for k, v in fields.items() if v not in (None, "")}

    async def _upsert_account(self, account: Account) -> tuple[str | None, str]:
        fields = self._account_fields(account)
        sf_id = account.crm_id if account.crm_source == self.source else None
        if not sf_id and account.domain:
            sf_id = await self._find_account_by_domain(account.domain)
        if sf_id:
            st, _ = await self._request(
                "PATCH", self._data(f"/sobjects/Account/{sf_id}"), fields
            )
            # Salesforce answers a successful PATCH with 204 No Content, not 200.
            return (sf_id, "updated") if st in (200, 204) else (None, "failed")
        st, body = await self._request("POST", self._data("/sobjects/Account"), fields)
        if st in (200, 201) and body.get("id"):
            return body["id"], "created"
        return None, "failed"

    async def _upsert_contact(self, contact: Contact, account_id: str) -> str | None:
        if not contact.email:
            return None
        first, last = _split_name(contact.full_name)
        fields = {
            "Email": contact.email,
            # LastName is required on Contact; fall back to the email local part so a
            # first-name-only record still syncs rather than being silently dropped.
            "LastName": last or first or contact.email.split("@")[0],
            "AccountId": account_id,
        }
        if last and first:
            fields["FirstName"] = first
        if contact.title:
            fields["Title"] = contact.title
        # Upsert on the standard Email field is not available, so resolve then create/update.
        st, body = await self._query(
            f"SELECT Id FROM Contact WHERE Email = '{_soql_escape(contact.email)}' LIMIT 1"
        )
        existing = body["records"][0]["Id"] if st == 200 and body.get("records") else None
        if existing:
            st, _ = await self._request(
                "PATCH", self._data(f"/sobjects/Contact/{existing}"), fields
            )
            return existing if st in (200, 204) else None
        st, body = await self._request("POST", self._data("/sobjects/Contact"), fields)
        return body.get("id") if st in (200, 201) else None

    # -- outbound ---------------------------------------------------------------------
    async def push_account(
        self, account: Account, *, contacts: list[Contact] | None = None
    ) -> CRMPushResult:
        if not self._live:
            return CRMPushResult(
                ok=False, source=self.source,
                detail={"error": "Salesforce is not connected (no token or instance URL)"},
            )
        try:
            sf_id, action = await self._upsert_account(account)
            if not sf_id:
                return CRMPushResult(
                    ok=False, source=self.source,
                    detail={"error": "account upsert failed", "account": account.name},
                )
            synced = 0
            for c in contacts or []:
                if await self._upsert_contact(c, sf_id):
                    synced += 1
            record = {
                "account_id": account.id, "crm_id": sf_id, "name": account.name,
                "action": action, "contacts_synced": synced,
            }
            self._record(self.pushed_accounts, record)
            logger.info("[salesforce] %s account %s (%s) + %d contacts",
                        action, account.name, sf_id, synced)
            return CRMPushResult(ok=True, source=self.source, external_id=sf_id, detail=record)
        except Exception as exc:  # never break a play/send on a CRM error
            logger.warning("[salesforce] push_account %s failed: %r", account.name, exc)
            return CRMPushResult(ok=False, source=self.source, detail={"error": str(exc)})

    async def push_activity(
        self, *, account_id: str | None, kind: str, detail: dict | None = None
    ) -> CRMPushResult:
        if not self._live or not account_id:
            return CRMPushResult(
                ok=False, source=self.source, detail={"error": "missing token/instance/id"}
            )
        try:
            d = detail or {}
            subject = f"InfoJoy · {kind}: {d.get('signal') or d.get('subject') or kind}"
            st, body = await self._request("POST", self._data("/sobjects/Task"), {
                "Subject": subject[:255],
                "Status": "Completed",
                "ActivityDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                # WhatId links the Task to the Account; only a real Salesforce id is valid, so a
                # leaked NEXUS account id is dropped rather than rejected by the API.
                **({"WhatId": account_id} if len(account_id) in (15, 18) else {}),
            })
            task_id = body.get("id") if st in (200, 201) else None
            self._record(self.pushed_activities,
                         {"account_id": account_id, "kind": kind, "task": task_id})
            return CRMPushResult(ok=bool(task_id), source=self.source,
                                 external_id=task_id, detail={"kind": kind})
        except Exception as exc:
            logger.warning("[salesforce] push_activity failed: %r", exc)
            return CRMPushResult(ok=False, source=self.source, detail={"error": str(exc)})

    # -- connection health -------------------------------------------------------------
    async def test_connection(self) -> CRMTestResult:
        if not self._token:
            return CRMTestResult(ok=False, label="Salesforce", detail="No access token configured.")
        if not self._base:
            return CRMTestResult(
                ok=False, label="Salesforce",
                detail="No instance URL — reconnect Salesforce so we learn your org's host.",
            )
        try:
            st, body = await self._request("GET", self._data("/limits"))
            if st == 200:
                host = urllib.parse.urlparse(self._base).hostname or "Salesforce"
                return CRMTestResult(ok=True, label=f"Salesforce · {host}", detail="Connected.")
            return CRMTestResult(
                ok=False, label="Salesforce",
                detail=_SALESFORCE_TEST_ERRORS.get(st, f"Salesforce returned HTTP {st}."),
            )
        except Exception as exc:
            logger.warning("[salesforce] test_connection failed: %r", exc)
            return CRMTestResult(
                ok=False, label="Salesforce",
                detail="Could not reach Salesforce. Try again shortly.",
            )


def _split_name(full_name: str | None) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


_HUBSPOT_TEST_ERRORS = {
    401: "Invalid or expired access token.",
    403: "Token is missing required scopes (crm.objects.companies.read/write).",
    429: "HubSpot rate limit reached — try again shortly.",
}


class HubSpotConnector(CRMConnector):
    """Real HubSpot CRM connector (private-app token).

    Outbound is idempotent on natural keys so re-syncs never duplicate: companies upsert by
    ``domain`` (or the stored HubSpot id), contacts upsert by ``email``, then contacts associate
    to their company. ``push_activity`` writes a timestamped Note against the company. Never
    raises across the boundary — every method returns a :class:`CRMPushResult`, so a flaky CRM
    or a missing scope degrades to a recorded failure (the account stays due for retry) rather
    than breaking a play or a send.
    """

    source = "hubspot"

    def __init__(
        self,
        access_token: str = "",
        *,
        api_base: str = "https://api.hubapi.com",
        token_provider: "TokenProvider" = None,
    ):
        super().__init__()
        self._token = (access_token or "").strip()
        self._base = api_base.rstrip("/")
        # Set for an OAuth-backed connection; ``None`` for a pasted private-app token, which
        # cannot be refreshed. Keeping it optional means the manual path never pays for the
        # OAuth path's machinery, and a 401 on a pasted token still reads as "invalid token"
        # rather than as a confusing refresh failure.
        self._token_provider = token_provider
        self._refresh_lock = asyncio.Lock()

    # -- HTTP -------------------------------------------------------------------------
    def _request_blocking(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
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
        """Renew the access token once, under a lock. True when a new token was installed.

        The lock is the point: a burst of pushes would otherwise each see a 401 and each mint a
        new token, and HubSpot invalidates the earlier ones — so the first requests in the burst
        would start failing with a token that was valid when they read it. Whoever holds the lock
        refreshes; everyone behind them finds the new token already in place and skips the call.
        """
        if self._token_provider is None:
            return False
        before = self._token
        async with self._refresh_lock:
            if self._token != before:
                return True  # someone else refreshed while we waited
            token = await self._token_provider()
            if not token:
                return False
            self._token = token
            return True

    async def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        """One HubSpot API call off the event loop.

        Retries once on 429 (rate limit) and once on 401 after refreshing an OAuth token — an
        access token lives 30 minutes, so a long-running worker will meet expiry mid-sweep.
        """
        status, payload = await asyncio.to_thread(self._request_blocking, method, path, body)
        if status == 401 and await self._refresh_token():
            status, payload = await asyncio.to_thread(self._request_blocking, method, path, body)
        if status == 429:
            await asyncio.sleep(1.0)
            status, payload = await asyncio.to_thread(self._request_blocking, method, path, body)
        return status, payload

    # -- company resolution ------------------------------------------------------------
    async def _find_company_by_domain(self, domain: str) -> str | None:
        st, body = await self._request(
            "POST",
            "/crm/v3/objects/companies/search",
            {
                "filterGroups": [
                    {"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}
                ],
                "properties": ["domain"],
                "limit": 1,
            },
        )
        if st == 200 and body.get("results"):
            return body["results"][0].get("id")
        return None

    def _company_properties(self, account: Account) -> dict:
        tech = ", ".join(account.tech_stack or []) if getattr(account, "tech_stack", None) else ""
        desc_bits = [b for b in (
            f"Industry: {account.industry}" if account.industry else "",
            f"Tech: {tech}" if tech else "",
            "Synced from InfoJoy GTM.",
        ) if b]
        props = {
            "name": account.name,
            "domain": account.domain or "",
            "country": account.country or "",
            "description": " ".join(desc_bits),
        }
        if account.employee_count:
            props["numberofemployees"] = str(account.employee_count)
        return {k: v for k, v in props.items() if v not in (None, "")}

    async def _upsert_company(self, account: Account) -> tuple[str | None, str]:
        """Return (company_id, action) where action is created|updated|failed."""
        props = self._company_properties(account)
        company_id = account.crm_id if account.crm_source == self.source else None
        if not company_id and account.domain:
            company_id = await self._find_company_by_domain(account.domain)
        if company_id:
            st, body = await self._request(
                "PATCH", f"/crm/v3/objects/companies/{company_id}", {"properties": props}
            )
            return (company_id, "updated") if st == 200 else (None, "failed")
        st, body = await self._request("POST", "/crm/v3/objects/companies", {"properties": props})
        if st in (200, 201):
            return body.get("id"), "created"
        return None, "failed"

    async def _upsert_contact(self, contact: Contact) -> str | None:
        if not contact.email:
            return None
        first, last = _split_name(contact.full_name)
        props = {"email": contact.email}
        if first:
            props["firstname"] = first
        if last:
            props["lastname"] = last
        if contact.title:
            props["jobtitle"] = contact.title
        st, body = await self._request(
            "POST",
            "/crm/v3/objects/contacts/batch/upsert",
            {"inputs": [{"idProperty": "email", "id": contact.email, "properties": props}]},
        )
        if st in (200, 201) and body.get("results"):
            return body["results"][0].get("id")
        return None

    async def _associate(self, from_type: str, from_id: str, to_type: str, to_id: str) -> None:
        await self._request(
            "PUT", f"/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}"
        )

    # -- outbound ---------------------------------------------------------------------
    async def push_account(
        self, account: Account, *, contacts: list[Contact] | None = None
    ) -> CRMPushResult:
        if not self._token:
            return CRMPushResult(ok=False, source=self.source, detail={"error": "no access token"})
        try:
            company_id, action = await self._upsert_company(account)
            if not company_id:
                return CRMPushResult(
                    ok=False, source=self.source,
                    detail={"error": "company upsert failed", "account": account.name},
                )
            synced_contacts = 0
            for c in contacts or []:
                contact_id = await self._upsert_contact(c)
                if contact_id:
                    await self._associate("contacts", contact_id, "companies", company_id)
                    synced_contacts += 1
            record = {
                "account_id": account.id, "crm_id": company_id, "name": account.name,
                "action": action, "contacts_synced": synced_contacts,
            }
            self._record(self.pushed_accounts, record)
            logger.info("[hubspot] %s company %s (%s) + %d contacts",
                        action, account.name, company_id, synced_contacts)
            return CRMPushResult(ok=True, source=self.source, external_id=company_id, detail=record)
        except Exception as exc:  # never break a play/send on a CRM error
            logger.warning("[hubspot] push_account %s failed: %r", account.name, exc)
            return CRMPushResult(ok=False, source=self.source, detail={"error": str(exc)})

    async def push_activity(
        self, *, account_id: str | None, kind: str, detail: dict | None = None
    ) -> CRMPushResult:
        if not self._token or not account_id:
            return CRMPushResult(ok=False, source=self.source, detail={"error": "missing token/id"})
        try:
            d = detail or {}
            body = d.get("signal") or d.get("subject") or kind
            note_body = f"InfoJoy · {kind}: {body}"
            ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            st, note = await self._request(
                "POST", "/crm/v3/objects/notes",
                {"properties": {"hs_note_body": note_body, "hs_timestamp": ts_iso}},
            )
            note_id = note.get("id") if st in (200, 201) else None
            # account_id here is the HubSpot company id (crm_id) the sync service passes; only a
            # numeric HubSpot id can be associated (skip if a NEXUS account id leaked through).
            if note_id and account_id and account_id.isdigit():
                await self._associate("notes", note_id, "companies", account_id)
            self._record(self.pushed_activities, {"account_id": account_id, "kind": kind, "note": note_id})
            return CRMPushResult(ok=bool(note_id), source=self.source, external_id=note_id, detail={"kind": kind})
        except Exception as exc:
            logger.warning("[hubspot] push_activity failed: %r", exc)
            return CRMPushResult(ok=False, source=self.source, detail={"error": str(exc)})

    # -- inbound ----------------------------------------------------------------------
    async def fetch_accounts(self) -> list[CRMAccount]:
        """Pull HubSpot companies so a sync can also bring CRM accounts into NEXUS."""
        if not self._token:
            return []
        out: list[CRMAccount] = []
        try:
            st, body = await self._request(
                "GET",
                "/crm/v3/objects/companies?limit=100&properties=name,domain,industry,"
                "numberofemployees,country",
            )
            if st != 200:
                return []
            for row in body.get("results", []):
                p = row.get("properties", {})
                if not p.get("name"):
                    continue
                emp = p.get("numberofemployees")
                out.append(CRMAccount(
                    external_id=row.get("id"), name=p["name"], domain=p.get("domain"),
                    industry=p.get("industry"), country=p.get("country"),
                    employee_count=int(emp) if emp and str(emp).isdigit() else None,
                ))
        except Exception as exc:
            logger.warning("[hubspot] fetch_accounts failed: %r", exc)
        return out

    # -- connection health -------------------------------------------------------------
    async def test_connection(self) -> CRMTestResult:
        """Verify the private-app token.

        Prefers ``/account-info/v3/details`` for a friendly portal label, but that endpoint needs
        the ``oauth`` scope, which many private apps do not grant. On 403 we retry against the
        companies scope we actually require for syncing, so a correctly-scoped token still
        reports connected.
        """
        if not self._token:
            return CRMTestResult(ok=False, label="HubSpot", detail="No access token configured.")
        try:
            st, body = await self._request("GET", "/account-info/v3/details")
            if st == 200:
                portal = body.get("portalId")
                return CRMTestResult(
                    ok=True,
                    label=f"HubSpot portal {portal}" if portal else "HubSpot",
                    detail="Connected.",
                )
            if st == 403:
                st_fallback, _ = await self._request("GET", "/crm/v3/objects/companies?limit=1")
                if st_fallback == 200:
                    return CRMTestResult(ok=True, label="HubSpot", detail="Connected.")
                st = st_fallback
            return CRMTestResult(
                ok=False,
                label="HubSpot",
                detail=_HUBSPOT_TEST_ERRORS.get(st, f"HubSpot returned HTTP {st}."),
            )
        except Exception as exc:
            # Log the cause for operators; show the user a message that cannot leak internals.
            logger.warning("[hubspot] test_connection failed: %r", exc)
            return CRMTestResult(
                ok=False, label="HubSpot", detail="Could not reach HubSpot. Try again shortly."
            )


# The deployment-wide connector. Two *separate* globals on purpose:
#   _connector — the memoized env-configured instance (what get_crm_connector() returns)
#   _override  — an instance installed deliberately via set_crm_connector()
# They used to be one variable, which made "is an override installed?" unanswerable: after any
# call to get_crm_connector() on an env-configured deployment the single global was non-None.
# Per-tenant resolution needs that distinction — see crm_credentials.resolve_crm_connector.
_connector: CRMConnector | None = None
_override: CRMConnector | None = None


def build_crm_connector_from_settings() -> CRMConnector:
    """Resolve the outbound CRM connector from ``NEXUS_CRM_PROVIDER`` (default: stub)."""
    from nexus.core.config import get_settings

    settings = get_settings()
    key = (settings.crm_provider or "").strip().lower()
    if key == "salesforce":
        return SalesforceConnector()
    if key == "hubspot":
        return HubSpotConnector(
            access_token=settings.hubspot_access_token, api_base=settings.hubspot_api_base
        )
    return StubCRMConnector()


def get_crm_connector() -> CRMConnector:
    """The deployment-wide connector: an installed override, else the env-configured one.

    This is the *fallback*. Per-tenant resolution lives in
    :func:`nexus.ingestion.crm_credentials.resolve_crm_connector`; call that from request and
    worker paths so each tenant syncs to its own CRM.
    """
    global _connector
    if _connector is None:
        _connector = build_crm_connector_from_settings()
    return _connector


def set_crm_connector(connector: CRMConnector | None) -> None:
    """Install (or clear) an explicit connector — the test seam for a recording stub.

    Sets both globals so ``get_crm_connector()`` returns it *and* per-tenant resolution knows it
    was installed deliberately. ``None`` clears both, so the next ``get_crm_connector()`` rebuilds
    from settings.
    """
    global _connector, _override
    _connector = connector
    _override = connector


def get_crm_connector_override() -> CRMConnector | None:
    """The deliberately installed connector, or ``None`` when the module has merely memoized the
    env-configured instance — the distinction ``get_crm_connector()`` cannot make."""
    return _override
