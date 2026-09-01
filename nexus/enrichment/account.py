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

import asyncio
import json
import logging
import re

from nexus.models.account import Account

logger = logging.getLogger("nexus.enrichment.account")

# What the billing seam charges for one firmographic crawl. Priced in `billing/rates.py` as
# "crawl + llm" — a search request plus an LLM completion, which is exactly what `fetch` spends.
ACCOUNT_CAPABILITY = "enrich.account"

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


# How long to wait before re-attempting an account the web had nothing for. Firmographics change on
# a scale of quarters; the refresh cycle runs in hours, so without this an account that can never be
# fully enriched issues a search request and an LLM completion on EVERY cycle and buys nothing each
# time. Measured live: 123 enrich.account events across 56 accounts, the largest single consumer of
# search credits in the product.
ENRICH_ATTEMPT_KEY = "enrich_attempted_at"


def should_attempt(account, *, force: bool) -> bool:
    """Is this account due a (paid) enrichment attempt?

    ``force`` is a PERSON pressing "Enrich" and is never throttled: the interval exists to stop a
    background sweep re-buying the same empty answer, not to tell a user who explicitly asked that
    nothing happened -- which reads as a broken button.

    Fails OPEN on anything unparseable. `custom_fields` is a free-form JSON column several paths
    write, and refusing to enrich because a timestamp is corrupt would be a silent, permanent
    outage for that one account.
    """
    if force:
        return True
    raw = (getattr(account, "custom_fields", None) or {}).get(ENRICH_ATTEMPT_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return True
    try:
        from datetime import datetime

        last = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        from datetime import timezone

        last = last.replace(tzinfo=timezone.utc)
    from nexus.core.config import get_settings
    from nexus.core.db import utcnow

    days = int(getattr(get_settings(), "account_enrich_min_interval_days", 30) or 0)
    if days <= 0:  # 0 disables the backoff entirely -- the pre-existing behaviour
        return True
    from datetime import timedelta

    return (utcnow() - last) >= timedelta(days=days)


def mark_attempted(account) -> None:
    """Record that we spent a request on this account, whatever it returned.

    Written on ATTEMPT, not on success: the expensive case is the account the web has nothing for,
    and recording only successes would leave exactly those accounts retrying forever.
    """
    from nexus.core.db import utcnow

    account.custom_fields = {
        **(getattr(account, "custom_fields", None) or {}),
        ENRICH_ATTEMPT_KEY: utcnow().isoformat(),
    }


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
                # 11 keys including a sentence of description and a keyword array. Measured live
                # 2026-08-27: at 400 the model stopped inside "description", `_parse_obj`
                # returned {}, and every candidate scored 65 against discovery's gate of 70 —
                # so nothing was ever enriched and nothing was ever discovered, silently.
                temperature=0.0, max_tokens=1200, purpose="account_enrich",
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

    async def from_source_db(self, account: Account) -> tuple[list[str], object | None]:
        """Fill blanks from a registered source database. ``(filled, hit)``; never raises.

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
            return [], None
        if hit is None:
            return [], None
        # Routed through `apply` rather than assigning here, so a source database is held to the
        # same blank-only rule as the web path and can never overwrite a customer's CRM data.
        filled = self.apply(account, {
            "industry": hit.get("industry"),
            "country": hit.get("country"),
            # Parsed by the hit, not by `apply`: a source column may hold "250", 250, or a band
            # like "51-200" that is not one number, and `apply` only accepts a real int.
            "employee_count": hit.employee_count(),
        })
        return filled, hit

    async def enrich(
        self, ts, account: Account, *, user_id: str | None = None,
        raise_on_block: bool = False, meter: bool = True, force: bool = False,
    ) -> list[str]:
        """Fill blank firmographics. Source databases first, then the web. Returns fields filled.

        ``ts`` is the requesting tenant's session, and it is **required** — this call spends a
        search request and an LLM completion, which is what ``enrich.account`` prices. Making it
        optional would recreate the state this method was in for most of the project's life: a
        capability sitting in the catalog with a rate card, metered at no call site.

        ``raise_on_block`` is the difference between a user pressing "Enrich" and a background
        sweep. A person gets a 402 carrying the upsell; the account-refresh pipeline, the ICP
        discovery run and the lookalike search get an empty list and carry on, because a quota on
        *enrichment* must never take down *signal collection*. Defaults to the safe one.

        ``meter=False`` is for **concurrent batch** callers only, and they must charge the batch
        themselves — see ``enrich_batch``. It exists because metering touches ``ts``, and
        SQLAlchemy's AsyncSession is not safe for concurrent use: N coroutines metering on one
        session interleave on a single connection and raise, or read each other's rows. The
        candidate sweeps in ``discovery/auto.py`` and ``lookalike/service.py`` are exactly that
        shape. Both callers are pinned by tests that assert the batch is charged once.
        """
        # Spacing between PAID attempts. Checked before the free source-database lookup so that
        # still runs -- it costs nothing and may fill the blanks on its own.
        due = should_attempt(account, force=force)

        filled, hit = await self.from_source_db(account)

        # The paid path runs only if the basics are still missing — the same gate `pipeline.py`
        # applies before calling here at all. When a registered source answered in full there is
        # nothing left for a search call and an LLM completion to add, and that skipped pair is
        # exactly where the COGS saving lands.
        if account.industry and account.employee_count:
            if hit is not None and meter:
                # Charged like the web crawl it replaced: the customer received firmographics and
                # is billed for the firmographics, not for our infrastructure. `cached` is what
                # keeps the margin visible. Never blocks — the answer is already in hand, and
                # this mirrors the shared-record hit in `nexus/people/enrich.py`.
                from nexus.sources.provider import meter_hit

                await meter_hit(ts, ACCOUNT_CAPABILITY, user_id=user_id,
                                source_name=getattr(hit, "source_name", ""))
            return filled

        if not due:
            # Attempted recently and the web had nothing to add. Skipping is the saving: this is
            # the account that would otherwise re-buy the same empty answer every refresh cycle.
            return filled

        if not (account.name or account.domain or "").strip():
            # `fetch` would return {} without issuing a request. Nothing was bought, so nothing is
            # charged — the same rule that keeps an unconfigured phone lookup off the bill.
            return filled

        if not meter:
            mark_attempted(account)
            data = await self.fetch(account)
            return filled + self.apply(account, data) if data else filled

        from nexus.billing.errors import QuotaExceeded
        from nexus.billing.meter import metered

        try:
            # Gated BEFORE the spend, which is the whole point of the context manager: a blocked
            # tenant must not have the search issued and then be told no.
            async with metered(
                ts, ACCOUNT_CAPABILITY, user_id=user_id, source="enrichment",
                attrs={"provider": "web", "cached": False},
            ):
                # Recorded on ATTEMPT, not on success. The expensive case is the account the web
                # has nothing for: marking only successes would leave exactly those accounts
                # retrying on every refresh cycle forever, which is the spend this exists to stop.
                mark_attempted(account)
                data = await self.fetch(account)
                if not data:
                    return filled
                # `apply` is blank-only, so fields the source already filled are left alone and
                # only the genuinely new ones are reported.
                filled = filled + self.apply(account, data)
        except QuotaExceeded:
            if raise_on_block:
                raise
            logger.info(
                "account enrichment skipped for %s: %s quota reached",
                account.id, ACCOUNT_CAPABILITY,
            )
        return filled

    async def enrich_batch(
        self, ts, accounts: list[Account], *, concurrency: int, user_id: str | None = None,
    ) -> None:
        """Enrich candidates concurrently, charging the batch once. Never raises.

        The single gate up front is not a shortcut, it is the only safe shape: ``metered`` reads
        and writes ``ts``, and running it inside the ``gather`` would put N coroutines on one
        AsyncSession — the concurrency trap CLAUDE.md documents for session-bound signal sources,
        which fails as an interleaved-connection error or, worse, as one coroutine reading
        another's rows. So the tenant is charged for ``len(accounts)`` before any of it starts,
        exactly as the bulk email verifier in ``routers/contacts.py`` does.

        Blocked means the whole batch is skipped rather than partially run: these are candidates
        for ranking, and a half-enriched set produces a silently worse ordering rather than a
        visible failure.
        """
        if not accounts:
            return

        from nexus.billing.errors import QuotaExceeded
        from nexus.billing.meter import metered

        sem = asyncio.Semaphore(max(1, concurrency))

        async def _one(acc: Account) -> None:
            async with sem:
                try:
                    await self.enrich(ts, acc, user_id=user_id, meter=False)
                except Exception:  # enrich already swallows its own errors; belt and braces
                    pass

        try:
            async with metered(
                ts, ACCOUNT_CAPABILITY, quantity=len(accounts), user_id=user_id,
                source="enrichment", attrs={"provider": "web", "cached": False, "batch": True},
            ):
                await asyncio.gather(*(_one(a) for a in accounts))
        except QuotaExceeded:
            logger.info(
                "candidate enrichment skipped for %d account(s): %s quota reached",
                len(accounts), ACCOUNT_CAPABILITY,
            )


_enricher: SearchBackedAccountEnricher | None = None


def get_account_enricher() -> SearchBackedAccountEnricher:
    global _enricher
    if _enricher is None:
        from nexus.agents.llm import get_llm_provider
        from nexus.integrations.search.provider import provider_for_task

        # The per-task provider, not the global one. Enrichment is the highest-volume search in
        # the product (measured: 123 of the billed search events across 56 accounts), and it issues
        # a plain query that any index answers — so it is the one task where paying Exa rates buys
        # nothing. Empty setting falls back to the global provider, so this is a no-op until an
        # operator picks one in the Control plane.
        _enricher = SearchBackedAccountEnricher(provider_for_task("enrichment"), get_llm_provider())
    return _enricher


def set_account_enricher(enricher: SearchBackedAccountEnricher | None) -> None:
    global _enricher
    _enricher = enricher
