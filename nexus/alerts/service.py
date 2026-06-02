"""Alert service: create tenant-scoped alerts and fan them out across delivery channels.

Channels are pluggable. ``in_app`` simply persists (the API/UI reads it back); ``webhook`` POSTs
to a configured URL; ``email`` is a deterministic stub that logs (swap for a real provider later).
Delivery failures never raise — an alert is always persisted so nothing is silently lost.
"""
from __future__ import annotations

import logging

import httpx

from nexus.core.config import get_settings
from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.alerts import ALERT_CHANNELS, ALERT_SEVERITIES, Alert

logger = logging.getLogger("nexus.alerts")


class AlertService:
    async def create(
        self,
        ts: TenantSession,
        *,
        title: str,
        body: str = "",
        severity: str = "info",
        channel: str = "in_app",
        account_id: str | None = None,
        signal_id: str | None = None,
        source: str = "system",
        meta: dict | None = None,
    ) -> Alert:
        """Persist an alert and attempt delivery on its channel."""
        if severity not in ALERT_SEVERITIES:
            severity = "info"
        if channel not in ALERT_CHANNELS:
            channel = "in_app"
        alert = Alert(
            tenant_id=ts.tenant_id,
            title=title,
            body=body,
            severity=severity,
            channel=channel,
            account_id=account_id,
            signal_id=signal_id,
            source=source,
            meta=meta or {},
        )
        ts.add(alert)
        await ts.flush()
        await self._deliver(alert)
        if alert.delivered_at is not None:
            await ts.flush()
        return alert

    async def _deliver(self, alert: Alert) -> None:
        try:
            if alert.channel == "webhook":
                await self._deliver_webhook(alert)
            elif alert.channel == "email":
                self._deliver_email(alert)
            # in_app needs no transport; reading it back is the delivery.
            alert.delivered_at = utcnow()
        except Exception:  # delivery must never lose the alert
            logger.warning("alert %s delivery via %s failed", alert.id, alert.channel, exc_info=True)

    async def _deliver_webhook(self, alert: Alert) -> None:
        url = get_settings().alert_webhook_url
        if not url:
            logger.info("alert %s: no webhook URL configured, skipping POST", alert.id)
            return
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json={
                    "id": alert.id,
                    "title": alert.title,
                    "body": alert.body,
                    "severity": alert.severity,
                    "account_id": alert.account_id,
                    "source": alert.source,
                },
            )
            resp.raise_for_status()

    def _deliver_email(self, alert: Alert) -> None:
        logger.info("[email-stub] alert %s -> %s: %s", alert.id, alert.severity, alert.title)

    async def list(
        self,
        ts: TenantSession,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Alert]:
        where = []
        if status:
            where.append(Alert.status == status)
        stmt = (
            ts.select(Alert, *where)
            .order_by(Alert.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await ts.session.scalars(stmt)).all())

    async def acknowledge(
        self, ts: TenantSession, alert_id: str, *, user_id: str | None = None
    ) -> Alert | None:
        alert = await ts.get(Alert, alert_id)
        if alert is None:
            return None
        alert.status = "acked"
        alert.acked_at = utcnow()
        alert.acked_by = user_id
        await ts.flush()
        return alert


_service: AlertService | None = None


def get_alert_service() -> AlertService:
    global _service
    if _service is None:
        _service = AlertService()
    return _service


def set_alert_service(service: AlertService) -> None:
    global _service
    _service = service
