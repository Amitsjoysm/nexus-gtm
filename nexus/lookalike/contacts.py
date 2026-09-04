"""Contact-level look-alike finder — "find more people like this person".

Given a seed contact (e.g. a champion who converted), rank the other people in the workspace by how
closely they resemble the seed: same/similar **role** (title), **seniority**, **department**, and
how similar their **company** is to the seed's company. Deterministic and offline-safe — it ranks
existing book contacts, so it needs no network and is reproducible in CI. (A future augmentation can
source net-new people at look-alike companies via the contact_search seam.)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from nexus.core.tenancy import TenantSession
from nexus.integrations.registry import get_registry
from nexus.lookalike.similarity import (
    company_similarity,
    contact_similarity,
    prepare_company,
    prepare_contact,
)
from nexus.models.account import Account, Contact

logger = logging.getLogger("nexus.lookalike.contacts")

# Cap the candidate pool so a huge workspace can't blow up memory/latency. Scoring is pure and the
# seed is prepared once + company similarity is memoized per account, so this bound is generous; a
# future version can pre-filter by title/seniority in SQL (see scalability notes) for 100k+ books.
_MAX_POOL = 1000


# LinkedIn renders a profile title as "Name - Title - Company | LinkedIn", with an en dash on some
# locales. Parsed rather than guessed at, because the alternative is a second paid call per result
# just to learn a name the SERP already handed us.
_PROFILE_TITLE_SPLIT = re.compile(r"\s+[-–—|]\s+")


# Exa returns a LinkedIn profile as markdown whose first lines are the name and the headline:
#
#     # Jordan Adams
#
#     Vice President of Sales, NextGen Healthcare
#
#     Boerne, Texas, United States (US)
#
# The page TITLE is usually just the name ("Jordan Adams"), so the role — the only thing that makes
# a suggestion judgeable — lives in the snippet or nowhere. Measured against the live Exa index.
#
# Punctuation separators need no space BEFORE them — "Vice President of Sales, NextGen Healthcare"
# has the comma flush against the role — while the word "at" does, or it would split "Data at Rest".
_HEADLINE_SPLIT = re.compile(r"\s*[,|@–-]\s+|\s+at\s+")

# Which separators actually introduce an EMPLOYER. Measured on live results: a comma, "at" and "@"
# do ("Vice President of Sales, NextGen Healthcare"), while a dash or a pipe usually introduce a
# territory or self-branding ("Vice President of Sales - Central", "VP of Sales | GTM | Dad").
# Taking the dash case as a company put "Central" in the company column for six of six results.
_EMPLOYER_SEP = re.compile(r"\s*[,@]\s+|\s+at\s+")


def _parse_headline(snippet: str) -> tuple[str, str]:
    """``(title, company)`` from a profile snippet. Both may be empty."""
    lines = [ln.strip().lstrip("#").strip() for ln in (snippet or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if len(lines) < 2:
        return "", ""
    headline = lines[1]
    # A headline is "VP of Sales at Acme", "VP of Sales, Acme" or "VP of Sales @ Acme | GTM | Dad".
    # Everything past the first separator is the employer; anything past a second is self-branding.
    parts = [p.strip() for p in _HEADLINE_SPLIT.split(headline) if p.strip()]
    if not parts:
        return "", ""
    title = parts[0][:120]
    # The company is read only from an EMPLOYER separator. A dash or pipe is just as likely to
    # introduce a territory, and "Central" sitting in the company column is worse than a blank one:
    # a blank says we do not know, a wrong one says we do.
    emp = [p.strip() for p in _EMPLOYER_SEP.split(headline) if p.strip()]
    company = emp[1][:120] if len(emp) > 1 else ""
    # Whatever followed an employer separator may itself carry trailing branding.
    if company:
        company = [c.strip() for c in _HEADLINE_SPLIT.split(company) if c.strip()][0][:120]
    # A "headline" that is really a location line ("Atlanta Metropolitan Area (US)") is not a role.
    if title.lower().endswith(("(us)", "area", "states")):
        return "", ""
    return title, company


def _same_person(a: str, b: str) -> bool:
    """Whether two display names are the same human, for excluding NAMESAKES.

    Measured live: `find_similar` on Brian Biggs' profile returned two other Brian Biggses in the
    top five, because a profile page's dominant text is the name — so "similar page" means "similar
    name". A rep asking for people like their champion does not want their champion's namesakes.
    """
    def norm(v: str) -> str:
        # Whitespace collapsed, not just stripped: "brian  biggs" and "Brian Biggs" are one person,
        # and a doubled space from a scraped page must not read as a different human.
        return " ".join(re.sub(r"[^a-z ]", " ", (v or "").lower()).split())

    na, nb = norm(a), norm(b)
    return bool(na) and na == nb


def _profile_url(url: str) -> bool:
    """A LinkedIn PERSON profile, not a company page or a job post.

    `find_similar` on a profile returns all three. Storing a company page as a person is the
    wrong-attribution failure this codebase keeps shipping — here it would put a company's name in
    a rep's call list as a human being.
    """
    low = (url or "").strip().lower()
    return "linkedin.com/in/" in low


def _parse_profile(title: str) -> tuple[str, str, str]:
    """``(name, title, company)`` from a LinkedIn SERP title. Empty name means unusable."""
    raw = (title or "").strip()
    if not raw:
        return "", "", ""
    parts = [p.strip() for p in _PROFILE_TITLE_SPLIT.split(raw) if p.strip()]
    parts = [p for p in parts if p.lower() != "linkedin"]
    if not parts:
        return "", "", ""
    name = parts[0]
    # A name with no spaces and no vowels, or one that is obviously a page label, is not a person.
    if len(name) < 3 or len(name) > 80:
        return "", "", ""
    return name, (parts[1] if len(parts) > 1 else ""), (parts[2] if len(parts) > 2 else "")


def _canonical_profile(url: str) -> str:
    """Compare profiles by path, ignoring scheme/host/query. `linkedin.com/in/alex-kim` and
    `www.linkedin.com/in/alex-kim/?trk=x` are the same person."""
    low = (url or "").strip().lower().split("?")[0].rstrip("/")
    marker = "linkedin.com/in/"
    return low.split(marker, 1)[1] if marker in low else low


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
    #: True when this person is NOT in the workspace yet. `contact_id` is empty for these, and the
    #: UI needs the distinction to offer "add" rather than "open" — a row that looks like a saved
    #: contact but has no id is a dead link.
    is_new: bool = False
    #: Employer as a plain string. Net-new people have no `Account` row, so `account_name` (which
    #: is read from one) would be empty and the rep would see a person with no company.
    company: str = ""

    def as_dict(self) -> dict:
        return {
            "contact_id": self.contact_id,
            "is_new": self.is_new,
            "company": self.company,
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


    async def source_new(
        self, ts: TenantSession, contact: Contact, *, limit: int = 10
    ) -> list[ContactLookalike]:
        """Source people NOT in the workspace who resemble this one. Never raises.

        **Exa `find_similar` is the primary searcher**, seeded with the contact's own LinkedIn
        profile URL. That is the strongest query available for a person: no title synonyms to
        guess at, no ICP round-trip, and the neural neighbour search is what Exa is for. The
        fallback is `contact_search` against the seed's account and role, used when the seed has no
        profile URL to be similar to, or when Exa answers with nothing — an unkeyed or rate-limited
        provider returns `[]`, and handing the rep an empty list there is indistinguishable from
        "no similar people exist".

        Returns candidates, and persists nothing. Sourcing a name is cheap; creating a Contact row
        commits the workspace to a person the rep has not looked at yet, so adding is a separate,
        explicit act.
        """
        out: list[ContactLookalike] = []
        seed_url = (contact.linkedin_url or "").strip()
        seed_name = contact.full_name or ""
        seen: set[str] = {_canonical_profile(seed_url)} if seed_url else set()

        registry = get_registry()
        account = await ts.get(Account, contact.account_id) if contact.account_id else None

        # ROLE SEARCH FIRST, not `find_similar`. Both are Exa; the difference is what "similar"
        # means for a PERSON. Measured live against Brian Biggs' profile, `find_similar` returned
        # two other Brian Biggses in its top five — a profile page's dominant text is the name, so
        # page similarity resolves to name similarity. The same seed's ROLE ("Vice President of
        # Sales" + the account's industry) returned eight distinct peers, none of them namesakes,
        # one with `healthcaresales` in the profile slug.
        #
        # `find_similar` is kept as the fallback for a seed with no title, where there is nothing
        # to search a role with and a weak answer beats none.
        hits = []
        title_q = (contact.title or "").strip()
        if title_q:
            industry = ((account.industry if account else "") or "").strip()
            query = " ".join(
                p for p in (title_q, "at a", industry, "company LinkedIn profile") if p
            )
            try:
                hits = list(await registry.search(query, limit=max(limit * 3, 12)) or [])
            except Exception:  # a search backend must never break the page
                logger.warning("role search failed for contact %s", contact.id, exc_info=True)
                hits = []
        if not hits and seed_url:
            try:
                hits = list(await registry.find_similar(seed_url, limit=max(limit * 2, 10)) or [])
            except Exception:
                logger.warning("find_similar failed for contact %s", contact.id, exc_info=True)
                hits = []

        for hit in hits:
            url = str(getattr(hit, "url", "") or "")
            if not _profile_url(url):
                continue
            key = _canonical_profile(url)
            if not key or key in seen:
                continue
            name, title, company = _parse_profile(str(getattr(hit, "title", "") or ""))
            if not name:
                continue
            # NAMESAKES ARE NOT PEERS. Excluded whichever path found them: a rep asking for people
            # like their champion does not want three more people with their champion's name.
            if _same_person(name, seed_name):
                continue
            # The page title is usually just the name, so the ROLE comes from the snippet or not at
            # all — and a suggestion with no role is one a rep cannot judge without opening it.
            snip_title, snip_company = _parse_headline(str(getattr(hit, "snippet", "") or ""))
            title = title or snip_title
            company = company or snip_company
            if not title:
                continue
            seen.add(key)
            out.append(ContactLookalike(
                contact_id="", full_name=name, account_id="", title=title or None,
                linkedin_url=url, company=company, is_new=True, score=70,
                reasons=[
                    f"{title} — similar role to {contact.title or seed_name}"
                    if contact.title else f"Similar profile to {seed_name}"
                ],
            ))
            if len(out) >= limit:
                return out

        if out:
            return out

        # Fallback: search by ROLE at the seed's own account. Weaker than profile similarity — it
        # finds the same job at the same company rather than the same kind of person anywhere — but
        # it is an answer, and it is what exists when there is no profile to seed with.
        try:
            if account is None:
                return out
            from nexus.relevance.engine import get_profile

            profile = await get_profile(ts)
            icp = (getattr(profile, "icp", None) or {}) if profile else {}
            cands = list(await registry.contact_search(account, icp, limit=limit) or [])
        except Exception:
            logger.warning("contact_search fallback failed for %s", contact.id, exc_info=True)
            return out

        for cand in cands:
            if (getattr(cand, "source", "") or "").lower() == "stub":
                continue                      # title-personas, never real people
            name = (getattr(cand, "full_name", "") or "").strip()
            if not name or name.lower() == (contact.full_name or "").lower():
                continue
            out.append(ContactLookalike(
                contact_id="", full_name=name, account_id="",
                title=getattr(cand, "title", None),
                seniority=getattr(cand, "seniority", None),
                email=getattr(cand, "email", None),
                linkedin_url=getattr(cand, "linkedin_url", None),
                company=account.name or "", is_new=True, score=55,
                reasons=[f"Similar role at {account.name}"],
            ))
            if len(out) >= limit:
                break
        return out


_service: ContactLookalikeService | None = None


def get_contact_lookalike_service() -> ContactLookalikeService:
    global _service
    if _service is None:
        _service = ContactLookalikeService()
    return _service


def set_contact_lookalike_service(service: ContactLookalikeService | None) -> None:
    global _service
    _service = service
