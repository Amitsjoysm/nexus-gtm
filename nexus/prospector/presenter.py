"""Turn a provider record and what the workspace holds into a row the browser may see.

The only place a search row is built, so the masking rule has one implementation: every provider
email, phone, domain and LinkedIn URL leaves here masked; a held row additionally carries the
workspace's OWN values in ``record`` (the tenant's data, already visible on Contacts/Accounts).
"""
from __future__ import annotations

from nexus.prospector.holdings import Held
from nexus.prospector.masking import mask_domain, mask_email, mask_linkedin, mask_phone
from nexus.prospector.records import VendorCompany, VendorContact
from nexus.prospector.schemas import ProspectRowOut, SavedRecordOut


def _record(held: Held | None) -> SavedRecordOut | None:
    if held is None:
        return None
    return SavedRecordOut(kind=held.kind, id=held.record_id, account_id=held.account_id,
                          email=held.email, phone=held.phone, domain=held.domain)


def contact_row(record: VendorContact, *, key: str, held: Held | None, api_page: int,
                api_page_size: int) -> ProspectRowOut:
    return ProspectRowOut(
        row_key=key, api_page=api_page, api_page_size=api_page_size,
        saved=held is not None, record=_record(held),
        first_name=record.first_name, last_name=record.last_name, title=record.title,
        department=record.department, seniority=record.seniority,
        company_name=record.company_name, country=record.country, state=record.state,
        city=record.city,
        email_masked=mask_email(record.email), phone_masked=mask_phone(record.direct_phone),
        domain_masked=mask_domain(record.company_url),
        has_email=bool(record.email), has_direct_phone=bool(record.direct_phone),
    )


def company_row(record: VendorCompany, *, key: str, held: Held | None, api_page: int,
                api_page_size: int) -> ProspectRowOut:
    return ProspectRowOut(
        row_key=key, api_page=api_page, api_page_size=api_page_size,
        saved=held is not None, record=_record(held),
        company_name=record.company_name, country=record.country, state=record.state,
        city=record.city, industry=record.industry,
        employee_size_range=record.employee_size_range,
        domain_masked=mask_domain(record.company_url),
        linkedin_masked=mask_linkedin(record.linkedin_url),
        has_linkedin=bool(record.linkedin_url),
    )
