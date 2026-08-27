"""Pull accounts and contacts FROM a connected CRM.

The CRM connection already existed and only ever pushed *outward* — `push_account` writes
NEXUS-enriched firmographics back to HubSpot or Salesforce. `sync_accounts` could pull companies,
but there was no way for a customer to say "bring my book across", and no way to pull people at all.

Deliberately shares the upsert rules in :mod:`nexus.imports.csv_ingest` rather than reimplementing
them. A second write path would drift, and the first thing to drift would be identity — which is
how a person ends up attached to the wrong company.

**The limit is mandatory and defaulted low.** A CRM can hold a hundred thousand companies, and each
imported account enters the refresh pipeline and starts costing credits. An import that silently
pulls everything is a bill the customer did not agree to.
"""
from __future__ import annotations

import logging

from nexus.imports.csv_ingest import _apply_account, normalise_domain, normalise_email
from nexus.models.account import Account, Contact

logger = logging.getLogger("nexus.imports.crm_pull")

# What an operator gets if they name no limit. Low on purpose: an import is easy to repeat and
# impossible to un-bill.
DEFAULT_LIMIT = 100
MAX_LIMIT = 5_000


def clamp_limit(requested: int | None) -> int:
    """Bound the requested count into [1, MAX_LIMIT]."""
    if requested is None:
        return DEFAULT_LIMIT
    return max(1, min(int(requested), MAX_LIMIT))


async def import_accounts_from_crm(ts, connector, *, limit: int | None = None) -> dict:
    """Upsert up to ``limit`` accounts from the tenant's CRM.

    Identity is the normalised domain, then the CRM's own external id — NOT the company name.
    A CRM export is exactly where near-duplicate names live ("Acme", "Acme Inc", "Acme, Inc.").
    """
    limit = clamp_limit(limit)
    created = updated = skipped = 0
    errors: list[str] = []

    try:
        remote = await connector.fetch_accounts()
    except Exception as exc:  # a CRM outage must not 500 the import screen
        logger.warning("crm fetch_accounts failed: %r", exc)
        return {"created": 0, "updated": 0, "skipped": 0, "total_rows": 0,
                "errors": [f"could not read from the CRM: {exc}"]}

    remote = list(remote)[:limit]
    source = getattr(connector, "source", "crm")

    for record in remote:
        name = (getattr(record, "name", "") or "").strip()
        domain = normalise_domain(getattr(record, "domain", "") or "")
        external_id = getattr(record, "external_id", None)
        if not name and not domain:
            skipped += 1
            errors.append("a CRM record had neither a name nor a website")
            continue

        existing = None
        if domain:
            existing = await ts.first(Account, Account.domain == domain)
        if existing is None and external_id:
            existing = await ts.first(
                Account, Account.crm_id == str(external_id), Account.crm_source == source
            )
        if existing is None and name:
            existing = await ts.first(Account, Account.name == name)

        fields = {
            "name": name,
            "domain": domain,
            "industry": getattr(record, "industry", None) or "",
            "country": getattr(record, "country", None) or "",
            "employee_count": str(getattr(record, "employee_count", "") or ""),
        }

        if existing is None:
            account = Account(tenant_id=ts.tenant_id, name=name or domain, source=f"crm:{source}")
            _apply_account(account, fields, {})
            account.crm_id = str(external_id) if external_id else None
            account.crm_source = source
            ts.add(account)
            created += 1
        else:
            _apply_account(existing, fields, {})
            # Record the CRM link on a row we already had, so a later push updates rather than
            # creating a duplicate on the CRM side.
            if external_id and not existing.crm_id:
                existing.crm_id = str(external_id)
                existing.crm_source = source
            updated += 1
        await ts.flush()

    return {"created": created, "updated": updated, "skipped": skipped,
            "total_rows": len(remote), "errors": errors[:50]}


async def import_contacts_from_crm(ts, connector, *, limit: int | None = None) -> dict:
    """Upsert up to ``limit`` contacts from the tenant's CRM.

    Attachment order is deliberate: the CRM's own account domain, then the account NAME as the CRM
    spells it, then the email's domain. Falling straight to the email domain would put every
    contact at an agency, a subsidiary or a personal address onto the wrong company — and a person
    attached to the wrong company is a rep calling a stranger with someone else's context.
    """
    limit = clamp_limit(limit)
    created = updated = skipped = 0
    errors: list[str] = []

    try:
        remote = await connector.fetch_contacts(limit=limit)
    except Exception as exc:
        logger.warning("crm fetch_contacts failed: %r", exc)
        return {"created": 0, "updated": 0, "skipped": 0, "total_rows": 0,
                "errors": [f"could not read contacts from the CRM: {exc}"]}

    remote = list(remote)[:limit]
    source = getattr(connector, "source", "crm")

    for record in remote:
        email = normalise_email(getattr(record, "email", "") or "")
        full_name = (getattr(record, "full_name", "") or "").strip()
        if not email and not full_name:
            skipped += 1
            errors.append("a CRM contact had neither a name nor an email")
            continue

        account = None
        crm_domain = normalise_domain(getattr(record, "account_domain", "") or "")
        crm_account_name = (getattr(record, "account_name", "") or "").strip()
        if crm_domain:
            account = await ts.first(Account, Account.domain == crm_domain)
        if account is None and crm_account_name:
            account = await ts.first(Account, Account.name == crm_account_name)
        if account is None and email:
            account = await ts.first(Account, Account.domain == normalise_domain(email.split("@")[-1]))

        if account is None:
            fallback_domain = crm_domain or (normalise_domain(email.split("@")[-1]) if email else "")
            if not fallback_domain and not crm_account_name:
                skipped += 1
                errors.append(f"{full_name or email}: no company could be resolved")
                continue
            account = Account(
                tenant_id=ts.tenant_id,
                name=crm_account_name or fallback_domain,
                source=f"crm:{source}",
            )
            if fallback_domain:
                account.domain = fallback_domain
            ts.add(account)
            await ts.flush()

        existing = await ts.first(Contact, Contact.email == email) if email else None
        if existing is None:
            contact = Contact(
                tenant_id=ts.tenant_id,
                account_id=account.id,
                full_name=full_name or (email.split("@")[0] if email else "unknown"),
                email=email or None,
            )
            contact.title = getattr(record, "title", None) or None
            contact.phone = getattr(record, "phone", None) or None
            contact.enrichment_source = f"crm:{source}"
            ts.add(contact)
            created += 1
        else:
            # A blank CRM field never overwrites, matching the CSV import: a sparsely-filled CRM
            # record must not erase what enrichment already established.
            if full_name:
                existing.full_name = full_name
            if getattr(record, "title", None):
                existing.title = record.title
            if getattr(record, "phone", None):
                existing.phone = record.phone
            updated += 1
        await ts.flush()

    return {"created": created, "updated": updated, "skipped": skipped,
            "total_rows": len(remote), "errors": errors[:50]}
