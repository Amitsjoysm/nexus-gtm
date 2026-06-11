"""Pydantic wire contracts for the Segment Campaign Engine API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from nexus.models.campaign import Campaign, CampaignTarget


class CampaignIn(BaseModel):
    name: str = Field(..., max_length=200)
    list_id: str
    icp: dict = Field(default_factory=dict)
    sequence: str = Field(default="ai-orchestrated-outbound", max_length=120)
    send_risky: bool = False
    cadence_id: str | None = None
    review_each_touch: bool = False


class CampaignTargetOut(BaseModel):
    id: str
    account_id: str
    status: str
    skip_reason: str | None = None
    draft: dict = Field(default_factory=dict)
    error: str | None = None

    @classmethod
    def from_model(cls, t: CampaignTarget) -> "CampaignTargetOut":
        return cls(
            id=t.id,
            account_id=t.account_id,
            status=t.status,
            skip_reason=t.skip_reason,
            draft=t.draft or {},
            error=t.error,
        )


class CampaignOut(BaseModel):
    id: str
    name: str
    list_id: str
    status: str
    sequence: str
    icp: dict = Field(default_factory=dict)
    report: dict = Field(default_factory=dict)
    send_risky: bool = False
    cadence_id: str | None = None
    review_each_touch: bool = False
    created_at: datetime

    @classmethod
    def from_model(cls, c: Campaign) -> "CampaignOut":
        return cls(
            id=c.id,
            name=c.name,
            list_id=c.list_id,
            status=c.status,
            sequence=c.sequence,
            icp=c.icp or {},
            report=c.report or {},
            send_risky=c.send_risky,
            cadence_id=c.cadence_id,
            review_each_touch=c.review_each_touch,
            created_at=c.created_at,
        )


class CampaignDetailOut(CampaignOut):
    targets: list[CampaignTargetOut] = Field(default_factory=list)
    # Reply attribution: per-stage outcome counts (replied/meeting/won/...) recorded against
    # this campaign — the ROI rollup that proves what the sends actually produced.
    outcomes: dict = Field(default_factory=dict)

    @classmethod
    def from_models(
        cls,
        c: Campaign,
        targets: list[CampaignTarget],
        outcomes: dict | None = None,
    ) -> "CampaignDetailOut":
        base = CampaignOut.from_model(c)
        return cls(
            **base.model_dump(),
            targets=[CampaignTargetOut.from_model(t) for t in targets],
            outcomes=outcomes or {},
        )


class CampaignPreviewOut(BaseModel):
    """The approval sample: a few drafted targets + the skip report."""

    campaign_id: str
    status: str
    report: dict = Field(default_factory=dict)
    sample: list[CampaignTargetOut] = Field(default_factory=list)
