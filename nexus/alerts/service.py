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
        await self._deliver(ts, alert)
        if alert.delivered_at is not None:
            await ts.flush()
        return alert

    async def _deliver(self, ts: TenantSession, alert: Alert) -> None:
        """Deliver on the channel named on the alert, using THIS TENANT's own credentials.

        `get_alert_channels()` is a process-wide singleton built from the deployment env and has no
        idea whose alert it is holding. One configured Slack URL would therefore have posted every
        tenant's account names and buying signals into that one workspace — the cross-tenant leak
        `nexus/ingestion/crm_credentials.py` exists to prevent, in the same shape.
        """
        from nexus.alerts.connections import resolve_alert_channels

        try:
            registry = await resolve_alert_channels(ts)
            result = await registry.deliver(alert)
            if result.ok:
                alert.delivered_at = utcnow()
            else:
                logger.info("alert %s not delivered via %s: %s", alert.id, alert.channel, result.detail)
        except Exception as exc:  # a channel failure must never lose the persisted alert
            # The message, REDACTED — not `exc_info=True`. A Slack or Teams webhook URL is the
            # credential, and Telegram puts its bot token in the path by design, so httpx's
            # `raise_for_status` writes all three straight into a traceback. The stack frames on an
            # outbound HTTP delivery are the same every time; the message is the diagnostic part.
            from nexus.core.redact import redact

            logger.warning(
                "alert %s delivery via %s failed: %s",
                alert.id, alert.channel, redact(f"{type(exc).__name__}: {exc}"),
            )

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
