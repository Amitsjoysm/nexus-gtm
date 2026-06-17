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
from dataclasses import dataclass, field
from datetime import datetime, timezone

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact

logger = logging.getLogger("nexus.integrations.crm")


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

    # -- routing ----------------------------------------------------------------------
    async def fetch_owners(self) -> list[CRMOwner]:
        """Pull account→owner mappings for routing. Real adapters override; stub: none."""
        return []


class StubCRMConnector(CRMConnector):
    """Zero-network default: no inbound accounts, recording outbound. For tests/CI."""

    source = "stub"

    async def fetch_accounts(self) -> list[CRMAccount]:
        return []


class SalesforceConnector(CRMConnector):
    source = "salesforce"

    def __init__(self, sample: list[CRMAccount] | None = None):
        super().__init__()
        self._sample = sample or []

    async def fetch_accounts(self) -> list[CRMAccount]:
        # Real impl: SOQL via Salesforce REST. Returns injected sample for now.
        return self._sample


def _split_name(full_name: str | None) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


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

    def __init__(self, access_token: str = "", *, api_base: str = "https://api.hubapi.com"):
        super().__init__()
        self._token = (access_token or "").strip()
        self._base = api_base.rstrip("/")

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

    async def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        """One HubSpot API call off the event loop, with a single retry on 429 (rate limit)."""
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
            "Synced from NEXUS GTM.",
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
            note_body = f"NEXUS · {kind}: {body}"
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


_connector: CRMConnector | None = None


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
    global _connector
    if _connector is None:
        _connector = build_crm_connector_from_settings()
    return _connector


def set_crm_connector(connector: CRMConnector | None) -> None:
    global _connector
    _connector = connector
