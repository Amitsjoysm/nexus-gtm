"""Ingestion service: normalize, dedupe, and persist signals; emit events."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from nexus.core import metrics
from nexus.core.config import get_settings
from nexus.core.db import utcnow
from nexus.core.events import Event, get_event_bus
from nexus.core.tenancy import TenantSession
from nexus.ingestion.sources import RawSignal, SignalSource
from nexus.models.account import Account
from nexus.models.signal import SIGNAL_KINDS, SignalEvent

logger = logging.getLogger("nexus.ingestion")

# SignalEvent column limits. Postgres enforces VARCHAR lengths (SQLite does not, so the
# offline suite can't catch overflows); real-world titles/URLs from web sources routinely
# exceed them, so every wire-derived value is clamped at this single choke point.
_MAX_TITLE = 400
_MAX_URL = 500
_MAX_SOURCE = 60
_MAX_DEDUPE = 200


def _clamp(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[:limit]


def _clamp_dedupe(key: str) -> str:
    """Bound the dedupe key without losing uniqueness: over-long keys keep a readable
    prefix plus a digest of the FULL key, so two distinct long keys can't collide the
    way plain truncation would."""
    if len(key) <= _MAX_DEDUPE:
        return key
    digest = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{key[:_MAX_DEDUPE - len(digest) - 1]}:{digest}"


class IngestionService:
    def __init__(self, sources: list[SignalSource] | None = None):
        self.sources = sources or []

    async def ingest(
        self, ts: TenantSession, account: Account, raw: list[RawSignal]
    ) -> list[SignalEvent]:
        """Persist new signals (idempotent by dedupe_key) and publish ``signal.created``."""
        if not raw:
            return []

        existing = {
            s.dedupe_key
            for s in await ts.list(SignalEvent, SignalEvent.account_id == account.id)
        }
        bus = get_event_bus()
        created: list[SignalEvent] = []
        for r in raw:
            if r.kind not in SIGNAL_KINDS:
                continue
            dedupe_key = _clamp_dedupe(r.dedupe_key)
            if dedupe_key in existing:
                continue
            existing.add(dedupe_key)
            ev = SignalEvent(
                tenant_id=ts.tenant_id,
                account_id=account.id,
                contact_id=r.contact_id,
                kind=r.kind,
                source=_clamp(r.source, _MAX_SOURCE),
                title=_clamp(r.title, _MAX_TITLE),
                body=r.body,
                url=_clamp(r.url, _MAX_URL),
                strength=r.resolved_strength(),
                dedupe_key=dedupe_key,
                occurred_at=r.occurred_at,
            )
            ts.add(ev)
            created.append(ev)

        if created:
            await ts.flush()
            # In the SAME transaction as the signals. A subscriber on the event bus cannot do this:
            # at publish time the signal is flushed but not committed, so a second session cannot
            # see it, and `alerts.signal_id` is a foreign key it could not satisfy. Found by
            # deploying — the bus version produced five signals and zero alerts.
            from nexus.alerts.signal_alerts import raise_alerts_for

            await raise_alerts_for(ts, account, created)
            for ev in created:
                await bus.publish(
                    Event(
                        name="signal.created",
                        tenant_id=ts.tenant_id,
                        payload={"signal_id": ev.id, "account_id": account.id, "kind": ev.kind},
                    )
                )
        return created

    async def run_sources(self, ts: TenantSession, account: Account) -> list[SignalEvent]:
        """Pull from all configured sources and ingest the results.

        Each source's run is recorded (M16) whether or not it produced anything. Before that, a
        source that had been failing for a week was indistinguishable from a quiet market: both
        looked like "no new signals", and the first sign of trouble was a rep asking why an
        obviously-funded account showed no round.
        """
        settings = get_settings()
        default_timeout = settings.source_timeout_s
        if await self._over_daily_budget(ts, settings.tenant_daily_source_runs):
            # Refuse the crawl, not the request. Returning [] leaves existing signals intact and
            # the next UTC day resumes automatically; raising would turn a spend guard into an
            # outage on the account page.
            logger.warning(
                "tenant %s hit the daily crawl budget of %s source runs; skipping",
                ts.tenant_id, settings.tenant_daily_source_runs,
            )
            metrics.record_source_run("budget", "throttled", 0)
            return []
        collected: list[RawSignal] = []
        runs: list[dict] = []
        for src in self.sources:
            # A source may declare its own budget. The default 8s assumes one request; a source
            # that issues several — and deliberately spaces them so a keyless backend does not
            # start refusing — needs longer, or it is killed mid-run every time and reports
            # nothing, which is indistinguishable from "this account has no signals".
            timeout = getattr(src, "timeout_s", None) or default_timeout
            # Most sources are a pure function of the account. A change detector is not: it needs
            # the stored baseline, so it asks for the session rather than opening its own (which
            # would sit outside the caller's transaction and its RLS binding).
            if hasattr(src, "bind_session"):
                src.bind_session(ts)
            name = getattr(src, "name", str(src))
            started = utcnow()
            clock = time.perf_counter()
            outcome, error, found = "ok", "", 0
            try:
                items = await asyncio.wait_for(src.fetch(account), timeout=timeout)
                collected.extend(items)
                found = len(items)
                # `empty` is not `ok`: a source that runs cleanly and finds nothing every single
                # time is a broken source, and merging the two states hides exactly that.
                outcome = "ok" if found else "empty"
            except asyncio.TimeoutError:
                outcome, error = "timeout", f"timed out after {timeout:.1f}s"
                logger.warning("signal source %s timed out after %.1fs", name, timeout)
            except Exception as exc:  # a broken source must not stop ingestion
                outcome, error = "error", f"{type(exc).__name__}: {exc}"[:2000]
                logger.warning("signal source %s failed", name, exc_info=True)
            runs.append({
                "source": name,
                "outcome": outcome,
                "items_found": found,
                "error": error,
                "duration_ms": round((time.perf_counter() - clock) * 1000, 2),
                "provenance": dict(getattr(src, "last_provenance", {}) or {}),
                "started_at": started,
                "finished_at": utcnow(),
            })
            metrics.record_source_run(name, outcome, found)

        created = await self.ingest(ts, account, collected)
        await self._record_runs(ts, account, runs, created)
        return created

    async def _over_daily_budget(self, ts: TenantSession, cap: int) -> bool:
        """Whether this tenant has already spent today's crawl budget.

        Counts `signal_source_runs` rows for the current UTC day. The crawl history is the ledger,
        so the count cannot drift from what was actually spent the way a separate counter would.
        Never raises: a budget check that fails closed would stop signal collection because of a
        bookkeeping problem, which is the wrong trade.
        """
        if cap <= 0:
            return False
        try:
            from datetime import datetime, time, timezone

            from sqlalchemy import func, select

            from nexus.models.source_run import SignalSourceRun

            midnight = datetime.combine(utcnow().date(), time.min, tzinfo=timezone.utc)
            spent = await ts.session.scalar(
                select(func.count())
                .select_from(SignalSourceRun)
                .where(
                    SignalSourceRun.tenant_id == ts.tenant_id,
                    SignalSourceRun.started_at >= midnight,
                )
            )
            return int(spent or 0) >= cap
        except Exception:
            logger.warning("daily crawl budget check failed; allowing", exc_info=True)
            return False

    async def _record_runs(
        self, ts: TenantSession, account: Account, runs: list[dict], created: list[SignalEvent]
    ) -> None:
        """Persist the crawl history. Never raises: bookkeeping must not lose the signals it
        describes, so a failed write is logged and the ingested signals still stand."""
        if not runs:
            return
        from nexus.models.source_run import SignalSourceRun

        # Attribute new signals back to the source that produced them, so `items_new` answers
        # "is this source finding anything we did not already have?".
        new_by_source: dict[str, int] = {}
        for ev in created:
            new_by_source[ev.source or ""] = new_by_source.get(ev.source or "", 0) + 1
        try:
            for run in runs:
                ts.add(
                    SignalSourceRun(
                        tenant_id=ts.tenant_id,
                        account_id=account.id,
                        items_new=new_by_source.get(run["source"], 0),
                        **run,
                    )
                )
            await ts.flush()
        except Exception:
            logger.warning("failed to record signal source runs", exc_info=True)


_service: IngestionService | None = None


def get_ingestion_service() -> IngestionService:
    global _service
    if _service is None:
        from nexus.ingestion.sources import (
            AtsSignalSource,
            DemoSignalSource,
            DorkedSearchSource,
            PublicApiSignalSource,
            RssSignalSource,
            WebNewsSource,
            WebsiteWatchSignalSource,
        )
        from nexus.enrichment.browser import get_browser_provider

        settings = get_settings()
        selected = settings.signal_source_list
        sources: list[SignalSource] = []
        if settings.demo_signals_active:
            # Synthetic signals: local/dev only. `demo_signals_active` is force-false in
            # staging/prod, so production can never fabricate events even if the flag is set.
            sources.append(DemoSignalSource())
        sources.append(WebNewsSource(get_browser_provider()))
        # Dork-backed search: one precise query per signal kind, biased to recent results. ON by
        # default because the broad query alone systematically loses the strongest signals — a
        # funding round competing with three press mentions for six relevance-ranked slots. It
        # runs ALONGSIDE WebNewsSource, not instead of it; they fail differently, and shared
        # event-bucketed dedupe collapses anything both of them find.
        #
        # Every dork is one billed search call, so `no_dorks` is the lever for an operator who
        # wants the cheaper pipeline back.
        if "no_dorks" not in selected:
            # Pace only the keyless backend: it is the one with anti-bot heuristics rather than a
            # contractual rate limit. An operator can override either way.
            pace = settings.signal_dork_pace_s
            effective = (
                settings.signal_search_provider or settings.search_provider
            ).strip().lower()
            if pace <= 0 and effective in ("duckduckgo", "ddg", ""):
                pace = 1.5
            sources.append(
                DorkedSearchSource(
                    max_queries=settings.signal_dork_max_queries, pace_s=pace
                )
            )
        # The account's own ATS board (M17). Keyless and first-party — the strongest hiring
        # evidence there is — so it is on by default; `no_ats` opts out for a deployment that
        # does not want the careers-page crawl.
        if "no_ats" not in selected:
            sources.append(AtsSignalSource())
        # SEC EDGAR / GitHub / Hacker News (M18-M19). Keyless, and each sub-source degrades on its
        # own — GitHub exhausting its 60/hour budget must not stop EDGAR reporting a 10-Q.
        if "no_public_apis" not in selected:
            sources.append(PublicApiSignalSource(github_token=settings.github_token))
        # Website change monitoring (M20). Opt-in: it fetches up to four pages per account per
        # refresh, which is the heaviest source in the pipeline, and it is only useful once a
        # baseline exists — so an operator should turn it on deliberately.
        if "website" in selected:
            sources.append(WebsiteWatchSignalSource())
        # RSS/Atom company feeds (blog / newsroom / press). Opt-in via NEXUS_SIGNAL_SOURCES=...,rss
        # so the default pipeline is byte-for-byte unchanged.
        if "rss" in selected:
            sources.append(RssSignalSource())
        _service = IngestionService(sources=sources)
    return _service


def set_ingestion_service(service: IngestionService) -> None:
    global _service
    _service = service
