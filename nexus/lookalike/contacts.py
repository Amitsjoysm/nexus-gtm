"""Contact-level look-alike finder — "find more people like this person".

Given a seed contact (e.g. a champion who converted), rank the other people in the workspace by how
closely they resemble the seed: same/similar **role** (title), **seniority**, **department**, and
how similar their **company** is to the seed's company. Deterministic and offline-safe — it ranks
existing book contacts, so it needs no network and is reproducible in CI. (A future augmentation can
source net-new people at look-alike companies via the contact_search seam.)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nexus.core.tenancy import TenantSession
from nexus.lookalike.similarity import (
    company_similarity,
    contact_similarity,
    prepare_company,
    prepare_contact,
)
from nexus.models.account import Account, Contact

# Cap the candidate pool so a huge workspace can't blow up memory/latency. Scoring is pure and the
# seed is prepared once + company similarity is memoized per account, so this bound is generous; a
# future version can pre-filter by title/seniority in SQL (see scalability notes) for 100k+ books.
_MAX_POOL = 1000


@dataclass(slots=True)
class ContactLookalike:
    contact_id: str
    full_name: str
    account_id: str
    account_name: str = ""
    title: str | None = None
    seniority: str | None = None
    email: str | None = None
    linkedin_url: str | None = None
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "contact_id": self.contact_id,
            "full_name": self.full_name,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "title": self.title,
            "seniority": self.seniority,
            "email": self.email,
            "linkedin_url": self.linkedin_url,
            "score": self.score,
            "reasons": list(self.reasons),
        }


class ContactLookalikeService:
    async def find(
        self, ts: TenantSession, contact: Contact, *, limit: int = 10
    ) -> list[ContactLookalike]:
        seed_account = await ts.get(Account, contact.account_id)

        # Every other contact in the workspace (tenant-scoped via ts.list), bounded.
        pool = await ts.list(Contact, Contact.id != contact.id, limit=_MAX_POOL)
        if not pool:
            return []

        # Batch-load the candidates' accounts (avoid N+1) for company-similarity + display.
        acct_ids = {c.account_id for c in pool if c.account_id}
        if seed_account is not None:
            acct_ids.add(seed_account.id)
        accounts = {
            a.id: a for a in await ts.list(Account, Account.id.in_(list(acct_ids)))
        } if acct_ids else {}

        # Extract the seed's features ONCE (the expensive tokenization), then reuse across the pool.
        seed_contact_feat = prepare_contact(contact)
        seed_account_feat = prepare_company(seed_account) if seed_account is not None else None
        # Company similarity depends only on the candidate's *account*, so memoize per account_id:
        # 50 contacts at one account → one score, not 50.
        company_sim_by_account: dict[str, float | None] = {}

        def company_sim_for(account_id: str) -> float | None:
            if account_id in company_sim_by_account:
                return company_sim_by_account[account_id]
            val: float | None = None
            cand_account = accounts.get(account_id)
            if seed_account_feat is not None and cand_account is not None:
                val = company_similarity(
                    seed_account, cand_account, seed_features=seed_account_feat
                ).score / 100.0
            company_sim_by_account[account_id] = val
            return val

        out: list[ContactLookalike] = []
        for cand in pool:
            cand_account = accounts.get(cand.account_id)
            company_sim = company_sim_for(cand.account_id) if cand.account_id else None
            sim = contact_similarity(
                contact, cand, company_sim=company_sim, seed_features=seed_contact_feat
            )
            out.append(
                ContactLookalike(
                    contact_id=cand.id,
                    full_name=cand.full_name,
                    account_id=cand.account_id,
                    account_name=cand_account.name if cand_account is not None else "",
                    title=cand.title,
                    seniority=cand.seniority,
                    email=cand.email,
                    linkedin_url=cand.linkedin_url,
                    score=sim.score,
                    reasons=sim.reasons[:5],
                )
            )

        out.sort(key=lambda lk: lk.score, reverse=True)
        return out[:limit]


_service: ContactLookalikeService | None = None


def get_contact_lookalike_service() -> ContactLookalikeService:
    global _service
    if _service is None:
        _service = ContactLookalikeService()
    return _service


def set_contact_lookalike_service(service: ContactLookalikeService | None) -> None:
    global _service
    _service = service
