"""Web-backed account (firmographic) enrichment.

Fills an Account's blank firmographics — industry, employee_count, country, tech_stack, a short
description — from the open web, so accounts are useful even when no premium data provider
(InfoJoy / Apollo / ZoomInfo) is configured. It goes through ``get_search_provider()``, which is
Exa when a key is set and **falls back to DuckDuckGo automatically** otherwise, then has the LLM
extract structured fields. Offline-safe (stub LLM extracts nothing → no-op) and never raises.

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
                               'Return JSON with keys: "industry" (string), "employee_count" '
                               '(integer or null), "country" (string), "description" (one '
                               'sentence), "tech_stack" (array of strings), "linkedin_url" (the '
                               "company's LinkedIn page URL if present, else \"\"). Use null/\"\"/[] "
                               "when unknown. JSON only, no prose."),
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
        if cf_changed:
            account.custom_fields = cf
        return filled

    async def enrich(self, account: Account) -> list[str]:
        """Fetch from the web and apply to the account. Returns the list of fields filled."""
        data = await self.fetch(account)
        if not data:
            return []
        return self.apply(account, data)


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
