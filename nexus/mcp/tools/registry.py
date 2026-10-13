# nexus/mcp/tools/registry.py
"""What a tool is: its name, the words a model reads, its argument schema, the permission it needs,
the hints Claude and ChatGPT show the user, and the handler that does the work.

Annotations are required by Anthropic's directory (a ``title`` and the applicable ``readOnlyHint``
or ``destructiveHint`` on every tool; missing ones are its most common rejection) and read by
ChatGPT to decide when to ask the user first. They follow from the scope, so they cannot disagree
with what the tool may do.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from nexus.mcp.gateway import Gateway, ToolOutcome

logger = logging.getLogger("nexus.mcp.tools")

ID_PATTERN = "^[A-Za-z0-9_-]{1,64}$"
REQUEST_ID = {
    "type": "string", "pattern": "^[A-Za-z0-9_-]{8,64}$",
    "description": "Optional. Reuse the same value when retrying this exact call, so it is "
                   "answered from the first attempt instead of running (and charging) twice.",
}

Handler = Callable[[Gateway, dict], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    scope: str                      # read | research | write
    input_schema: dict
    handler: Handler
    #: Research tools: the rate card that prices one unit, and what a unit is.
    price_capability: str | None = None
    price_unit: str = ""
    idempotent: bool = False
    #: Where the handler sends requests, for the structural test (method, path template).
    calls: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def annotations(self) -> dict:
        if self.scope == "read":
            return {"title": self.title, "readOnlyHint": True, "openWorldHint": False}
        return {
            "title": self.title, "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": self.idempotent, "openWorldHint": self.scope == "research",
        }


def describe(spec: ToolSpec, prices: dict[str, float]) -> dict:
    """The ``tools/list`` entry. A research tool's price is read from the rate cards when the list
    is answered, never written into the description, so a repricing in Admin reaches every model."""
    description = spec.description
    if spec.price_capability:
        price = prices.get(spec.price_capability)
        if price is not None and price > 0:
            description += (f" Costs {price:g} credits {spec.price_unit}. The balance is checked "
                            "before anything is spent.")
        else:
            description += " Costs credits, as in the app; the balance is checked first."
    return {
        "name": spec.name,
        "title": spec.title,
        "description": description,
        "inputSchema": spec.input_schema,
        "annotations": spec.annotations(),
    }


async def load_prices(capabilities: set[str]) -> dict[str, float]:
    """Active rate card credits for these capabilities. Never raises: without prices the list still
    answers, with a sentence that does not name a number."""
    if not capabilities:
        return {}
    try:
        from sqlalchemy import select

        from nexus.core.db import get_platform_sessionmaker
        from nexus.models.billing import BillingRateCard

        async with get_platform_sessionmaker()() as s:
            rows = (await s.scalars(select(BillingRateCard).where(
                BillingRateCard.capability_id.in_(sorted(capabilities)),
                BillingRateCard.active.is_(True)))).all()
        return {r.capability_id: float(r.credits_per_unit or 0) for r in rows}
    except Exception:
        logger.warning("could not read rate cards for tool prices", exc_info=True)
        return {}
