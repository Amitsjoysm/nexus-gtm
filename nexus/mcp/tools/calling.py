# nexus/mcp/tools/calling.py
"""The call list, from chat (owner decision 2026-10-19): queue a call with its script, and log how a
call went. Never dials: placing a call happens on the app's Calls page, where the rep is."""
from __future__ import annotations

from nexus.mcp.brand import PRODUCT_NAME
from nexus.mcp.gateway import Gateway, ToolOutcome
from nexus.mcp.tools.common import ID, fail, link, obj, ok
from nexus.mcp.tools.registry import ToolSpec

#: ``nexus.models.calling.ALL_DISPOSITIONS``, spelled out so the schema is the contract a model reads.
DISPOSITIONS = ["connected", "voicemail", "no_answer", "callback", "meeting_booked",
                "not_interested", "bad_number", "gatekeeper"]


async def _queue_call(gw: Gateway, args: dict) -> ToolOutcome:
    body = {"account_id": args["account_id"], "reason": args.get("reason") or "",
            "priority": args["priority"]}
    if args.get("contact_id"):
        body["contact_id"] = args["contact_id"]
    task = await gw.post("/api/calling/tasks", json=body)
    if not task.ok:
        return fail(task)
    task_id = task.data["id"]
    # The script is free, as on the Calls page; a call without one is still queued.
    script = await gw.post(f"/api/calling/tasks/{task_id}/script")
    return ok({"task": task.data, "script": script.data if script.ok else None,
               "url": link("/calls"),
               "note": f"Queued, not dialled. The rep calls from {PRODUCT_NAME}'s Calls page; "
                       f"fetch 'call:{task_id}' gives the pre-call brief."})


async def _log_call_outcome(gw: Gateway, args: dict) -> ToolOutcome:
    body = {"disposition": args["disposition"], "notes": args.get("notes") or ""}
    for key in ("duration_s", "next_step"):
        if args.get(key) is not None:
            body[key] = args[key]
    out = await gw.post(f"/api/calling/tasks/{args['task_id']}/disposition", json=body)
    return ok({"activity": out.data}) if out.ok else fail(out)


WRITE: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="queue_call", title="Queue a call", scope="write",
        description="Put a call on the user's call list for an account (and a contact there), "
                    "with an AI call script. Never dials.",
        input_schema=obj({
            "account_id": ID,
            "contact_id": {**ID, "description": "The person to call, if known."},
            "reason": {"type": "string", "maxLength": 500,
                       "description": "Why call now, e.g. the signal that prompted it."},
            "priority": {"type": "integer", "minimum": 0, "maximum": 100, "default": 50},
        }, ("account_id",)),
        handler=_queue_call,
        calls=(("POST", "/api/calling/tasks"), ("POST", "/api/calling/tasks/{id}/script")),
    ),
    ToolSpec(
        name="log_call_outcome", title="Log a call's outcome", scope="write",
        description="Record how a call from the call list went (connected, voicemail, meeting "
                    "booked, ...), with notes and the next step. No answer, callback and gatekeeper "
                    "keep the call on the list; the others close it.",
        input_schema=obj({
            "task_id": ID,
            "disposition": {"type": "string", "enum": DISPOSITIONS},
            "notes": {"type": "string", "maxLength": 4000},
            "duration_s": {"type": "integer", "minimum": 0, "maximum": 86400},
            "next_step": {"type": "string", "maxLength": 500},
        }, ("task_id", "disposition")),
        handler=_log_call_outcome, calls=(("POST", "/api/calling/tasks/{id}/disposition"),),
    ),
)
