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


# Sales territories and segments that appear after an employer separator where a company should be.
# Closed vocabulary, unlike company names — which is what makes matching it safe.
_TERRITORY_WORDS = frozenset({
    "central", "east", "west", "north", "south", "northeast", "northwest",
    "southeast", "southwest", "midwest", "eastern", "western", "northern", "southern",
    "emea", "apac", "apj", "latam", "amer", "americas", "anz", "na", "us", "usa",
    "uk", "eu", "europe", "asia", "canada", "global", "international", "worldwide",
    "enterprise", "smb", "mid-market", "midmarket", "commercial", "public sector",
    "federal", "strategic", "named accounts", "corporate", "remote",
})


def _looks_like_territory(value: str) -> bool:
    """True when a would-be company name is really a territory or segment.

    Matches the WHOLE value, not a substring: "CentralSquare Technologies" is a real company and
    must survive, while a bare "Central" must not.
    """
    low = " ".join((value or "").lower().split())
    if not low:
        return False
    if low in _TERRITORY_WORDS:
        return True
    # "Central Enterprise Sales", "US Central" — every word is territory/segment vocabulary or a
    # sales noun, so there is no company name in there at all.
    filler = _TERRITORY_WORDS | {"sales", "region", "territory", "division", "area", "team"}
    words = low.replace("-", " ").split()
    return len(words) > 1 and all(w in filler for w in words)


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
    # ...and may not be a company at all. "Vice President of Sales, Central" puts a TERRITORY after
    # an employer separator, and three of six live results did exactly that.
    #
    # A hand-list is normally the wrong tool here (see the Apify key-spelling note in CLAUDE.md),
    # but sales territories are a genuinely CLOSED vocabulary where company names are not, and the
    # asymmetry is safe in this direction: rejecting a real company called "Central" costs a blank
    # field, while accepting a territory shows the rep a company that does not exist.
    if company and _looks_like_territory(company):
        company = ""
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


@dataclass(slots=True)
class IcpTitleTargets:
    """The titles this workspace actually sells to, read from its ICP.

    `source_new` used to ignore the ICP entirely: it searched the seed's title, scored every result
    a flat 70, and returned them in whatever order the search engine chose. So a workspace whose ICP
    names "Chief Revenue Officer, VP of Sales, Head of Recruiting" got a list that was neither
    ranked nor filtered by any of it, and a Software Engineer who happened to rank well for the
    query sat above a CRO.
    """

    wanted: tuple[str, ...] = ()
    wanted_tokens: frozenset[str] = frozenset()
    excluded: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.wanted and not self.excluded


#: Abbreviations a LinkedIn headline uses where a CRM record spells the role out. Applied to the
#: SCORING copy of a title only, never to what is displayed.
#:
#: Measured on the ranking above: a seed of "Vice President of Sales" scored a candidate "VP of
#: Sales" — the identical role — at 43, BELOW a "Chief Revenue Officer" at 55, because the token
#: overlap between "vice president" and "vp" is zero. A sourcing list whose top result is not the
#: seed's own role is one a rep stops trusting on the first use.
_TITLE_ABBREVIATIONS: tuple[tuple[str, str], ...] = (
    (r"\bsvp\b", "senior vice president"),
    (r"\bevp\b", "executive vice president"),
    (r"\bavp\b", "assistant vice president"),
    (r"\bvp\b", "vice president"),
    (r"\bcro\b", "chief revenue officer"),
    (r"\bcfo\b", "chief financial officer"),
    (r"\bcmo\b", "chief marketing officer"),
    (r"\bcto\b", "chief technology officer"),
    (r"\bceo\b", "chief executive officer"),
    (r"\bcoo\b", "chief operating officer"),
    (r"\bciso\b", "chief information security officer"),
    (r"\bcio\b", "chief information officer"),
    (r"\bdir\b", "director"),
    (r"\bsr\b", "senior"),
    (r"\bjr\b", "junior"),
    (r"\bmgr\b", "manager"),
    (r"\bbd\b", "business development"),
    (r"\bbiz dev\b", "business development"),
    (r"\bsdr\b", "sales development representative"),
    (r"\bae\b", "account executive"),
    (r"\bhr\b", "human resources"),
    (r"\bta\b", "talent acquisition"),
)


