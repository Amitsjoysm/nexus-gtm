# nexus/mcp/tools/discovery.py
"""Adding new companies that match the ideal customer profile: the Relevance page's Populate, from
chat (owner decision 2026-10-19). Same price and gates as the app: the balance is checked for the
whole count before anything is bought, and one Populate runs at a time per workspace."""
from __future__ import annotations

from nexus.mcp.gateway import Gateway, ToolOutcome
from nexus.mcp.tools.common import fail, link, obj, ok
from nexus.mcp.tools.registry import ToolSpec


async def _add_icp_companies(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post("/api/discovery/populate", json={"count": args["count"]})
    if not out.ok:
        return fail(out)
    run = out.data or {}
    return ok({"run": run, "url": link("/accounts?source=found"),
               "note": f"Running in the background. fetch 'populate:{run.get('id')}' shows "
                       "progress; companies are charged only as they are added."})


RESEARCH: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="add_icp_companies", title="Add companies matching the ICP", scope="research",
        description="Add up to the given number of NEW companies matching the workspace's ideal "
                    "customer profile, as the Relevance page's Populate does. Needs a saved "
                    "profile; runs in the background. Use get_workspace_overview to see the "
                    "profile first, and confirm the count with the user.",
        input_schema=obj({"count": {"type": "integer", "minimum": 1, "maximum": 100}}, ("count",)),
        handler=_add_icp_companies, price_capability="discovery.account_added",
        price_unit="for each company added", calls=(("POST", "/api/discovery/populate"),),
    ),
)
