"""What a selection costs, field by field (decisions D4, D5, D6). Charges nothing.

* **Contacts view**: 1 ``prospector.email`` per record that has an email, when emails were asked
  for; 1 ``prospector.phone`` per direct phone, when phones were asked for. The two are chosen
  separately (2026-11-18): a rep can reveal only a phone, only an email, or both. A record with
  neither costs nothing (its name, title and company were already visible while browsing).
* **Companies view**: 1 ``prospector.company`` per record.
* **"Add their companies to an account list"** from the Contacts view (``company_list``) is free, as
  is the company a saved contact needs (D5).
* **Charge once** (D6): a (record, field) already in the ledger is not charged again, but is still
  delivered.
* **A held row costs nothing for what the workspace already has.** A contact the workspace holds
  WITHOUT a field it now asks for (saved earlier without its phone, say) gets that field: charged
  like any delivery unless already paid, and written into the blank, never over a value. Before
  this a held row was simply free and untouched, so a phone not bought on the first save could
  never be bought at all.

The quote is shown before anything is spent and is exactly what the delivery charges.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nexus.prospector.holdings import Held, held_companies, held_contacts, purchased
from nexus.prospector.identity import fingerprint
from nexus.prospector.records import VendorCompany, VendorContact
from nexus.prospector.selection import Resolved

CAPABILITY = {"email": "prospector.email", "phone": "prospector.phone",
              "company": "prospector.company"}
ACTIONS = ("save", "list", "reveal", "company_list", "export")

Record = VendorContact | VendorCompany


@dataclass(slots=True)
class Line:
    identity: str
    fingerprint: str
    record: Record
    held: Held | None
    #: The paid fields this delivery hands over (written, exported), charged or not.
    deliver: tuple[str, ...] = ()
    #: The subset of ``deliver`` it charges for: not already in the ledger.
    fields: tuple[str, ...] = ()


@dataclass(slots=True)
class Quote:
    lines: list[Line] = field(default_factory=list)
    charges: dict[str, int] = field(default_factory=dict)
    already_paid: int = 0
    held: int = 0
    unavailable: int = 0
    with_email: int = 0
    with_phone: int = 0
    #: Held records that get a field they were missing.
    filled: int = 0


def chargeable_fields(view: str, action: str, record: Record, *, include_phones: bool,
                      include_emails: bool = True) -> tuple[str, ...]:
    """The paid fields this record would deliver, before holdings and the ledger."""
    if action == "company_list":
        return ()
    if view == "companies":
        return ("company",)
    wanted = []
    if include_emails and record.email:
        wanted.append("email")
    if include_phones and record.direct_phone:
        wanted.append("phone")
    return tuple(wanted)


def missing_on_held(held: Held, wanted: tuple[str, ...]) -> tuple[str, ...]:
    """The wanted fields a held CONTACT does not have yet. A held company has nothing to add."""
    if held.kind != "contact":
        return ()
    have = {"email": bool((held.email or "").strip()), "phone": bool((held.phone or "").strip())}
    return tuple(f for f in wanted if f in have and not have[f])


async def quote(ts, *, view: str, action: str, resolved: Resolved, include_phones: bool,
                include_emails: bool = True) -> Quote:
    records = [r for _, r in resolved.records]
    if view == "contacts":
        held = await held_contacts(ts, records)
    else:
        held = await held_companies(ts, records)
    prints = {ident: fingerprint(ident) for ident, _ in resolved.records}
    paid = await purchased(ts, list(prints.values()))

    out = Quote(unavailable=resolved.unavailable)
    for ident, record in resolved.records:
        if isinstance(record, VendorContact):
            out.with_email += int(bool(record.email))
            out.with_phone += int(bool(record.direct_phone))
        line = Line(identity=ident, fingerprint=prints[ident], record=record,
                    held=held.get(ident))
        wanted = chargeable_fields(view, action, record, include_phones=include_phones,
                                   include_emails=include_emails)
        if line.held is not None:
            out.held += 1
            wanted = missing_on_held(line.held, wanted)
            out.filled += int(bool(wanted))
        line.deliver = wanted
        line.fields = tuple(f for f in wanted if (line.fingerprint, f) not in paid)
        out.already_paid += len(wanted) - len(line.fields)
        for f in line.fields:
            cap = CAPABILITY[f]
            out.charges[cap] = out.charges.get(cap, 0) + 1
        out.lines.append(line)
    return out