def expand_title(value: str) -> str:
    """A title with its abbreviations spelled out, for comparison only.

    Expanding rather than contracting, because the long form is what carries the tokens both the
    seniority ranker and the department classifier already look for — contracting to "vp" would
    match neither.
    """
    low = " ".join((value or "").lower().split())
    if not low:
        return ""
    for pattern, full in _TITLE_ABBREVIATIONS:
        low = re.sub(pattern, full, low)
    return " ".join(low.split())


def _title_words(value: str) -> set[str]:
    """Meaningful words in a title. Parenthetical acronyms are kept — "Chief Revenue Officer (CRO)"
    is how an ICP writes it and "CRO" is how a headline does."""
    words = re.split(r"[^a-z0-9]+", expand_title(value))
    return {w for w in words if len(w) > 2 and w not in _TITLE_STOPWORDS}


#: Words that appear in every second title and carry no signal about the role.
_TITLE_STOPWORDS = frozenset({"the", "and", "for", "our", "his", "her", "their", "with"})


def icp_title_targets(icp: dict | None) -> IcpTitleTargets:
    """Read the ICP's title vocabulary. Every key it may use, because they are not interchangeable.

    `buyer_titles` is what the Relevance page writes and is the common case; `titles` and
    `title_keywords` predate it and are still honoured, because a workspace that filled one of those
    in has expressed the same intent and silently ignoring it is the "configured and doing nothing"
    state this codebase keeps finding.
    """
    icp = icp or {}
    wanted: list[str] = []
    for key in ("buyer_titles", "titles", "title_keywords", "job_levels"):
        values = icp.get(key) or []
        if isinstance(values, str):
            values = [values]
        wanted.extend(str(v).strip() for v in values if str(v).strip())

    excluded = [
        str(v).strip().lower()
        for v in (icp.get("exclude_title_keywords") or [])
        if str(v).strip()
    ]
    tokens: set[str] = set()
    for title in wanted:
        tokens |= _title_words(title)
    return IcpTitleTargets(
        wanted=tuple(dict.fromkeys(wanted)),
        wanted_tokens=frozenset(tokens),
        excluded=tuple(dict.fromkeys(excluded)),
    )


def icp_title_fit(title: str, targets: IcpTitleTargets) -> tuple[float, str]:
    """How well a candidate's title matches the ICP. ``(0..1, reason)``.

    Returns ``0.0`` with no reason when the ICP names no titles — an unstated ICP must not be read
    as "nobody fits", the same bias as the relevance engine scoring an unknown attribute neutral
    rather than punishing it.
    """
    if not targets.wanted or not title:
        return 0.0, ""
    low = " ".join(title.lower().split())
    for wanted in targets.wanted:
        w = " ".join(wanted.lower().split())
        # Substring both ways: the ICP writes "Vice President of Sales" and a headline writes
        # "VP of Sales, Central" — neither contains the other whole, so the token overlap below is
        # what actually catches most real pairs. This is the exact-ish case.
        if w and (w in low or low in w):
            return 1.0, f"Exactly an ICP buyer title: {wanted}"
    overlap = _title_words(title) & targets.wanted_tokens
    if not overlap:
        return 0.0, ""
    fit = min(1.0, len(overlap) / 2.0)
    return fit, f"Matches ICP buyer titles on {', '.join(sorted(overlap)[:3])}"


def _is_excluded(title: str, targets: IcpTitleTargets) -> bool:
    """A title the workspace has said it does not sell to. Dropped, not ranked low.

    Ranking an excluded title merely low still puts it in front of a rep when the list is short,
    and `exclude_title_keywords` is the one part of an ICP that is a statement about who NOT to
    contact.
    """
    low = (title or "").lower()
    return any(bad in low for bad in targets.excluded)


