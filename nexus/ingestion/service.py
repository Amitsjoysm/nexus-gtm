"""Ingestion service: normalize, dedupe, and persist signals; emit events."""
from __future__ import annotations

import asyncio
import hashlib
import logging

from nexus.core.config import get_settings
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
        """Pull from all configured sources and ingest the results."""
        timeout = get_settings().source_timeout_s
        collected: list[RawSignal] = []
        for src in self.sources:
            try:
                collected.extend(await asyncio.wait_for(src.fetch(account), timeout=timeout))
            except asyncio.TimeoutError:
                logger.warning("signal source %s timed out after %.1fs", src.name, timeout)
            except Exception:  # a broken source must not stop ingestion
                logger.warning("signal source %s failed", getattr(src, "name", src), exc_info=True)
        return await self.ingest(ts, account, collected)


_service: IngestionService | None = None


def get_ingestion_service() -> IngestionService:
    global _service
    if _service is None:
        from nexus.ingestion.sources import DemoSignalSource, WebNewsSource
        from nexus.enrichment.browser import get_browser_provider

        sources: list[SignalSource] = []
        if get_settings().demo_signals_enabled:
            # Synthetic signals: local/dev only. Production must not fabricate events.
            sources.append(DemoSignalSource())
        sources.append(WebNewsSource(get_browser_provider()))
        _service = IngestionService(sources=sources)
    return _service


def set_ingestion_service(service: IngestionService) -> None:
    global _service
    _service = service
