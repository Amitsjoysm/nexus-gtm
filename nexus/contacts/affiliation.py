# nexus/contacts/affiliation.py
"""Does a search result PROVE that a person works at an account?

Reported from staging 2026-09-10: "Find contacts" on devbay.com added a person at a completely
different company. The source, reproduced locally, was a leadership page on ANOTHER company's domain
(`softmount.devsoftmount.com/leadership/`, titled "Leadership – Devbay"); the extraction took everyone
on it, and `LinkedInFinder` then attached a profile to each by name alone — a namesake's.

Both trusted a NAME MATCH, which is the mistake this codebase has made six times for companies. For
people it is worse: a rep phones a stranger with somebody else's context. So a person is attributed
to an account only on evidence that is about the account and not merely about its name:

* **the account's own domain** — its team / about / leadership page. A company is authoritative
  about who works for it; `team.devbay.com` counts, `devsoftmount.com` does not.
* **LinkedIn naming this company** — the one third-party site where a person states their own
  employer. The profile (or company page) must name the company, not just rank for it.

Everything else — data-vendor pages, news, another company's site with a similar name — is a hint,
never proof, however plausible it reads. That costs some recall on companies with no team page and
thin LinkedIn coverage, and it is the right way round: a missing contact is a rep with one fewer
call to make; a wrong one is a rep calling the wrong human.
"""
from __future__ import annotations

import re

#: Below this length a company phrase must appear as a WHOLE WORD: "Opp" is inside "opportunity".
#: At or above it, squashed containment is safe and catches "Devbay Technologies" / "devbay.com".
_SQUASH_MIN = 6


def _field(hit, name: str) -> str:
    if isinstance(hit, dict):
        return str(hit.get(name) or "")
    return str(getattr(hit, name, "") or "")


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _label(url_or_domain: str) -> str:
    # One definition of "the label that IS the company" (subdomains, `.co.uk`), shared with the
    # ATS source that first needed it.
    from nexus.ingestion.ats import _identity_token

    return _identity_token(url_or_domain or "")


def _company_phrases(account) -> list[str]:
    phrases = [(getattr(account, "name", None) or "").strip()]
    domain = (getattr(account, "domain", None) or "").strip()
    if domain:
        phrases.append(_label(domain))
    return [p for p in phrases if len(_squash(p)) >= 2]


def mentions_company(text: str, account) -> bool:
    """Whether ``text`` names this account, as a word for short names and squashed for long ones."""
    lowered, squashed = (text or "").lower(), _squash(text)
    for phrase in _company_phrases(account):
        sq = _squash(phrase)
        if len(sq) >= _SQUASH_MIN:
            if sq in squashed:
                return True
        elif re.search(rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])", lowered):
            return True
    return False


def names_person(hit, full_name: str) -> bool:
    """Whether this result is about this person: every name token appears in it."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", (full_name or "").lower()) if len(t) >= 2]
    hay = " ".join(_field(hit, f) for f in ("url", "title", "snippet")).lower()
    return bool(tokens) and all(t in hay for t in tokens)


def proves_affiliation(hit, account) -> bool:
    """Whether this result is evidence ABOUT the account — its own site, or LinkedIn naming it."""
    host = _label(_field(hit, "url"))
    own = _label(getattr(account, "domain", None) or "")
    if own and host == own:
        return True
    if host == "linkedin":
        text = " ".join(_field(hit, f) for f in ("title", "snippet", "url"))
        return mentions_company(text, account)
    return False


def canonical_url(url: str | None) -> str:
    """Comparable form of a URL: no scheme, no `www.`, no trailing slash, lowercase."""
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")
