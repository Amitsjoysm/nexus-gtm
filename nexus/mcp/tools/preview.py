# nexus/mcp/tools/preview.py
"""What a call would do and cost, without doing it, and the credits a call spent (owner decision
2026-11-20: "ask permission and give credit info at each step").

The dry-run convention (Stripe's upcoming invoice, Terraform's ``plan``): one optional argument,
``preview``, on every tool that costs credits or changes something that matters. A preview runs no
handler and reads only a price, so it cannot act; the server (``server.run_tool``) decides which of
the two a call is. Prices come from ``GET /billing/quote``, which prices exactly as the meter does.
"""
from __future__ import annotations

from nexus.mcp.gateway import Gateway, ToolOutcome
from nexus.mcp.tools.common import fail, ok


def ask_line(will: str, credits: float, uses: bool, covered: bool, reason: str | None) -> str:
    """The sentence the assistant reads: what to ask the user, or why this cannot run."""
    if not covered:
        return ("Tell the user this cannot run: "
                f"{reason or 'the plan or the credit balance does not cover it.'}")
    cost = f"up to {credits:g} credits" if uses and credits else "no credits"
    return (f"Ask the user: '{will} This uses {cost}. Go ahead?' If they agree, make the same "
            "call without preview.")


def answer(will: str, *, credits: float = 0.0, uses: bool = False, covered: bool = True,
           reason: str | None = None, balance: float | None = None, **extra) -> ToolOutcome:
    """Every preview's shape, so the tools that build their own (Prospector now; Populate,
    save_icp and finding buyers later) cannot drift from the generic one."""
    return ok({"preview": True, "will": will, "max_credits": credits if uses else 0,
               "uses_credits": uses, "balance": balance, "covered": covered,
               "ask_the_user": ask_line(will, credits, uses, covered, reason), **extra})


async def quote(gw: Gateway, capability: str, units: int, will: str) -> ToolOutcome:
    """A preview priced as ``units`` of one rate card, with the workspace's balance and whether the
    billing engine would let the call through."""
    q = await gw.get("/api/billing/quote", {"capability": capability, "quantity": units})
    if not q.ok:
        return fail(q)
    d = q.data or {}
    return answer(will, credits=float(d.get("credits") or 0), uses=bool(d.get("uses_credits")),
                  covered=bool(d.get("covered", True)), reason=d.get("reason"),
                  balance=d.get("balance"), units=units)


async def generic(gw: Gateway, spec, args: dict) -> ToolOutcome:
    """The preview of a tool priced by its own rate card: ``spec.units`` of it (one when unset).
    No units, or no rate card, is a call that costs nothing."""
    will = spec.will(args) if spec.will else f"{spec.title}."
    units = int(spec.units(args)) if spec.units else 1
    if not spec.price_capability or units <= 0:
        return answer(will)
    return await quote(gw, spec.price_capability, units, will)


async def balance(gw: Gateway) -> float | None:
    """The workspace's credit balance, or None when it cannot be read."""
    out = await gw.get("/api/billing/credits")
    return (out.data or {}).get("balance") if out.ok else None
