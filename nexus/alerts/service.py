"""Alert service: create tenant-scoped alerts and fan them out across delivery channels.

Channels are pluggable behind the :class:`AlertChannel` interface (see ``channels.py``): ``in_app``
persists, ``webhook``/``slack`` POST, ``email`` is a stub. This service owns persistence and the
durability guarantee — delivery is wrapped so a channel failure never loses the alert: it is always
saved, and only stamped ``delivered_at`` when the channel actually handled it.
"""
from __future__ import annotations

import logging

from nexus.alerts.channels import get_alert_channels
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
            result = await get_alert_channels().deliver(alert)
            if result.ok:
                alert.delivered_at = utcnow()
            else:
                logger.info("alert %s not delivered via %s: %s", alert.id, alert.channel, result.detail)
        except Exception:  # a channel failure must never lose the persisted alert
            logger.warning("alert %s delivery via %s failed", alert.id, alert.channel, exc_info=True)

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
