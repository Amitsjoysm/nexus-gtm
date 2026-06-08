"""CampaignService: the business logic for the Segment Campaign Engine.

Two phases, both reusing the orchestration engine:
- ``run_draft_phase``: per target, create+execute a ``research_compose`` run (no send),
  snapshot the draft, and classify un-actionable targets into the skip report.
- ``run_send_phase`` (Task 7): per approved target, replay the draft through
  ``SendMessageTool`` so the outbound hard gates fire exactly as they do for a single run.

Targets are processed independently with try/except isolation: one account's failure never
blocks the rest. The service mutates rows in the passed-in ``TenantSession`` and flushes;
the caller (API or worker) owns the commit.
"""
from __future__ import annotations

from nexus.agents.runtime import get_agent_runtime
from nexus.core.tenancy import TenantSession
from nexus.models.campaign import (
    Campaign,
    CampaignTarget,
    CAMP_APPROVED,
    CAMP_AWAITING_APPROVAL,
    CAMP_COMPLETED,
    CAMP_DRAFTING,
    CAMP_SENDING,
    TARGET_APPROVED,
    TARGET_DRAFTED,
    TARGET_DRAFTING,
    TARGET_FAILED,
    TARGET_PENDING,
    TARGET_SENT,
    TARGET_SKIPPED,
    SKIP_NO_CONTACT,
    SKIP_RESEARCH_FAILED,
    SKIP_UNDELIVERABLE,
    SKIP_UNGROUNDED,
)
from nexus.models.orchestration import OrchestrationRun, RUN_COMPLETED
from nexus.models.workflow import ListItem
from nexus.orchestration.engine import get_orchestration_engine
from nexus.orchestration.tools import SendMessageTool, ToolContext, ToolError
from nexus.verification import STATUS_INVALID


class CampaignError(Exception):
    """Raised for an invalid campaign state transition (e.g. approving too early)."""


