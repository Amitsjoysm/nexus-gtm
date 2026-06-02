"""Ingestion service: normalize, dedupe, and persist signals; emit events."""
from __future__ import annotations

import asyncio
import logging

from nexus.core.config import get_settings
from nexus.core.events import Event, get_event_bus
from nexus.core.tenancy import TenantSession
from nexus.ingestion.sources import RawSignal, SignalSource
from nexus.models.account import Account
from nexus.models.signal import SIGNAL_KINDS, SignalEvent

logger = logging.getLogger("nexus.ingestion")


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
            if r.dedupe_key in existing:
                continue
            existing.add(r.dedupe_key)
            ev = SignalEvent(
                tenant_id=ts.tenant_id,
                account_id=account.id,
                contact_id=r.contact_id,
                kind=r.kind,
                source=r.source,
                title=r.title,
                body=r.body,
                url=r.url,
                strength=r.resolved_strength(),
                dedupe_key=r.dedupe_key,
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

        _service = IngestionService(
            sources=[DemoSignalSource(), WebNewsSource(get_browser_provider())]
        )
    return _service


def set_ingestion_service(service: IngestionService) -> None:
    global _service
    _service = service
