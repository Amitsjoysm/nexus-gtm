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
import logging
from dataclasses import dataclass, field

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


class HubSpotConnector(CRMConnector):
    source = "hubspot"

    def __init__(self, sample: list[CRMAccount] | None = None):
        super().__init__()
        self._sample = sample or []

    async def fetch_accounts(self) -> list[CRMAccount]:
        # Real impl: HubSpot CRM API. Returns injected sample for now.
        return self._sample


_connector: CRMConnector | None = None


def build_crm_connector_from_settings() -> CRMConnector:
    """Resolve the outbound CRM connector from ``NEXUS_CRM_PROVIDER`` (default: stub)."""
    from nexus.core.config import get_settings

    key = (get_settings().crm_provider or "").strip().lower()
    if key == "salesforce":
        return SalesforceConnector()
    if key == "hubspot":
        return HubSpotConnector()
    return StubCRMConnector()


def get_crm_connector() -> CRMConnector:
    global _connector
    if _connector is None:
        _connector = build_crm_connector_from_settings()
    return _connector


def set_crm_connector(connector: CRMConnector | None) -> None:
    global _connector
    _connector = connector
