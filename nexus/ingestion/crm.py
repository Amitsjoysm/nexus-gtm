"""CRM connectors (1st-party data). Interface + Salesforce/HubSpot adapters.

Adapters are stubs that define the integration shape: pull accounts/contacts and push updates.
Real implementations swap the ``_fetch`` bodies for authenticated API calls; the service layer
and dedupe logic are unchanged.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact


@dataclass(slots=True)
class CRMAccount:
    external_id: str
    name: str
    domain: str | None = None
    industry: str | None = None
    employee_count: int | None = None
    country: str | None = None


class CRMConnector(abc.ABC):
    source: str

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


class SalesforceConnector(CRMConnector):
    source = "salesforce"

    def __init__(self, sample: list[CRMAccount] | None = None):
        self._sample = sample or []

    async def fetch_accounts(self) -> list[CRMAccount]:
        # Real impl: SOQL via Salesforce REST. Returns injected sample for now.
        return self._sample


class HubSpotConnector(CRMConnector):
    source = "hubspot"

    def __init__(self, sample: list[CRMAccount] | None = None):
        self._sample = sample or []

    async def fetch_accounts(self) -> list[CRMAccount]:
        # Real impl: HubSpot CRM API. Returns injected sample for now.
        return self._sample
