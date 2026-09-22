"""Who and what a ledger event is about, read from the records its refs name (spec §18.3, §18.5).

Resolved once, at shipping, and sealed into the archive beside the envelope. Three things need it:
the person keys an erasure searches by, the known names the scrubber replaces, and the person and
company an insights fact is about.

Runs on the platform session, because the shipper reads across workspaces — but every record is
checked against the event's OWN tenant before a field is taken from it, so a ref that names another
workspace's row contributes nothing. That check is the whole safety story of this module: this is
the one place in the ledger where tenant-scoped records are read without a tenant binding.

Never raises. A record that cannot be read resolves to empty fields, and an event with no resolved
contact simply produces no insights fact and no person key — never a failed batch.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Resolved:
    tenant_id: str = ""
    consent_terms_version: str = ""
    contact_email: str = ""
    contact_name: str = ""
    contact_title: str = ""
    contact_seniority: str = ""
    account_name: str = ""
    account_domain: str = ""
    account_industry: str = ""
    account_employee_count: int | None = None
    account_country: str = ""
    sdr_name: str = ""
    sdr_email: str = ""
    other_names: list[str] = field(default_factory=list)

    def person_emails(self) -> list[str]:
        """Whose erasure must find this event. The SDR is our customer's employee, not a
        prospect — their address is still scrubbed out of training text, but an erasure request
        is about the person we contacted."""
        return [e for e in (self.contact_email,) if e]

    def known(self):
        """The names, companies and addresses the scrubber replaces with stable placeholders."""
        from nexus.engagement.ledger.scrub import Known

        return Known(
            people=[n for n in (self.contact_name, self.sdr_name, *self.other_names) if n],
            companies=[c for c in (self.account_name, self.account_domain) if c],
            emails=[e for e in (self.contact_email, self.sdr_email) if e],
        )

    def as_dict(self) -> dict:
        from dataclasses import asdict

        data = asdict(self)
        data["person_emails"] = self.person_emails()
        return data


async def resolve(session, envelope: dict) -> Resolved:
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection
    from nexus.models.identity import User

    tenant_id = str(envelope.get("tenant_id") or "")
    refs = envelope.get("refs") or {}
    out = Resolved(tenant_id=tenant_id)
    if not tenant_id:
        return out

    def owned(row) -> bool:
        return row is not None and getattr(row, "tenant_id", None) == tenant_id

    try:
        contact = await session.get(Contact, refs["contact_id"]) if refs.get("contact_id") else None
        if owned(contact):
            out.contact_email = (contact.email or "").strip().lower()
            out.contact_name = contact.full_name or ""
            out.contact_title = contact.title or ""
            out.contact_seniority = contact.seniority or ""
        account_id = refs.get("account_id") or (
            contact.account_id if owned(contact) else None)
        account = await session.get(Account, account_id) if account_id else None
        if owned(account):
            out.account_name = account.name or ""
            out.account_domain = (account.domain or "").strip().lower()
            out.account_industry = account.industry or ""
            out.account_employee_count = account.employee_count
            out.account_country = account.country or ""
        mailbox = (await session.get(MailboxConnection, refs["mailbox_id"])
                   if refs.get("mailbox_id") else None)
        user_id = (envelope.get("actor") or {}).get("user_id")
        if owned(mailbox):
            out.sdr_email = mailbox.email or ""
            user_id = user_id or mailbox.owner_user_id
        user = await session.get(User, user_id) if user_id else None
        if user is not None:
            out.sdr_name = user.full_name or ""
            out.sdr_email = out.sdr_email or (user.email or "")
        out.consent_terms_version = await _terms_version(session, tenant_id)
    except Exception:
        return out
    return out


async def _terms_version(session, tenant_id: str) -> str:
    """The terms the workspace agreed to when this was collected, stamped on every training row."""
    from sqlalchemy import select

    from nexus.models.ledger import TrainingConsent

    row = (await session.execute(
        select(TrainingConsent.terms_version)
        .where(TrainingConsent.tenant_id == tenant_id)
        .order_by(TrainingConsent.decided_at.desc())
        .limit(1)
    )).scalars().first()
    return row or ""
