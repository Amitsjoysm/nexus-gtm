"""Tool registry: the typed capabilities a plan can invoke.

A Tool wraps a unit of work (usually an existing agent) behind a stable name + input
schema so the planner can select it and the engine can validate and execute it. Tools
read and write a shared *blackboard* on the run, which is how subagents stay aware of
each other's output (research facts feed the message composer, the composed draft feeds
the sender, and so on).

Every tool is offline-safe: the wrapped agents run against the deterministic stub LLM in
tests/CI, so the whole orchestration is exercisable without network or API keys.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass

from nexus.agents.runtime import AgentRuntime
from nexus.core.tenancy import TenantSession
from nexus.integrations.email_sender import (
    account_is_configured,
    resolve_account,
    save_to_drafts,
    send_email,
)
from nexus.integrations.sep import get_sep_connector
from nexus.models.account import Contact
from nexus.models.identity import Tenant
from nexus.models.orchestration import OrchestrationRun
from nexus.verification import STATUS_INVALID


class ToolError(Exception):
    """Raised by a tool to fail its step with a human-readable reason."""


@dataclass(slots=True)
class ToolContext:
    ts: TenantSession
    runtime: AgentRuntime
    run: OrchestrationRun
    inputs: dict

    @property
    def blackboard(self) -> dict:
        return self.run.blackboard

    @property
    def account_id(self) -> str | None:
        return self.run.account_id or self.run.blackboard.get("account_id")


class Tool(abc.ABC):
    name: str
    description: str = ""
    # Outbound/side-effecting tools default to requiring approval. The planner may
    # still override per-step, but this is the safe default.
    requires_approval: bool = False

    @abc.abstractmethod
    async def run(self, tc: ToolContext) -> dict: ...


class _AgentTool(Tool):
    """Wraps a registered agent, surfacing agent-level errors as step failures.

    Agents return ``{"error": ...}`` in their output for in-band problems (e.g. "research
    requires an account") while still reporting ``status == "completed"``. For a workflow
    step that ambiguity is a failure, so we raise — this is also the out-of-context guard:
    a step that couldn't ground itself does not silently feed garbage downstream.
    """

    agent_name: str

    async def run(self, tc: ToolContext) -> dict:
        result = await tc.runtime.run(
            self.agent_name, tc.ts, account_id=tc.account_id, **tc.inputs
        )
        if result.status != "completed":
            raise ToolError(result.error or f"{self.agent_name} failed")
        if isinstance(result.output, dict) and result.output.get("error"):
            raise ToolError(str(result.output["error"]))
        return result.output


class ResearchTool(_AgentTool):
    name = "research"
    description = "Gather account/person facts and produce a grounded research brief."
    agent_name = "research"

    async def run(self, tc: ToolContext) -> dict:
        out = await super().run(tc)
        # Publish facts to the blackboard so the composer can ground its message.
        tc.blackboard["research"] = {
            "brief": out.get("brief", ""),
            "facts": out.get("facts", []),
            "sources": out.get("sources", []),
        }
        return out


class ScoringTool(_AgentTool):
    name = "scoring"
    description = "Compute the account's composite fit/intent/health score."
    agent_name = "scoring"

    async def run(self, tc: ToolContext) -> dict:
        out = await super().run(tc)
        tc.blackboard["composite"] = out.get("composite")
        return out


class ComposeMessageTool(_AgentTool):
    name = "compose_message"
    description = "Draft a personalized outreach message grounded in research + value props."
    agent_name = "messaging"

    async def run(self, tc: ToolContext) -> dict:
        out = await super().run(tc)
        # Stage the draft for the approval gate. We record whether it is grounded in
        # retrieved facts; an ungrounded draft is allowed to exist but is flagged so the
        # reviewer (and the grounded-send gate) can refuse it.
        research = tc.blackboard.get("research", {}) or {}
        facts = research.get("facts", []) or []
        grounded = bool(facts)

        # Verify the recipient *now*, so the approval UI can show deliverability before a
        # human decides — not only at send time. The verdict travels on the draft.
        email_status: str | None = None
        email_confidence: float | None = None
        provider_type: str | None = None
        email_signals: dict = {}
        contact_id = out.get("contact_id")
        if contact_id:
            contact = await tc.ts.get(Contact, contact_id)
            if contact is not None and contact.email:
                verdict = await tc.runtime.registry.verify_email(contact.email)
                email_status = verdict.status
                email_confidence = verdict.confidence
                provider_type = verdict.provider_type
                email_signals = dict(verdict.signals)
                # Persist the verdict so the inbox can show deliverability without
                # re-verifying (and so a verdict survives beyond this run).
                contact.email_status = verdict.status

        tc.blackboard["draft"] = {
            "contact_id": contact_id,
            "subject": out.get("subject", ""),
            "body": out.get("body", ""),
            "message": out.get("message", ""),
            "grounded": grounded,
            # Surfaced to the reviewer at the approval gate (the gate copies the draft).
            "grounding": {
                "facts": list(facts)[:6],
                "sources": research.get("sources", []),
            },
            "email_status": email_status,
            "email_confidence": email_confidence,
            "provider_type": provider_type,
            "email_signals": email_signals,
        }
        return out


class SendMessageTool(Tool):
    """Outbound: enroll the drafted message into a sequence. Requires approval.

    Reads the approved draft from the blackboard (a reviewer may have edited it at the
    gate) and pushes through the SEP connector. Fails loudly if there is no draft — a
    send must never invent its own content.
    """

    name = "send_message"
    description = "Push the approved outreach draft into the sequence platform."
    requires_approval = True

    async def run(self, tc: ToolContext) -> dict:
        draft = tc.blackboard.get("draft")
        if not draft or not (draft.get("subject") or draft.get("body")):
            raise ToolError("no approved draft to send")

        email = None
        email_status = None
        contact = None
        contact_id = draft.get("contact_id")
        if contact_id:
            contact = await tc.ts.get(Contact, contact_id)
            email = contact.email if contact else None

        # Draft mode: write to the reviewer's mailbox Drafts for manual review/send instead of
        # sending. No SEP push and no hard send gates — nothing is delivered to the prospect here,
        # so an ungrounded/unverified draft is allowed (the human finalizes it in their mailbox).
        if (draft.get("delivery_mode") or "send") == "draft":
            tenant = await tc.ts.session.get(Tenant, tc.ts.tenant_id)
            settings = (getattr(tenant, "email_settings", None) or {}) if tenant else {}
            account = resolve_account(settings, draft.get("from_account"))
            if account is None:
                raise ToolError("no mailbox configured to save a draft")
            res = await save_to_drafts(
                account, to=email or "",
                subject=draft.get("subject", ""), body=draft.get("body", ""),
            )
            if not res.ok:
                raise ToolError(f"draft save failed: {res.detail}")
            return {"ok": True, "drafted": True, "delivery_mode": "draft",
                    "email": email, "from_account": account["id"]}

        # Grounded-send gate (Tier-1 credibility): never push an outbound message that wasn't
        # grounded in retrieved facts. A reviewer approving at the gate cannot override this —
        # an ungrounded cold email is exactly the kind of generic spam this product exists to
        # avoid sending.
        if not draft.get("grounded"):
            raise ToolError("refusing to send an ungrounded draft (no research facts)")

        # Verified-send gate: a hard-invalid address never gets a send. We re-verify here
        # (cheap: the registry caches by email) so the decision is enforced at the boundary,
        # not just shown in the UI. Unknown/valid pass — a real everifier returns invalid for
        # the addresses we must never touch; the offline stub only rejects malformed syntax.
        if email:
            verdict = await tc.runtime.registry.verify_email(email)
            email_status = verdict.status
            if contact is not None:
                contact.email_status = verdict.status
            if verdict.status == STATUS_INVALID:
                raise ToolError(f"refusing to send to an undeliverable address ({email})")

        sequence = tc.inputs.get("sequence") or "ai-orchestrated-outbound"
        res = await get_sep_connector().push_contact(
            sequence=sequence,
            email=email,
            payload={
                "subject": draft.get("subject", ""),
                "body": draft.get("body", ""),
                "run_id": tc.run.id,
            },
        )

        # Real delivery: if the workspace configured its own SMTP (Gmail/Outlook), actually send
        # the email from its mailbox. When SMTP isn't configured we keep the recording-only
        # behavior (the SEP push above), so workspaces without a mailbox — and the offline suite —
        # are unchanged. A delivery failure raises so the touch is NOT marked sent (it retries),
        # rather than silently recording a send that never left.
        delivered: bool | None = None
        from_account: str | None = None
        if email:
            tenant = await tc.ts.session.get(Tenant, tc.ts.tenant_id)
            settings = (getattr(tenant, "email_settings", None) or {}) if tenant else {}
            # Route to the mailbox the reviewer chose at the gate (draft["from_account"]),
            # falling back to the workspace default account. None when SMTP is unconfigured.
            account = resolve_account(settings, draft.get("from_account"))
            if account and account_is_configured(account):
                from_account = account["id"]
                send_res = await send_email(
                    account, to=email,
                    subject=draft.get("subject", ""), body=draft.get("body", ""),
                )
                delivered = send_res.ok
                if not send_res.ok:
                    raise ToolError(f"smtp send failed: {send_res.detail}")

        return {"ok": res.ok, "platform": res.platform, "sequence": sequence,
                "email": email, "email_status": email_status, "delivered": delivered,
                "from_account": from_account}


class DiscoveryTool(_AgentTool):
    name = "discovery"
    description = "Surface companies/contacts matching the conversation's ICP (read-only)."
    agent_name = "discovery"
    requires_approval = False

    async def run(self, tc: ToolContext) -> dict:
        gi = tc.run.goal_input or {}
        result = await tc.runtime.run(
            self.agent_name,
            tc.ts,
            account_id=None,
            target=gi.get("target", "companies"),
            icp=gi.get("icp") or {},
            max_candidates=gi.get("max_candidates"),
        )
        if result.status != "completed":
            raise ToolError(result.error or "discovery failed")
        if isinstance(result.output, dict) and result.output.get("error"):
            raise ToolError(str(result.output["error"]))
        tc.blackboard["discovery"] = result.output
        return result.output


_DEFAULT_CADENCE_STEPS = [
    {"channel": "email", "delay_days": 0, "angle": "intro + signal hook"},
    {"channel": "call", "delay_days": 2, "angle": "reference the email; quick value pitch"},
    {"channel": "email", "delay_days": 4, "angle": "bump with a case study / social proof"},
]


class SetupCadenceTool(Tool):
    """Let the orchestrator define a multi-touch cadence (email + call steps) on its own.

    Reads ``cadence_name`` / ``steps`` / ``description`` from the run's goal_input; when steps
    aren't specified it picks a sensible cold-calling 3-touch (email -> call -> email). Creating a
    cadence definition is not outbound, so no approval gate — actual sends/calls still flow through
    the campaign launch + approval gate."""

    name = "setup_cadence"
    description = "Create a multi-touch cadence (email and/or call steps) from a brief."
    requires_approval = False

    async def run(self, tc: ToolContext) -> dict:
        from nexus.cadences.service import CadenceError, get_cadence_service

        gi = {**(tc.run.goal_input or {}), **(tc.inputs or {})}
        steps = gi.get("steps") or _DEFAULT_CADENCE_STEPS
        name = gi.get("cadence_name") or gi.get("name") or "AI cadence"
        try:
            cadence = await get_cadence_service().create_cadence(
                tc.ts,
                name=name,
                description=gi.get("description"),
                steps=steps,
                created_by_user_id=getattr(tc.run, "created_by_user_id", None),
            )
        except CadenceError as exc:
            raise ToolError(str(exc))
        out = {
            "cadence_id": cadence.id,
            "name": cadence.name,
            "steps": [
                {"channel": s.get("channel", "email"),
                 "delay_days": int(s.get("delay_days", 0)),
                 "angle": s.get("angle", "")}
                for s in steps
            ],
        }
        tc.blackboard["cadence"] = out
        return out


# Registry ---------------------------------------------------------------------------

_REGISTRY: dict[str, Tool] = {}


def register_tool(tool: Tool) -> None:
    _REGISTRY[tool.name] = tool


def get_tool(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def available_tools() -> list[str]:
    return sorted(_REGISTRY)


for _t in (ResearchTool(), ScoringTool(), ComposeMessageTool(), SendMessageTool(),
           DiscoveryTool(), SetupCadenceTool()):
    register_tool(_t)
