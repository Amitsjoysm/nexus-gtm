"""What a draft may say about a company and its signals (SDR feedback, 2026-10-26, D8).

Decided with the owner: drafts use verified facts, the current facts minus the wrong ones, the
earlier emails, the person's LinkedIn profile and posts, and strong signals. Measured on the local
data before this: 57% of accounts with signals had only generic news among the eight a draft read,
37% only 0.4 mentions, "Known tech" listed DMARC and Let's Encrypt, and industry and country were
often the ICP's own words stamped on a web find (fixed forward on 2026-10-26, but still on the rows
written before). One module, read by the composer, the call script and campaign drafts.
"""
from __future__ import annotations

from datetime import datetime, timezone

#: Website plumbing an enrichment reads off DNS and headers. True, and nothing a buyer would want an
#: email to mention.
INFRASTRUCTURE_TECH = frozenset({
    "dmarc", "spf", "dkim", "bimi", "let's encrypt", "letsencrypt", "cloudflare", "cloudflare cdn",
    "akamai", "fastly", "amazon cloudfront", "cloudfront", "proofpoint", "mimecast", "digicert",
    "sectigo", "godaddy ssl", "google tag manager", "hsts", "http/2", "http/3", "ipv6",
})

#: The web step of ICP discovery stamped the ICP's first industry and first country on every web
#: find until this day (`scripts/quarantine_stamped_companies.py`).
STAMP_FIXED = datetime(2026, 10, 26, tzinfo=timezone.utc)
WEB_FIND_SOURCES = ("auto_discovery", "discovery")


def useful_tech(stack) -> list[str]:
    return [str(t).strip() for t in (stack or [])
            if str(t).strip() and str(t).strip().lower() not in INFRASTRUCTURE_TECH]


def looks_stamped(account, field: str, icp: dict | None) -> bool:
    """Is ``account.<field>`` likely the ICP's own word stamped by the old web step?

    All of: the account was a web find from before the fix, nothing recorded where the value came
    from, and it equals the ICP's FIRST industry (or country), which is what the stamp wrote. A
    label LinkedIn or enrichment wrote carries its source and is kept."""
    if not icp or (getattr(account, "source", "") or "") not in WEB_FIND_SOURCES:
        return False
    created = getattr(account, "created_at", None)
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created >= STAMP_FIXED:
            return False
    sources = getattr(account, "field_sources", None) or {}
    if isinstance(sources, dict) and field in sources:
        return False
    wanted = {"industry": "industries", "country": "countries"}.get(field)
    first = ((icp.get(wanted) or [None])[0] if wanted else None) or ""
    value = getattr(account, field, None) or ""
    return bool(first) and value.strip().lower() == str(first).strip().lower()


def citable_signals(signals) -> list:
    """The signals a draft may cite: events (the alert floor, `signal_alert_floor`, 0.5), inside
    the platform's AI window. A 0.4 mention is a company's own blog post, not a reason to write."""
    from nexus.core.config import get_settings
    from nexus.ingestion.window import within_ai_window

    floor = float(getattr(get_settings(), "signal_alert_floor", 0.5) or 0.5)
    return [s for s in within_ai_window(signals) if (getattr(s, "strength", 0) or 0) >= floor]
