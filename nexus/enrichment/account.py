"""Account (firmographic) enrichment: registered source databases first, then the open web.

Fills an Account's blank firmographics — industry, employee_count, country, tech_stack, a short
description — so accounts are useful even when no premium data provider (InfoJoy / Apollo /
ZoomInfo) is configured.

Two paths, cheapest first. A **registered source database** (`nexus/sources/`) is consulted ahead
of everything else: it costs nothing at the margin, because one vendor licence is amortised across
every tenant. Only if the basics are still blank does the **web** path run — ``get_search_provider()``,
which is Exa when a key is set and **falls back to DuckDuckGo automatically** otherwise, then an
LLM extraction. Offline-safe (stub LLM extracts nothing → no-op) and never raises; a source
database that is down simply falls through to the web, never blocking enrichment.

Deeper page scraping (Firecrawl / Scrapegraph-AI / a Camoufox browser) can slot in behind this
same shape later if search snippets prove too thin; they'd each need their own key/service.
"""
from __future__ import annotations

import json
import logging
import re

from nexus.models.account import Account

logger = logging.getLogger("nexus.enrichment.account")

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_obj(text: str | None) -> dict:
    if not text:
        return {}
    m = _JSON_OBJ_RE.search(text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class SearchBackedAccountEnricher:
    """Find and apply firmographics for an account from web search + LLM extraction."""

    def __init__(self, search, llm):
        self.search = search
        self.llm = llm

    async def fetch(self, account: Account) -> dict:
        label = (account.name or account.domain or "").strip()
        if not label:
            return {}
        query = f"{label} {account.domain or ''} company industry employees headquarters technology"
        try:
            hits = list(await self.search.search(query, limit=6) or [])
        except Exception as exc:  # provider isolation
            logger.warning("account enrich search failed for %r: %r", label, exc)
            return {}
        if not hits:
            return {}
        blob = "\n".join(
            f"- {getattr(h, 'title', '')}: {getattr(h, 'snippet', '')} ({getattr(h, 'url', '')})"
            for h in hits
        )
        try:
            from nexus.agents.llm import LLMMessage

            resp = await self.llm.complete(
                [
                    LLMMessage("system", "You extract company firmographics from web snippets. "
                               "Use only facts present in the snippets; never invent."),
                    LLMMessage("user", f"Company: {label} ({account.domain or 'unknown domain'}).\n"
                               f"Snippets:\n{blob}\n\n"
                               'Return JSON with keys: "industry" (string), "sub_industry" (a more '
                               'specific niche/sub-category, string), "employee_count" '
                               '(integer or null), "revenue" (annual revenue as a short string like '
                               '"$10M-$50M" or "", best estimate), "country" (string), "region" '
                               '(state/province/region, string), "city" (string), "description" (one '
                               'sentence), "tech_stack" (array of strings), "keywords" (array of 3-8 '
                               'short focus/SEO keywords describing what they do), "linkedin_url" '
                               "(the company's LinkedIn page URL if present, else \"\"). Use "
                               "null/\"\"/[] when unknown. JSON only, no prose."),
                ],
                temperature=0.0, max_tokens=400, purpose="account_enrich",
            )
        except Exception as exc:
            logger.warning("account enrich LLM failed for %r: %r", label, exc)
            return {}
        return _parse_obj(resp.text)

    def apply(self, account: Account, data: dict) -> list[str]:
        """Fill BLANK fields only — never overwrite existing CRM/user data. Returns filled keys."""
        filled: list[str] = []
        industry = (data.get("industry") or "").strip()
        if industry and not account.industry:
            account.industry = industry[:120]
            filled.append("industry")
        ec = data.get("employee_count")
        if isinstance(ec, bool):  # JSON true/false would sneak past the int check below
            ec = None
        if isinstance(ec, int) and ec > 0 and not account.employee_count:
            account.employee_count = ec
            filled.append("employee_count")
        country = (data.get("country") or "").strip()
        if country and not account.country:
            account.country = country[:80]
            filled.append("country")
        tech = data.get("tech_stack")
        if isinstance(tech, list) and tech and not account.tech_stack:
            account.tech_stack = [str(t).strip()[:60] for t in tech if str(t).strip()][:20]
            if account.tech_stack:
                filled.append("tech_stack")
        # description + linkedin_url live in custom_fields (no dedicated columns).
        cf = dict(account.custom_fields or {})
        cf_changed = False
        description = (data.get("description") or "").strip()
        if description and not cf.get("description"):
            cf["description"] = description[:500]
            cf_changed = True
            filled.append("description")
        linkedin = (data.get("linkedin_url") or "").strip()
        if linkedin and "linkedin.com" in linkedin.lower() and not cf.get("linkedin_url"):
            cf["linkedin_url"] = linkedin[:300]
            cf_changed = True
            filled.append("linkedin_url")
        # Extra look-alike dimensions (no dedicated columns) — blank-only string/array fields.
        for key, cap in (("sub_industry", 120), ("revenue", 60), ("region", 80), ("city", 80)):
            val = (data.get(key) or "").strip()
            if val and not cf.get(key):
                cf[key] = val[:cap]
                cf_changed = True
                filled.append(key)
        kw = data.get("keywords")
        if isinstance(kw, list) and kw and not cf.get("keywords"):
            cleaned = [str(k).strip()[:40].lower() for k in kw if str(k).strip()][:8]
            if cleaned:
                cf["keywords"] = cleaned
                cf_changed = True
                filled.append("keywords")
        if cf_changed:
            account.custom_fields = cf
        return filled

    async def from_source_db(self, account: Account) -> list[str]:
        """Fill blanks from a registered source database. Returns filled keys; never raises.

        Tried ahead of the web+LLM path because it is free at the margin: one vendor licence
        amortised across every tenant, instead of a search call and an LLM completion per account.
        A source that is down, slow or does not hold this domain returns nothing and `enrich`
        continues to the paid path — the locked posture for `nexus/sources/` is that it is an
        optimisation, never a dependency.
        """
        try:
            from nexus.sources.provider import enrich_company

            hit = await enrich_company(account.domain or "")
        except Exception as exc:  # provider isolation, same as every other provider here
            logger.warning("source database account enrich failed: %r", exc)
            return []
        if hit is None:
            return []
        # Routed through `apply` rather than assigning here, so a source database is held to the
        # same blank-only rule as the web path and can never overwrite a customer's CRM data.
        return self.apply(account, {
            "industry": hit.get("industry"),
            "country": hit.get("country"),
            # Parsed by the hit, not by `apply`: a source column may hold "250", 250, or a band
            # like "51-200" that is not one number, and `apply` only accepts a real int.
            "employee_count": hit.employee_count(),
        })

    async def enrich(self, account: Account) -> list[str]:
        """Fill blank firmographics. Source databases first, then the web. Returns fields filled."""
        filled = await self.from_source_db(account)

        # The paid path runs only if the basics are still missing — the same gate `pipeline.py`
        # applies before calling here at all. When a registered source answered in full there is
        # nothing left for a search call and an LLM completion to add, and that skipped pair is
        # exactly where the COGS saving lands.
        if account.industry and account.employee_count:
            return filled

        data = await self.fetch(account)
        if not data:
            return filled
        # `apply` is blank-only, so fields the source already filled are left alone and only the
        # genuinely new ones are reported.
        return filled + self.apply(account, data)


_enricher: SearchBackedAccountEnricher | None = None


def get_account_enricher() -> SearchBackedAccountEnricher:
    global _enricher
    if _enricher is None:
        from nexus.agents.llm import get_llm_provider
        from nexus.integrations.search.provider import get_search_provider

        _enricher = SearchBackedAccountEnricher(get_search_provider(), get_llm_provider())
    return _enricher


def set_account_enricher(enricher: SearchBackedAccountEnricher | None) -> None:
    global _enricher
    _enricher = enricher
