# nexus/enrichment/b2b_actor.py
"""Structured firmographics from an Apify actor, with no LLM in the loop.

`teodor_banea/b2b-lead-enrichment-free` returns already-parsed company fields — industry,
description, LinkedIn URL, tech stack, and where its paid providers are configured, size, geography
and revenue. That matters more than "one more provider": the existing web path is a SEARCH plus an
LLM completion that extracts fields out of page text, and the extraction half is the fragile half.

Measured on the live deployment 2026-09-09: enriching `anthropic.com` took 66.6 seconds and filled
NOTHING. Exa answered 200 every time; Groq answered `retry-after=862s` on one key and 15s on the
other, the chain fell through to the offline stub, and the stub extracts nothing. A company with one
of the most documented web presences in the industry came back blank because the reader was down,
not because the web was quiet.

This actor has no reader to be down. It is therefore tried FIRST and the web+LLM path becomes the
fallback, which is the opposite of the usual ordering here and is deliberate.

Two fields are deliberately NOT taken from it:

* `companyName` is the scraped page title. It returned "Home \\ Anthropic" for anthropic.com, and
  an account renamed to a page title is worse than an account with the name the customer typed.
* `personEmail` / `personPhone` — contact enrichment is a separate capability with its own
  waterfall, its own consent posture and its own price. Quietly harvesting people here would bill
  the wrong meter and bypass `nexus/people/`.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("nexus.enrichment.b2b_actor")

#: Registered in `nexus/integrations/apify.py`. A logical name, so swapping the actor is one line
#: there rather than an edit here.
ACTOR = "b2b_enrichment"

#: The actor scrapes a site, resolves DNS/SSL and queries GitHub, so it is slower than a search
#: call. Bounded because this runs inside a user-facing enrich, and an unbounded provider on that
#: path is the 48-second "Add company" this codebase just finished removing.
TIMEOUT_S = 45.0

#: Free-tier providers only. `google-kg` is listed by the actor but needs an API key we do not
#: supply, and asking for it without one costs a provider timeout per row for nothing.
_PROVIDER_ORDER = ["website-scraper", "ssl-cert", "dns-mx", "github"]


def _int_or_none(value) -> int | None:
    """A real employee count, or nothing. Ranges like "51-200" are not one number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip().replace(",", "")
    return int(text) if text.isdigit() and int(text) > 0 else None


def _revenue_text(value) -> str:
    """Revenue as the SHORT DISPLAY STRING `apply` stores, e.g. "$32.3M".

    Deliberately not an int for `accounts.annual_revenue`: `apply` is the single place a provider's
    answer becomes account state, it stores revenue as a string in `custom_fields`, and widening it
    for one new provider would give this actor a write path the others do not have. A number the
    actor happens to return is formatted into that same shape instead.
    """
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        if value <= 0:
            return ""
        for scale, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
            if value >= scale:
                return f"${value / scale:.1f}".rstrip("0").rstrip(".") + suffix
        return f"${int(value)}"
    text = str(value).strip()
    return text[:60] if text else ""


def to_fields(row: dict) -> dict:
    """The actor's row mapped onto the names `SearchBackedAccountEnricher.apply` understands.

    Routed through `apply` rather than assigned here, for the reason `from_source_db` states: a new
    provider must be held to the same blank-only rule and can never overwrite a customer's own data.
    """
    if not isinstance(row, dict):
        return {}
    industries = row.get("companyIndustries")
    industry = (row.get("companyIndustry") or "").strip()
    if not industry and isinstance(industries, list) and industries:
        industry = str(industries[0]).strip()

    tech = [str(t).strip() for t in (row.get("techStack") or []) if str(t).strip()]
    return {
        "industry": industry,
        "employee_count": _int_or_none(row.get("companySizeEmployees")),
        "country": (row.get("companyCountry") or "").strip(),
        "region": (row.get("companyRegion") or "").strip(),
        "city": (row.get("companyCity") or "").strip(),
        "description": (row.get("companyDescription") or "").strip(),
        "linkedin_url": (row.get("companyLinkedinUrl") or "").strip(),
        "revenue": _revenue_text(row.get("companyAnnualRevenue")),
        "tech_stack": tech,
        # Deliberately absent: `companyName` (a scraped page title) and every `person*` field.
    }


async def fetch(domain: str) -> dict:
    """Firmographics for one domain. `{}` for anything that is not a clean answer. Never raises."""
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain or "." not in domain:
        return {}
    from nexus.integrations.apify import ApifyNotConfigured, get_apify_client

    client = get_apify_client()
    try:
        async with asyncio.timeout(TIMEOUT_S):
            items = await client.run_actor(
                ACTOR,
                {
                    # The actor reads a CSV, so one company is a header plus one row.
                    "inputCsvSource": "paste",
                    "inputCsvContent": f"domain\n{domain}",
                    "maxRows": 1,
                    "enableFirmographics": True,
                    "enableTechnographics": True,
                    # Contacts are `nexus/people/`'s job, on its own meter and its own waterfall.
                    "enableContacts": False,
                    "includeRawResponses": False,
                    "firmographicProviderOrder": list(_PROVIDER_ORDER),
                    "technographicProviderOrder": list(_PROVIDER_ORDER),
                },
                timeout=TIMEOUT_S,
            )
    except ApifyNotConfigured:
        return {}
    except TimeoutError:
        logger.info("b2b enrichment for %s exceeded %ss; falling through to the web", domain, TIMEOUT_S)
        return {}
    except Exception as exc:  # provider isolation, as every other provider here
        logger.warning("b2b enrichment actor failed for %s: %r", domain, exc)
        return {}

    rows = [r for r in (items or []) if isinstance(r, dict)]
    if not rows:
        return {}
    # THE ROW MUST BE ABOUT THE DOMAIN WE ASKED FOR. The actor takes a list and returns a dataset;
    # taking row zero is how a stranger's firmographics land on a customer's account — the
    # wrong-attribution failure `nexus/companies/` has shipped six times.
    for row in rows:
        got = (row.get("companyDomain") or "").strip().lower()
        if not got or got == domain or got.endswith("." + domain) or domain.endswith("." + got):
            return to_fields(row)
    logger.info("b2b enrichment for %s returned only other domains; discarding", domain)
    return {}
