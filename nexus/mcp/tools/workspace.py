# nexus/mcp/tools/workspace.py
"""Where the user stands: the dashboard's numbers with today's priorities and the ideal customer
profile, the work queues (alerts, replies waiting, calls to make), the relationship network, and
acknowledging an alert. Everything here is free."""
from __future__ import annotations

from nexus.mcp.gateway import Gateway, ToolOutcome
from nexus.mcp.tools.common import ID, fail, link, obj, ok
from nexus.mcp.tools.registry import ToolSpec


async def _overview(gw: Gateway, args: dict) -> ToolOutcome:
    numbers = await gw.get("/api/analytics/overview")
    if not numbers.ok:
        return fail(numbers)
    # Each part answers on its own: the engagement engine or the profile being unavailable must
    # not hide the dashboard's numbers.
    today = await gw.get("/api/engagement/today")
    profile = await gw.get("/api/relevance/profile")
    icp = (profile.data or {}).get("icp") if profile.ok else None
    return ok({"overview": numbers.data, "today": today.data if today.ok else [],
               "ideal_customer_profile": icp, "url": link("/dashboard")})


_QUEUES = {
    "alerts": ("/api/alerts", lambda a: {"limit": a["limit"]}),
    "replies": ("/api/engagement/desk", lambda a: {"tab": "needs_action"}),
    "calls": ("/api/calling/queue", lambda a: {"status_": "open", "mine": str(a["mine"]).lower(),
                                               "limit": a["limit"]}),
}


async def _list_queue(gw: Gateway, args: dict) -> ToolOutcome:
    path, params = _QUEUES[args["kind"]]
    out = await gw.get(path, params(args))
    if not out.ok:
        return fail(out)
    items = out.data or []
    return ok({"kind": args["kind"], "items": items[: args["limit"]],
               "hint": {"alerts": "acknowledge_alert clears one.",
                        "replies": "fetch 'reply:<id>' reads one; draft_reply suggests an answer.",
                        "calls": "fetch 'call:<id>' gives the pre-call brief."}[args["kind"]]})


async def _search_network(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post("/api/network/search", json={"query": args["query"],
                                                      "limit": args["limit"]})
    if not out.ok:
        return fail(out)
    return ok({"query": args["query"], "people": out.data,
               "note": "fetch 'person:<id>' shows who on the team can introduce them."})


async def _acknowledge_alert(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/alerts/{args['alert_id']}/ack")
    return ok({"alert": out.data}) if out.ok else fail(out)


READ: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_workspace_overview", title="Get the workspace overview", scope="read",
        description="Start here for 'what should I work on?': the dashboard's numbers (for a rep, "
                    "their own work), today's prioritized engagement actions, and the workspace's "
                    "ideal customer profile.",
        input_schema=obj({}), handler=_overview,
        calls=(("GET", "/api/analytics/overview"), ("GET", "/api/engagement/today"),
               ("GET", "/api/relevance/profile")),
    ),
    ToolSpec(
        name="list_queue", title="List alerts, replies or calls", scope="read",
        description="A work queue: 'alerts' (signals worth acting on), 'replies' (buyer replies "
                    "waiting for a decision on the reply desk) or 'calls' (the call list).",
        input_schema=obj({
            "kind": {"type": "string", "enum": ["alerts", "replies", "calls"]},
            "mine": {"type": "boolean", "default": True,
                     "description": "Calls only: just the user's own."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        }, ("kind",)),
        handler=_list_queue,
        calls=(("GET", "/api/alerts"), ("GET", "/api/engagement/desk"),
               ("GET", "/api/calling/queue")),
    ),
    ToolSpec(
        name="search_network", title="Search the team's network", scope="read",
        description="Who the team knows: people in teammates' connected contacts and calendars "
                    "matching a description (e.g. 'CFOs at fintechs in London'), with the strength "
                    "of the best connection. Only connections shared with the user are searched.",
        input_schema=obj({
            "query": {"type": "string", "minLength": 1, "maxLength": 300},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        }, ("query",)),
        handler=_search_network, calls=(("POST", "/api/network/search"),),
    ),
)

WRITE: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="acknowledge_alert", title="Acknowledge an alert", scope="write",
        description="Mark an alert from list_queue as seen, so it leaves the open queue.",
        input_schema=obj({"alert_id": ID}, ("alert_id",)),
        handler=_acknowledge_alert, idempotent=True, calls=(("POST", "/api/alerts/{id}/ack"),),
    ),
)