def _canonical_profile(url: str) -> str:
    """Compare profiles by path, ignoring scheme/host/query. `linkedin.com/in/alex-kim` and
    `www.linkedin.com/in/alex-kim/?trk=x` are the same person."""
    low = (url or "").strip().lower().split("?")[0].rstrip("/")
    marker = "linkedin.com/in/"
    return low.split(marker, 1)[1] if marker in low else low


#: How a sourced candidate's score is split between resembling the SEED and fitting the ICP.
#:
#: Weighted toward the seed because that is what the rep asked for — "people like this person" —
#: while the ICP is the workspace's standing answer to "who is worth contacting at all". Both
#: matter: seed-only returns the seed's peers at companies nobody sells to, ICP-only ignores the
#: question that was asked.
_SEED_WEIGHT = 0.6
_ICP_WEIGHT = 0.4

#: With a stated ICP, a candidate the ICP does not name must at least be this close to the seed to
#: survive. Below it, it is neither who the workspace sells to nor who the rep asked for.
#:
#: **Only applied when the ICP actually names titles.** Without one there is no basis to call a
#: search result noise, and dropping on the seed score alone cost real coverage: measured, a "VP
#: Revenue Operations" sourced for a "VP Sales" seed — same seniority, adjacent function, an
#: entirely reasonable peer — fell under a flat floor. Unknown means allow here, as it does in the
#: entitlements engine and the alert resolver; what an absent ICP costs is ranking, not results.
_SEED_ONLY_FLOOR = 0.5


def _score_candidate(
    *, title: str, seed, seed_features, targets: IcpTitleTargets
) -> tuple[int, list[str]] | None:
    """``(score 0..100, reasons)`` for a sourced person, or None to drop them.

    Every sourced candidate used to score a flat 70, which is not a score — it is the absence of
    one. The list then had no top, so a CRO and a regional coordinator were indistinguishable and
    the rep worked whichever order the search engine returned.
    """
    reasons: list[str] = []

    # Resemblance to the seed, through the same scorer that ranks the workspace's own contacts, so
    # `existing` and `new` cannot disagree about what "similar role" means. The title is expanded
    # for comparison only — "VP of Sales" and "Vice President of Sales" are one role.
    candidate = _TitleOnly(title=expand_title(title))
    seed_sim = contact_similarity(seed, candidate, seed_features=seed_features)
    seed_fit = seed_sim.score / 100.0
    if seed_sim.reasons and seed_sim.breakdown:
        reasons.extend(seed_sim.reasons[:2])

    icp_fit, icp_reason = icp_title_fit(title, targets)
    if icp_reason:
        reasons.append(icp_reason)
    elif targets.wanted:
        # The ICP is STATED and this person does not match it. Said out loud, because the score
        # alone cannot distinguish "close to your champion and exactly who you sell to" from "close
        # to your champion but not who you sell to" — and those need different decisions.
        #
        # Measured on the live workspace: an ICP naming "Facilities Director", a seed who is a VP of
        # Sales, and eight sourced VPs of Sales all scoring 60 with no indication why they were not
        # 100. The number was right and unreadable.
        reasons.append(
            f"Not one of your ICP buyer titles ({', '.join(targets.wanted[:2])})"
        )

    if targets.wanted:
        blended = seed_fit * _SEED_WEIGHT + icp_fit * _ICP_WEIGHT
        # With a STATED ICP, a candidate must either be a title this workspace sells to or be
        # genuinely close to the seed. "Regional Coordinator" for a VP of Sales seed cleared a flat
        # floor on seniority alone and sat in the list; noise in a sourcing list is what makes a rep
        # stop opening it.
        if icp_fit <= 0.0 and seed_fit < _SEED_ONLY_FLOOR:
            return None
    else:
        # An ICP that names no titles must not drag every candidate's score down by 40%, and must
        # not drop anybody either. Unstated is not "nobody fits".
        blended = seed_fit

    if not reasons:
        reasons.append(f"{title} — sourced on role")
    return round(blended * 100), reasons[:4]


