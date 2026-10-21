# nexus/mcp/tools/engagement.py
"""Campaigns and the reply desk, up to the line the app keeps for itself (owner decisions
2026-10-19): a campaign can be created as a DRAFT with its people and opening emails written, and a
reply can be drafted and saved to the user's own Drafts. Approving, launching and sending stay in the
app, where a person reads every email first."""
from __future__ import annotations

from nexus.mcp.brand import PRODUCT_NAME
from nexus.mcp.gateway import Gateway, ToolOutcome
from nexus.mcp.tools.common import ID, IDS, fail, link, obj, ok, refused
from nexus.mcp.tools.registry import REQUEST_ID, ToolSpec

#: The app's own starting steps (StepsEditor.blankStep): an opening email, then a follow-up two
#: business days later, both sent at the best time in the contact's timezone.
_DEFAULT_STEPS = [
    {"channel": "email", "angle": "", "timing_mode": "auto", "delay_business_days": 0,
     "send_time_local": None, "allowed_weekdays": [0, 1, 2, 3, 4]},
    {"channel": "email", "angle": "", "timing_mode": "auto", "delay_business_days": 2,
     "send_time_local": None, "allowed_weekdays": [0, 1, 2, 3, 4]},
]


async def _draft_reply(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/engagement/desk/{args['reply_id']}/draft",
                        idempotency_key=args.get("request_id"))
    if not out.ok:
        return fail(out)
    data = dict(out.data or {})
    data["note"] = ("Not sent and not saved. save_reply_draft puts it in the user's Drafts; "
                    f"sending happens on {PRODUCT_NAME}'s reply desk.")
    return ok(data)


async def _save_reply_draft(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/engagement/desk/{args['reply_id']}/save-draft",
                        json={"subject": args.get("subject") or "", "body": args["body"]})
    if not out.ok:
        return fail(out)
    return ok({**(out.data or {}), "note": "Saved to the user's Drafts folder, in the thread. "
                                           "Not sent."})


async def _mailbox_id(gw: Gateway) -> str | ToolOutcome:
    out = await gw.get("/api/engagement/mailboxes")
    if not out.ok:
        return fail(out)
    mine = [m for m in out.data or [] if m.get("mine") and m.get("status") == "connected"]
    if not mine:
        return refused(f"Connect a Gmail or Outlook mailbox in {PRODUCT_NAME} > Settings > My "
                       "mailboxes first: a campaign sends from the user's own mailbox.", 409)
    return mine[0]["id"]


async def _create_campaign_draft(gw: Gateway, args: dict) -> ToolOutcome:
    who = {"list_id": args.get("list_id"), "contact_ids": args.get("contact_ids") or [],
           "account_ids": args.get("account_ids") or []}
    if not (who["list_id"] or who["contact_ids"] or who["account_ids"]):
        return refused("Say who it goes to: a list_id, contact_ids or account_ids.")
    mailbox = await _mailbox_id(gw)
    if isinstance(mailbox, ToolOutcome):
        return mailbox
    body = {"name": args["name"], "mailbox_id": mailbox}
    if args.get("template_id"):
        body["template_id"] = args["template_id"]   # the server copies the template's steps
    else:
        body["steps"] = _DEFAULT_STEPS
    made = await gw.post("/api/engagement/campaigns", json=body)
    if not made.ok:
        return fail(made)
    cid = made.data["id"]
    people = {k: v for k, v in who.items() if v}
    added = await gw.post(f"/api/engagement/campaigns/{cid}/contacts",
                          json={"contact_ids": [], "account_ids": [], **people})
    if not added.ok:
        return ok({"campaign_id": cid, "url": link(f"/engagement/campaigns/{cid}"),
                   "note": f"The campaign was created, but nobody was added: {added.message}"})
    result = {"campaign_id": cid, "name": args["name"], "added": len(added.data.get("added", [])),
              "skipped": added.data.get("skipped", []), "drafted": 0,
              "url": link(f"/engagement/campaigns/{cid}")}
    if args["draft_first_emails"] and result["added"]:
        drafted = await gw.post(f"/api/engagement/campaigns/{cid}/draft", params={"limit": 25},
                                idempotency_key=args.get("request_id"))
        if drafted.ok:
            result["drafted"] = (drafted.data or {}).get("drafted", 0)
        else:
            result["draft_note"] = drafted.message
    result["note"] = (f"A draft: nothing was sent. The user reads and approves each opening email "
                      f"and launches the campaign in {PRODUCT_NAME}.")
    return ok(result)


RESEARCH: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="draft_reply", title="Draft a reply to a buyer", scope="research",
        description="Suggest an answer to a reply waiting on the reply desk (from list_queue "
                    "kind 'replies'), written from the conversation and the workspace's style. "
                    "Nothing is sent or saved.",
        input_schema=obj({"reply_id": ID, "request_id": REQUEST_ID}, ("reply_id",)),
        handler=_draft_reply, price_capability="ai.reply_draft", price_unit="per draft",
        calls=(("POST", "/api/engagement/desk/{id}/draft"),),
    ),
    ToolSpec(
        name="create_campaign_draft", title="Create a campaign draft", scope="research",
        description="Create an outreach campaign as a DRAFT sending from the user's own mailbox: "
                    "who it goes to (a saved list, contacts, or everyone with an email at "
                    "accounts), its steps (a sequence template, or an opening email and one "
                    "follow-up), and, unless told not to, its opening emails written for review. "
                    "Never approves, launches or sends.",
        input_schema=obj({
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "list_id": {**ID, "description": "A saved list: its people, or everyone at its "
                                             "companies."},
            "contact_ids": IDS, "account_ids": IDS,
            "template_id": {**ID, "description": "A sequence template's id for the steps."},
            "draft_first_emails": {"type": "boolean", "default": True},
            "request_id": REQUEST_ID,
        }, ("name",)),
        handler=_create_campaign_draft, price_capability="ai.email_draft",
        price_unit="for each opening email written",
        calls=(("GET", "/api/engagement/mailboxes"), ("POST", "/api/engagement/campaigns"),
               ("POST", "/api/engagement/campaigns/{id}/contacts"),
               ("POST", "/api/engagement/campaigns/{id}/draft")),
    ),
)

WRITE: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="save_reply_draft", title="Save a reply to Drafts", scope="write",
        description="Put an answer to a reply-desk reply in the user's own mailbox Drafts, in the "
                    "same thread, for them to review and send. Never sends.",
        input_schema=obj({
            "reply_id": ID,
            "subject": {"type": "string", "maxLength": 300},
            "body": {"type": "string", "minLength": 1, "maxLength": 20000},
        }, ("reply_id", "body")),
        handler=_save_reply_draft, calls=(("POST", "/api/engagement/desk/{id}/save-draft"),),
    ),
)
