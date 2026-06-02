"""Sales Engagement Platform push (Outreach / Salesloft) behind one interface.

Stubs record the push intent so plays are testable end-to-end; real adapters POST to the SEP API.
Pushes should be idempotent and routed through the outbox in production (see ARCHITECTURE.md §10).
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("nexus.integrations.sep")


@dataclass(slots=True)
class SEPPushResult:
    ok: bool
    platform: str
    detail: dict = field(default_factory=dict)


class SEPConnector(abc.ABC):
    platform: str

    @abc.abstractmethod
    async def push_contact(
        self, *, sequence: str, email: str | None, payload: dict
    ) -> SEPPushResult: ...


class _RecordingConnector(SEPConnector):
    """Base stub that logs/records the push rather than calling a remote API."""

    def __init__(self) -> None:
        self.pushed: list[dict] = []

    async def push_contact(self, *, sequence, email, payload) -> SEPPushResult:
        record = {"sequence": sequence, "email": email, "payload": payload}
        self.pushed.append(record)
        logger.info("[%s] push to sequence %s for %s", self.platform, sequence, email)
        return SEPPushResult(ok=True, platform=self.platform, detail=record)


class OutreachConnector(_RecordingConnector):
    platform = "outreach"


class SalesloftConnector(_RecordingConnector):
    platform = "salesloft"


_connector: SEPConnector | None = None


def get_sep_connector() -> SEPConnector:
    global _connector
    if _connector is None:
        _connector = OutreachConnector()
    return _connector


def set_sep_connector(connector: SEPConnector) -> None:
    global _connector
    _connector = connector
