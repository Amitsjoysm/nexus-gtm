"""What a selection costs, field by field (decisions D4, D5, D6). Charges nothing.

* **Contacts view**: 1 ``prospector.email`` per record that has an email; 1 ``prospector.phone`` per
  direct phone, only when ``include_phones``. A record with neither costs nothing (its name, title
  and company were already visible while browsing).
* **Companies view**: 1 ``prospector.company`` per record.
* **"Add their companies to an account list"** from the Contacts view (``company_list``) is free, as
  is the company a saved contact needs (D5).
* **Charge once** (D6): a (record, field) already in the ledger is not charged again.
* **Held rows are free**: the workspace already has the record; saving or listing links it, and an
  export writes the workspace's own values.

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
    #: The fields this delivery charges for (after holdings and the ledger).
    fields: tuple[str, ...] = ()


@dataclass(slots=True)
class Quote:
    lines: list[Line] = field(default_factory=list)
    charges: dict[str, int] = field(default_factory=dict)
    already_paid: int = 0
    held: int = 0
    unavailable: int = 0


def chargeable_fields(view: str, action: str, record: Record, *,
                      include_phones: bool) -> tuple[str, ...]:
    if action == "company_list":
        return ()
    if view == "companies":
        return ("company",)
    wanted = []
    if record.email:
        wanted.append("email")
    if include_phones and record.direct_phone:
        wanted.append("phone")
    return tuple(wanted)


async def quote(ts, *, view: str, action: str, resolved: Resolved,
                include_phones: bool) -> Quote:
    records = [r for _, r in resolved.records]
    if view == "contacts":
        held = await held_contacts(ts, records)
    else:
        held = await held_companies(ts, records)
    prints = {ident: fingerprint(ident) for ident, _ in resolved.records}
    paid = await purchased(ts, list(prints.values()))

    out = Quote(unavailable=resolved.unavailable)
    for ident, record in resolved.records:
        line = Line(identity=ident, fingerprint=prints[ident], record=record,
                    held=held.get(ident))
        if line.held is not None:
            out.held += 1
        else:
            wanted = chargeable_fields(view, action, record, include_phones=include_phones)
            line.fields = tuple(f for f in wanted if (line.fingerprint, f) not in paid)
            out.already_paid += len(wanted) - len(line.fields)
            for f in line.fields:
                cap = CAPABILITY[f]
                out.charges[cap] = out.charges.get(cap, 0) + 1
        out.lines.append(line)
    return out