@dataclass(slots=True)
class _TitleOnly:
    """A sourced person, for the scorer.

    A LinkedIn headline gives a title and nothing else — no seniority field, no department — and
    `prepare_contact` reads both from the title anyway. This exists so the sourced path can use the
    SAME scorer as the existing-contact path rather than a second one that would drift.
    """

    title: str
    seniority: str | None = None


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

        # The ICP, read ONCE. It is used three ways below — to shape the query, to rank the
        # results, and to drop the ones this workspace has said it does not sell to. Before this,
        # `source_new` read it only in the fallback path that runs when everything else found
        # nothing, so a workspace whose ICP names "CRO, VP of Sales, Head of Recruiting" got a list
        # that was neither ranked nor filtered by any of it.
        icp: dict = {}
        try:
            from nexus.relevance.engine import get_profile

            profile = await get_profile(ts)
            icp = (getattr(profile, "icp", None) or {}) if profile else {}
        except Exception:  # an unreadable ICP must not take the feature down
            logger.warning("could not read the ICP for contact %s", contact.id, exc_info=True)
        targets = icp_title_targets(icp)

        # ROLE SEARCH FIRST, not `find_similar`. Both are Exa; the difference is what "similar"
        # means for a PERSON. Measured live against Brian Biggs' profile, `find_similar` returned
        # two other Brian Biggses in its top five — a profile page's dominant text is the name, so
        # page similarity resolves to name similarity. The same seed's ROLE ("Vice President of
        # Sales" + the account's industry) returned eight distinct peers, none of them namesakes,
        # one with `healthcaresales` in the profile slug.
        hits = []
        title_q = (contact.title or "").strip()
        # A seed with no title used to fall straight to `find_similar` and its namesakes. If the
        # ICP names the titles this workspace sells to, that is a far better query than a page whose
        # dominant text is somebody's name — and it costs the same one call.
        if not title_q and targets.wanted:
            title_q = targets.wanted[0]
        if title_q:
            # The industry the query names: the seed's own account first, because that is a fact
            # about the person we are matching. The ICP's industries are the fallback, which is what
            # makes this work for an account whose industry nobody has filled in.
            industry = ((account.industry if account else "") or "").strip()
            if not industry:
                industries = [str(i).strip() for i in (icp.get("industries") or []) if str(i).strip()]
                industry = industries[0] if industries else ""
            query = " ".join(
                p for p in (title_q, "at a", industry, "company LinkedIn profile") if p
            )
            try:
                hits = list(await registry.search(query, limit=max(limit * 3, 12)) or [])
            except Exception:  # a search backend must never break the page
                logger.warning("role search failed for contact %s", contact.id, exc_info=True)
                hits = []
        # `find_similar` is kept as the fallback for a seed with no title AND no ICP titles, where
        # there is nothing to search a role with and a weak answer beats none.
        if not hits and seed_url:
            try:
                hits = list(await registry.find_similar(seed_url, limit=max(limit * 2, 10)) or [])
            except Exception:
                logger.warning("find_similar failed for contact %s", contact.id, exc_info=True)
                hits = []

        seed_features = prepare_contact(contact)
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
            # A title the workspace has said it does not sell to. Dropped rather than ranked low:
            # on a short list, "ranked low" still means "in front of the rep".
            if _is_excluded(title, targets):
                continue

            scored = _score_candidate(
                title=title, seed=contact, seed_features=seed_features, targets=targets
            )
            if scored is None:
                # Neither similar to the seed nor anything the ICP asks for. A Software Engineer
                # returned for a VP of Sales seed is noise, and noise in a sourcing list is what
                # makes a rep stop opening it.
                continue
            score, reasons = scored
            seen.add(key)
            out.append(ContactLookalike(
                contact_id="", full_name=name, account_id="", title=title or None,
                linkedin_url=url, company=company, is_new=True, score=score,
                reasons=reasons,
            ))

        if out:
            # RANKED, not returned in search order. Every result used to score a flat 70, so the
            # list had no top: a CRO and a regional coordinator were indistinguishable, and the rep
            # worked whichever the search engine happened to put first.
            out.sort(key=lambda lk: lk.score, reverse=True)
            return out[:limit]

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
