"""Insights for contacts as a screen shows them: what may be said, how likely a reply is, when to
send (spec §18.5). Assembles the pure rules over one batch of reads.

A fixed number of queries for any list: the contacts, their accounts, the latest score per account,
recent signals per account, and (for an opted-in workspace) one read each of person and company
profiles from the insights store. A workspace that has not opted in reads nothing from the store;
its likelihood is built from its own data only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from nexus.engagement.insights.best_time import BestTime, for_campaign, for_person
from nexus.engagement.insights.likelihood import Likelihood, likelihood
from nexus.engagement.insights.rules import Insight, describe

SIGNAL_WINDOW_DAYS = 30
MAX_CONTACTS = 100


@dataclass(slots=True)
class ContactInsight:
    contact_id: str
    person: Insight
    company: Insight
    likelihood: Likelihood
    best_time: BestTime

    def as_dict(self) -> dict:
        return {"contact_id": self.contact_id, "person": self.person.as_dict(),
                "company": self.company.as_dict(), "likelihood": self.likelihood.as_dict(),
                "best_time": self.best_time.as_dict()}


async def _latest_scores(ts, account_ids: set[str]) -> dict[str, int]:
    from nexus.models.intelligence import AccountScore

    scores = await ts.list(AccountScore, AccountScore.account_id.in_(account_ids)) \
        if account_ids else []
    latest: dict[str, AccountScore] = {}
    for s in scores:
        if s.account_id not in latest or s.computed_at > latest[s.account_id].computed_at:
            latest[s.account_id] = s
    return {a: s.composite for a, s in latest.items()}


async def _recent_signals(ts, account_ids: set[str], now: datetime) -> dict[str, int]:
    from sqlalchemy import func, select

    from nexus.models.signal import SignalEvent

    if not account_ids:
        return {}
    rows = (await ts.session.execute(
        select(SignalEvent.account_id, func.count())
        .where(SignalEvent.tenant_id == ts.tenant_id)
        .where(SignalEvent.account_id.in_(account_ids))
        .where(SignalEvent.occurred_at >= now - timedelta(days=SIGNAL_WINDOW_DAYS))
        .group_by(SignalEvent.account_id))).all()
    return {account_id: int(n) for account_id, n in rows}


async def for_contacts(ts, contact_ids: list[str], *, now: datetime) -> list[ContactInsight]:
    from nexus.engagement.insights import client
    from nexus.engagement.ledger import consent
    from nexus.models.account import Account, Contact

    ids = list(dict.fromkeys(contact_ids))[:MAX_CONTACTS]
    contacts = await ts.list(Contact, Contact.id.in_(ids)) if ids else []
    if not contacts:
        return []
    account_ids = {c.account_id for c in contacts if c.account_id}
    accounts = {a.id: a for a in await ts.list(Account, Account.id.in_(account_ids))} \
        if account_ids else {}
    scores = await _latest_scores(ts, account_ids)
    signals = await _recent_signals(ts, account_ids, now)

    consented = await consent.status(ts) == "on"
    people = await client.person_profiles([c.email for c in contacts if c.email]) \
        if consented else {}
    domains = [getattr(accounts.get(c.account_id), "domain", "") or "" for c in contacts]
    companies = await client.company_profiles([d for d in domains if d]) if consented else {}

    out = []
    order = {cid: i for i, cid in enumerate(ids)}
    for c in sorted(contacts, key=lambda c: order[c.id]):
        account = accounts.get(c.account_id)
        person = describe(people.get((c.email or "").strip().lower()), viewer_consented=consented)
        company = describe(companies.get((getattr(account, "domain", "") or "").strip().lower()),
                           viewer_consented=consented, subject="company")
        out.append(ContactInsight(
            contact_id=c.id, person=person, company=company,
            likelihood=likelihood(fit=scores.get(c.account_id),
                                  recent_signals=signals.get(c.account_id, 0),
                                  propensity=person.propensity if person.level == "pattern"
                                  else None),
            best_time=for_person(person, company)))
    return out


async def campaign_best_time(ts, campaign, *, now: datetime) -> BestTime:
    """The hour most of a campaign's people reply at, where the rules allow a pattern."""
    from nexus.models.engagement import EngagementEnrollment

    enrollments = await ts.list(EngagementEnrollment,
                                EngagementEnrollment.campaign_id == campaign.id)
    insights = await for_contacts(ts, [e.contact_id for e in enrollments], now=now)
    return for_campaign([i.person for i in insights])
