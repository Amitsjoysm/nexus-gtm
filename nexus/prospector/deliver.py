"""Take selected records into the workspace: save, list, reveal, or list their companies.

The order is the safety (spec §9.2):

1. **Resolve** the selection from the shared cache (platform writes only, before any tenant write).
2. **Quote** it: which fields are new to this workspace (``pricing``).
3. **Preflight** the whole quote: a 402 before anything is bought or written.
4. **One savepoint** for the records, the list items, the ledger and the charge. A charge refused at
   the last moment takes the records with it: nothing bought, nothing charged, nothing half-written.

Actions (D8, D24): ``save`` and ``reveal`` (one row) create the records; ``list`` creates them and
puts them on a list of the matching kind; ``company_list`` (Contacts view) puts the people's
companies on an account list, free. A held row is linked, never rewritten.
"""
from __future__ import annotations

import logging

from nexus.lists import service as lists
from nexus.prospector import charges
from nexus.prospector.errors import ProspectorError
from nexus.prospector.fetch import record_browse_cost
from nexus.prospector.pricing import Line, Quote, quote
from nexus.prospector.records import VendorCompany
from nexus.prospector.schemas import DeliverOut
from nexus.prospector.selection import Selection, resolve
from nexus.prospector.writers import upsert_account, upsert_contact

logger = logging.getLogger("nexus.prospector.deliver")

ACTIONS = ("save", "list", "reveal", "company_list")


async def _target_list(ts, *, view: str, action: str, list_id: str | None, user_id: str,
                       role: str):
    if action not in ("list", "company_list"):
        return None
    if not list_id:
        raise ProspectorError(422, "Choose a list to add to.")
    plist = await lists.get_live(ts, list_id)
    if plist is None:
        raise ProspectorError(404, "That list does not exist.")
    wanted = "contact" if (view == "contacts" and action == "list") else "account"
    if plist.kind != wanted:
        raise ProspectorError(
            422, f"This needs {'a contact' if wanted == 'contact' else 'an account'} list.")
    if not lists.can_edit(plist, user_id=user_id, role=role):
        raise ProspectorError(403, "Only the list's owner or a manager can add to it.")
    return plist


def _validate(selection: Selection, action: str) -> None:
    if action not in ACTIONS:
        raise ProspectorError(422, f"Unknown action {action!r}.")
    if action == "company_list" and selection.view != "contacts":
        raise ProspectorError(422, "Adding people's companies is an action of the Contacts view.")
    if action == "reveal" and (selection.all_matching or len(selection.rows) != 1):
        raise ProspectorError(422, "Reveal takes exactly one row.")


async def prepare(ts, *, selection: Selection, action: str, include_phones: bool,
                  client=None) -> tuple[Quote, int]:
    """Resolve and quote. ``(quote, vendor_rows_bought)``. Writes nothing in the tenant."""
    resolved = await resolve(ts, selection, client=client)
    q = await quote(ts, view=selection.view, action=action, resolved=resolved,
                    include_phones=include_phones)
    return q, resolved.vendor_rows


async def deliver(ts, *, user_id: str, role: str, selection: Selection, action: str,
                  list_id: str | None, include_phones: bool, request_id: str,
                  client=None) -> DeliverOut:
    _validate(selection, action)
    plist = await _target_list(ts, view=selection.view, action=action, list_id=list_id,
                               user_id=user_id, role=role)
    q, bought = await prepare(ts, selection=selection, action=action,
                              include_phones=include_phones, client=client)
    await charges.preflight_all(ts, q.charges)

    out = DeliverOut(unavailable=q.unavailable, already_paid=q.already_paid, held=q.held,
                     list_id=plist.id if plist is not None else None)
    links: dict[str, tuple[str | None, str | None]] = {}
    account_ids: list[str] = []
    contact_ids: list[str] = []
    async with ts.session.begin_nested():
        for line in q.lines:
            contact_id, account_id = await _write(ts, line, selection.view, action,
                                                  include_phones, user_id, out)
            links[line.identity] = (contact_id, account_id)
            if account_id and account_id not in account_ids:
                account_ids.append(account_id)
            if contact_id and contact_id not in contact_ids:
                contact_ids.append(contact_id)
        if plist is not None:
            if plist.kind == "contact":
                added = await lists.add_members(ts, plist, contact_ids=contact_ids)
            else:
                added = await lists.add_members(ts, plist, account_ids=account_ids)
            out.listed = added.added + added.already
        await charges.record_purchases(ts, q.lines, request_id=request_id, links=links)
        await charges.meter_all(ts, q.charges, request_id=request_id, user_id=user_id)
    out.charged = {cap: n for cap, n in q.charges.items() if n > 0}
    out.record_ids = contact_ids if (selection.view == "contacts" and action != "company_list") \
        else account_ids
    await record_browse_cost(ts, rows=bought, user_id=user_id, view=selection.view)
    logger.info("prospector deliver view=%s action=%s records=%d created=%d charged=%s",
                selection.view, action, len(q.lines), out.created, out.charged)
    return out


async def _write(ts, line: Line, view: str, action: str, include_phones: bool, user_id: str,
                 out: DeliverOut) -> tuple[str | None, str | None]:
    """``(contact_id, account_id)`` for one line, creating what is missing."""
    if line.held is not None:
        out.reused += 1
        if line.held.kind == "contact":
            return line.held.record_id, line.held.account_id
        return None, line.held.record_id
    record = line.record
    if isinstance(record, VendorCompany):
        account, created, dropped = await upsert_account(
            ts, name=record.company_name, url=record.company_url, country=record.country,
            state=record.state, city=record.city, industry=record.industry,
            employee_size_range=record.employee_size_range, linkedin_url=record.linkedin_url,
            owner_user_id=user_id)
        out.created += int(created)
        out.reused += int(not created)
        out.dropped_linkedin += int(dropped)
        return None, account.id
    # A contact. Its company is a free by-product (D5), a bare name and domain.
    account, account_created, _ = await upsert_account(
        ts, name=record.company_name, url=record.company_url, owner_user_id=user_id)
    if action == "company_list":
        out.created += int(account_created)
        out.reused += int(not account_created)
        return None, account.id
    # The phone is written whenever phones were asked for: newly charged, or paid for under an
    # earlier request and delivered again free (D6).
    contact, created = await upsert_contact(ts, account, record, include_phone=include_phones)
    out.created += int(created)
    out.reused += int(not created)
    return contact.id, account.id
