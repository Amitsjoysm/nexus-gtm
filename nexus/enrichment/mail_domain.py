"""Which domain the organisation actually receives email on.

Every guessed address was built on `account.domain` — the website — and nothing checked that mail
goes there. A company whose site is `acme.io` but whose mail is `acme.com`, a site that redirects to
its real domain, a domain with no MX at all: each produced ten guesses at a place no mailbox exists,
and the finder then saved the least-bad one. That is the "wrong domain" half of the bounces.

The evidence ladder, strongest first:

1. **A colleague whose address is verified valid.** Proof, not inference: mail demonstrably arrives
   there for this company.
2. **Addresses published on the company's own site** (homepage and `/contact`), on a domain that
   belongs to the company — its website's name, or where the website redirects.
3. **Where the website redirects** (`acme.io` -> `acme.com`), if it accepts mail.
4. **The website domain**, if it accepts mail.

If nothing accepts mail, the answer is "no domain" and NOTHING is guessed — silence beats ten
addresses at a place that cannot receive them.

Resolved once per account and cached on `account.custom_fields["mail_domain"]` with its evidence and
a 30-day life, so an operator can see why an address was guessed where it was. The site fetch is
guarded by the same host rules as source databases (`sources/safety._is_blocked_host`): a domain is
tenant-typed input, and fetching whatever it names is otherwise an SSRF primitive.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from nexus.core.db import utcnow

logger = logging.getLogger("nexus.enrichment.mail_domain")

CACHE_KEY = "mail_domain"
TTL_DAYS = 30
#: Pages that carry a company's own addresses. Two fetches, not a crawl.
SITE_PATHS = ("", "/contact")
_FETCH_TIMEOUT_S = 10.0
#: Plenty for a contact page; a marketing homepage can be megabytes of inlined assets.
_MAX_PAGE_CHARS = 500_000
_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
#: Addresses that belong to a service, not to the company being researched.
_FOREIGN_LOCALS = frozenset({"noreply", "no-reply", "donotreply", "example", "you", "email"})


@dataclass(frozen=True)
class MailDomain:
    """The domain to guess on, and what proved it."""

    domain: str = ""
    #: verified_contacts | website_emails | website_redirect | website_domain | none
    source: str = "none"
    #: Addresses seen on the company's own site, kept so the finder can try a person's own first.
    published: tuple[str, ...] = ()
    #: Index into `name_patterns` that the company's verified addresses follow, when they agree.
    format_index: int | None = None


def normalise(domain: str | None) -> str:
    return (domain or "").strip().lower().lstrip("@").removeprefix("www.")


def _root(domain: str) -> str:
    """The name label of a domain: acme.com, acme.io and acme.co.uk all share "acme"."""
    parts = normalise(domain).split(".")
    return parts[-3] if len(parts) > 2 and len(parts[-2]) <= 3 else (parts[-2] if len(parts) > 1 else "")


def mail_domain_of(account) -> str:
    """The domain to guess addresses on: the resolved one when resolution has run, else the website.

    An account resolved to "no domain" returns "", and the finders then guess nothing — which is the
    point. Before resolution has ever run, this is exactly the old behaviour.
    """
    cached = (getattr(account, "custom_fields", None) or {}).get(CACHE_KEY)
    if isinstance(cached, dict) and "domain" in cached:
        return normalise(cached.get("domain"))
    return normalise(getattr(account, "domain", ""))


def cached_mail_domain(account) -> MailDomain | None:
    cached = (getattr(account, "custom_fields", None) or {}).get(CACHE_KEY)
    if not isinstance(cached, dict) or "domain" not in cached:
        return None
    index = cached.get("format_index")
    return MailDomain(
        domain=normalise(cached.get("domain")), source=str(cached.get("source") or "none"),
        published=tuple(cached.get("published") or ()),
        format_index=int(index) if isinstance(index, int) else None,
    )


def _fresh(account) -> bool:
    cached = (getattr(account, "custom_fields", None) or {}).get(CACHE_KEY) or {}
    checked = cached.get("checked_at")
    if not checked:
        return False
    try:
        from datetime import datetime

        when = datetime.fromisoformat(str(checked))
    except ValueError:
        return False
    if when.tzinfo is None:
        return False
    return (utcnow() - when) < timedelta(days=TTL_DAYS)


async def has_mx(domain: str) -> bool:
    """Whether this domain can receive mail at all (MX, else A/AAAA as the implicit MX)."""
    import asyncio

    def _check() -> bool:
        import dns.resolver  # lazy: dnspython is only needed when resolution actually runs

        resolver = dns.resolver.Resolver()
        resolver.timeout = resolver.lifetime = 5.0
        for rtype in ("MX", "A", "AAAA"):
            try:
                if list(resolver.resolve(domain, rtype)):
                    return True
            except Exception:
                continue
        return False

    try:
        return await asyncio.to_thread(_check)
    except Exception:
        return False


async def fetch_site(domain: str) -> tuple[str, list[str]]:
    """``(final host, addresses published on the site)``. Never raises; never fetches a private host."""
    from nexus.sources.safety import _is_blocked_host

    blocked, why = _is_blocked_host(domain, allow_private=False)
    if blocked:
        logger.info("not fetching %s: %s", domain, why)
        return "", []

    import httpx

    final_host, found = "", []
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_S, follow_redirects=True,
            headers={"User-Agent": "NexusGTM/1.0 (+contact-discovery)"},
        ) as client:
            for path in SITE_PATHS:
                try:
                    resp = await client.get(f"https://{domain}{path}")
                except Exception:
                    continue
                if resp.status_code >= 400:
                    continue
                final_host = final_host or normalise(urlsplit(str(resp.url)).hostname or "")
                found += _EMAIL.findall(resp.text[:_MAX_PAGE_CHARS])
    except Exception as exc:
        logger.info("site fetch failed for %s: %r", domain, exc)
    return final_host, [e.lower() for e in found]


def _company_addresses(published: list[str], *, website: str, final_host: str) -> list[str]:
    """Published addresses that belong to THIS company, not to a vendor or a privacy notice."""
    roots = {r for r in (_root(website), _root(final_host)) if r}
    keep = []
    for address in published:
        local, _, host = address.partition("@")
        if local in _FOREIGN_LOCALS:
            continue
        if host in (website, final_host) or (_root(host) and _root(host) in roots):
            keep.append(address)
    return list(dict.fromkeys(keep))


def infer_format_index(contacts) -> int | None:
    """Which name pattern this company's VERIFIED addresses follow, when they agree.

    Two colleagues at `jdoe@` and `jsmith@` say the format is first-initial+last, so that guess goes
    first — which matters most on a catch-all domain, where the one address returned cannot be
    proven and had better be the company's actual format rather than a default first.last.
    """
    from nexus.enrichment.providers import name_patterns
    from nexus.verification import STATUS_VALID

    votes: Counter[int] = Counter()
    for contact in contacts:
        if contact.email_status != STATUS_VALID or not contact.email:
            continue
        local = (contact.email or "").split("@")[0].lower()
        patterns = name_patterns(contact.full_name or "")
        if local in patterns:
            votes[patterns.index(local)] += 1
    if not votes:
        return None
    index, count = votes.most_common(1)[0]
    return index if count >= 1 else None


@dataclass
class _Evidence:
    candidates: list[tuple[str, str]] = field(default_factory=list)  # (domain, source)
    published: list[str] = field(default_factory=list)
    format_index: int | None = None


async def _gather(ts, account, *, fetch) -> _Evidence:
    from nexus.models.account import Contact
    from nexus.verification import STATUS_VALID

    evidence = _Evidence()
    website = normalise(account.domain)

    contacts = await ts.list(Contact, Contact.account_id == account.id)
    evidence.format_index = infer_format_index(contacts)
    proven = Counter(
        (c.email or "").split("@")[-1].lower()
        for c in contacts
        if c.email_status == STATUS_VALID and "@" in (c.email or "")
    )
    for domain, _ in proven.most_common(1):
        if domain:
            evidence.candidates.append((domain, "verified_contacts"))

    if website:
        final_host, published = await fetch(website)
        evidence.published = _company_addresses(published, website=website, final_host=final_host)
        for address in evidence.published:
            host = address.split("@")[-1]
            if host not in [d for d, _ in evidence.candidates]:
                evidence.candidates.append((host, "website_emails"))
        if final_host and final_host != website:
            evidence.candidates.append((final_host, "website_redirect"))
        evidence.candidates.append((website, "website_domain"))
    return evidence


async def resolve_mail_domain(ts, account, *, force: bool = False, fetch=None, mx=None) -> MailDomain:
    """The organisation's email domain, resolved from evidence and cached on the account."""
    if not force:
        cached = cached_mail_domain(account)
        if cached is not None and _fresh(account):
            return cached

    fetch = fetch or fetch_site
    mx = mx or has_mx
    try:
        evidence = await _gather(ts, account, fetch=fetch)
    except Exception as exc:  # resolution must never break enrichment
        logger.warning("mail domain resolution failed for %s: %r", account.id, exc)
        return MailDomain(domain=normalise(account.domain), source="website_domain")

    resolved = MailDomain(domain="", source="none", published=tuple(evidence.published),
                          format_index=evidence.format_index)
    for domain, source in evidence.candidates:
        # A colleague's verified address is proof that mail arrives there; everything else has to
        # show it can receive mail at all.
        if source == "verified_contacts" or await mx(domain):
            resolved = MailDomain(domain=domain, source=source,
                                  published=tuple(evidence.published),
                                  format_index=evidence.format_index)
            break

    fields = dict(getattr(account, "custom_fields", None) or {})
    fields[CACHE_KEY] = {
        "domain": resolved.domain, "source": resolved.source,
        "published": list(resolved.published)[:20], "format_index": resolved.format_index,
        "checked_at": utcnow().isoformat(),
    }
    account.custom_fields = fields  # reassigned so SQLAlchemy sees the JSON change
    return resolved