class CampaignService:
    async def create(
        self,
        ts: TenantSession,
        *,
        name: str,
        list_id: str,
        icp: dict,
        sequence: str,
        created_by_user_id: str | None,
    ) -> Campaign:
        """Create the campaign and one PENDING target per account in the List."""
        campaign = Campaign(
            tenant_id=ts.tenant_id,
            name=name,
            list_id=list_id,
            icp=icp or {},
            sequence=sequence or "ai-orchestrated-outbound",
            created_by_user_id=created_by_user_id,
        )
        ts.add(campaign)
        await ts.flush()

        items = await ts.list(ListItem, ListItem.list_id == list_id)
        # Dedupe accounts (a List could in principle carry an account twice).
        seen: set[str] = set()
        for item in items:
            if item.account_id in seen:
                continue
            seen.add(item.account_id)
            ts.add(
                CampaignTarget(
                    tenant_id=ts.tenant_id,
                    campaign_id=campaign.id,
                    account_id=item.account_id,
                    status=TARGET_PENDING,
                )
            )
        await ts.flush()
        return campaign

    async def list_targets(self, ts: TenantSession, campaign_id: str) -> list[CampaignTarget]:
        return await ts.list(CampaignTarget, CampaignTarget.campaign_id == campaign_id)

    async def run_draft_phase(self, ts: TenantSession, campaign: Campaign) -> Campaign:
        """Draft a grounded email for every PENDING target, then park for approval."""
        campaign.status = CAMP_DRAFTING
        await ts.flush()

        targets = await self.list_targets(ts, campaign.id)
        engine = get_orchestration_engine()
        runtime = get_agent_runtime()
        for target in targets:
            if target.status != TARGET_PENDING:
                continue
            await self._draft_one(ts, campaign, target, engine=engine, runtime=runtime)

        campaign.report = await self._build_report(ts, campaign)
        campaign.status = CAMP_AWAITING_APPROVAL
        await ts.flush()
        return campaign

    async def approve_and_send(
        self, ts: TenantSession, campaign: Campaign, *, decided_by: str | None
    ) -> Campaign:
        """Campaign-level approval: one human decision, then send every DRAFTED target."""
        if campaign.status != CAMP_AWAITING_APPROVAL:
            raise CampaignError(
                f"campaign must be awaiting_approval to approve, is '{campaign.status}'"
            )
        campaign.status = CAMP_APPROVED
        await ts.flush()
        return await self.run_send_phase(ts, campaign)

    async def run_send_phase(self, ts: TenantSession, campaign: Campaign) -> Campaign:
        campaign.status = CAMP_SENDING
        await ts.flush()

        targets = await self.list_targets(ts, campaign.id)
        tool = SendMessageTool()
        runtime = get_agent_runtime()
        for target in targets:
            if target.status != TARGET_DRAFTED:
                continue
            await self._send_one(ts, campaign, target, tool=tool, runtime=runtime)

        campaign.report = await self._build_report(ts, campaign)
        campaign.status = CAMP_COMPLETED
        await ts.flush()
        return campaign

    async def _send_one(self, ts, campaign, target, *, tool, runtime) -> None:
        target.status = TARGET_APPROVED
        await ts.flush()
        # Reuse the target's research_compose run as the ToolContext carrier; its blackboard
        # already holds the draft. Reassert the draft snapshot in case it was edited/rebuilt.
        run = await ts.get(OrchestrationRun, target.run_id) if target.run_id else None
        if run is None:
            target.status = TARGET_FAILED
            target.error = "missing draft run for send"
            await ts.flush()
            return
        run.blackboard = dict(run.blackboard or {})
        run.blackboard["draft"] = dict(target.draft or {})
        tc = ToolContext(
            ts=ts, runtime=runtime, run=run, inputs={"sequence": campaign.sequence}
        )
        try:
            await tool.run(tc)
            target.status = TARGET_SENT
            await ts.flush()
        except ToolError as exc:
            # A gate refused at the boundary. Classify into the report's skip reasons.
            msg = str(exc).lower()
            target.status = TARGET_SKIPPED
            if "ungrounded" in msg:
                target.skip_reason = SKIP_UNGROUNDED
            elif "undeliverable" in msg or "invalid" in msg:
                target.skip_reason = SKIP_UNDELIVERABLE
            else:
                target.skip_reason = SKIP_NO_CONTACT
            target.error = str(exc)
            await ts.flush()
        except Exception as exc:  # isolation: one bad send never blocks the rest
            target.status = TARGET_FAILED
            target.error = f"{type(exc).__name__}: {exc}"
            await ts.flush()

    async def _draft_one(self, ts, campaign, target, *, engine, runtime) -> None:
        target.status = TARGET_DRAFTING
        await ts.flush()
        try:
            run = await engine.create_run(
                ts,
                "research_compose",
                {"account_id": target.account_id},
                account_id=target.account_id,
            )
            await engine.execute_run(ts, run, runtime=runtime)
            target.run_id = run.id

            if run.status != RUN_COMPLETED:
                target.status = TARGET_SKIPPED
                target.skip_reason = SKIP_RESEARCH_FAILED
                target.error = run.error
                await ts.flush()
                return

            draft = dict((run.blackboard or {}).get("draft") or {})
            target.draft = draft
            reason = self._classify(draft)
            if reason is None:
                target.status = TARGET_DRAFTED
            else:
                target.status = TARGET_SKIPPED
                target.skip_reason = reason
            await ts.flush()
        except Exception as exc:  # isolation: one bad target never blocks the rest
            target.status = TARGET_FAILED
            target.error = f"{type(exc).__name__}: {exc}"
            await ts.flush()

    @staticmethod
    def _classify(draft: dict) -> str | None:
        """Return the primary skip reason for a draft, or None if it is sendable.

        Priority (most fundamental blocker first): ungrounded → no contact → undeliverable.
        ``ComposeMessageTool`` writes ``grounded`` (bool), ``contact_id`` (str|None), and
        ``email_status`` (str|None — None means no email was present to verify)."""
        if not draft.get("grounded"):
            return SKIP_UNGROUNDED
        if not draft.get("contact_id") or draft.get("email_status") is None:
            return SKIP_NO_CONTACT
        if draft.get("email_status") == STATUS_INVALID:
            return SKIP_UNDELIVERABLE
        return None

    async def _build_report(self, ts: TenantSession, campaign: Campaign) -> dict:
        targets = await self.list_targets(ts, campaign.id)
        skips: dict[str, int] = {}
        drafted = sent = skipped = failed = 0
        for t in targets:
            if t.status == TARGET_DRAFTED:
                drafted += 1
            elif t.status == TARGET_SENT:
                sent += 1
            elif t.status == TARGET_SKIPPED:
                skipped += 1
                if t.skip_reason:
                    skips[t.skip_reason] = skips.get(t.skip_reason, 0) + 1
            elif t.status == TARGET_FAILED:
                failed += 1
        return {
            "total": len(targets),
            "drafted": drafted,
            "sent": sent,
            "skipped": skipped,
            "failed": failed,
            "skips": skips,
        }


_service = CampaignService()


def get_campaign_service() -> CampaignService:
    return _service
