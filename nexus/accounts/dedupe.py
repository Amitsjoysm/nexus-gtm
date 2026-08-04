# nexus/accounts/dedupe.py
"""One answer to "do we already have this company?", for every path that creates an account.

Accounts arrive from six places — a rep typing a name, a CSV import, ICP auto-discovery, a CRM sync,
the look-alike finder and the shared-company backfill. Each grew its own check, and each compared
the **raw** domain string, so ``acme.com``, ``www.acme.com``, ``https://acme.com/`` and
``ACME.com`` were four different accounts in one workspace. The manual create endpoint had no check
at all, which is the path a rep uses most.

The rep sees the result: four Acme rows, the fit score computed four times, four inbox tasks for the
same funding round, and no way to tell which one holds the notes.

**Identity is the normalised domain**, exactly as in ``nexus/companies/resolution.py`` — the same
rule, deliberately, so an account and its shared company row can never disagree about who they are.

**Name matching is a fallback, never the primary key, and only on an exact normalised match.** A
name is not an identity: "Apex" the fintech and "Apex" the logistics firm are different companies,
and this codebase has already shipped six wrong-attribution bugs by trusting a name. An account with
no usable domain therefore dedupes only against an identical name, and two genuinely different
companies sharing a name stay separate — the safe direction to be wrong in.
"""
from __future__ import annotations

import logging
import re

from nexus.companies.resolution import normalise_domain

logger = logging.getLogger("nexus.accounts.dedupe")

# Suffixes a rep types inconsistently. "Acme", "Acme Inc" and "Acme, Inc." are one company; dropping
# these before comparing catches the common case without inventing a fuzzy match.
_LEGAL_SUFFIXES = re.compile(
    r"[\s,]+(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|co|co\.|"
    r"gmbh|ag|sa|s\.a\.|bv|b\.v\.|plc|pty|pte|srl|oy|ab|as|nv|n\.v\.)\s*$",
    re.I,
)


def normalise_name(name: str | None) -> str:
    """A comparable company name, or "".

    Lowercased, legal suffix removed, punctuation and repeated whitespace collapsed. Only used for
    exact comparison after normalisation — never as a similarity score.
    """
    raw = (name or "").strip().lower()
    if not raw:
        return ""
    previous = None
    while previous != raw:            # "Acme Co., Ltd." carries two suffixes
        previous = raw
        raw = _LEGAL_SUFFIXES.sub("", raw).strip(" ,.")
    raw = re.sub(r"[^\w\s]", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


async def find_existing_account(ts, *, domain: str | None = None, name: str | None = None):
    """The account this workspace already has for this company, or None.

    Tenant-scoped by construction: it goes through ``ts``, so it can only ever see the caller's own
    accounts. Deduping across tenants would be a data leak, not a feature.

    Archived accounts count as existing. A rep who archived Acme and re-adds it wants their old row
    back with its notes and history, not a second one — and silently creating a duplicate alongside
    an archived original is how a workspace ends up with both.
    """
    from sqlalchemy import func

    from nexus.models.account import Account

    normalised = normalise_domain(domain)
    if normalised:
        # Compare normalised-to-normalised. Stored domains are normalised on write (see
        # `normalise_on_write`), but rows created before that are not, so the candidate set is
        # fetched on a LIKE and then compared exactly — cheap, because the LIKE is selective.
        candidates = await ts.list(
            Account, Account.domain.ilike(f"%{normalised}%"), limit=25,
        )
        for candidate in candidates:
            if normalise_domain(candidate.domain) == normalised:
                return candidate
        return None

    # No usable domain: fall back to an exact normalised name match, and nothing looser.
    normalised_name = normalise_name(name)
    if not normalised_name:
        return None
    for candidate in await ts.list(
        Account, func.lower(Account.name).like(f"%{normalised_name[:40]}%"), limit=25,
    ):
        if normalise_name(candidate.name) == normalised_name and not (candidate.domain or ""):
            return candidate
    return None


def normalise_on_write(domain: str | None) -> str | None:
    """The domain to store. Keeps the raw value when it cannot be normalised.

    Storing the normalised form is what makes future comparisons cheap and stops the same company
    being written four ways. An unnormalisable value (a free-mail address, a bare label) is kept as
    given rather than blanked — it is still what the rep typed, and it may be all they have.
    """
    if domain is None:
        return None
    normalised = normalise_domain(domain)
    return normalised or (domain.strip() or None)
