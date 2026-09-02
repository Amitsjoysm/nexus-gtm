# nexus/api/routers/admin_features.py
"""Take a feature offline, platform-wide, without a deploy.

Gated on ``FEATURES_MANAGE``, superadmin preset only. Same argument as ``providers.manage`` and
``sources.manage``: the blast radius is every customer at once, and unlike a price change it is
immediately visible to all of them.

The console lists **every** `module.*` capability, not just the ones somebody has already touched.
An operator opening this during an incident needs the whole board — a list showing only existing
switches answers "what have we changed?" when the question is "what can I change?".
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.audit import record_admin_action
from nexus.billing.permissions import FEATURES_MANAGE
from nexus.core.db import get_platform_sessionmaker
from nexus.features.switches import invalidate
from nexus.models.billing import BillingCapability
from nexus.models.feature_switch import SWITCH_STATES, FeatureSwitch

router = APIRouter(prefix="/admin/features", tags=["admin-features"])


class FeatureOut(BaseModel):
    capability_id: str
    name: str
    state: str
    message: str
    updated_by: str = ""
    # How many other capabilities this one gates. Switching off `module.agents` also stops the
    # orchestration endpoints and `ai.chat_turn`, and an operator should see that BEFORE flipping
    # it rather than discovering the reach from the support queue.
    gates: list[str] = Field(default_factory=list)


class FeatureListOut(BaseModel):
    features: list[FeatureOut]
    states: list[str]


class FeatureIn(BaseModel):
    # `extra="forbid"` for the same reason every admin write body here has it: a field we do not
    # read, silently accepted, is a setting the operator believes they changed.
    model_config = {"extra": "forbid"}

    state: str
    # Shown to the customer verbatim, in place of the generic upsell. Optional — the UI has
    # per-state default wording — but a specific sentence is what turns "unavailable" into
    # something a rep can repeat to a prospect.
    message: str = ""
    note: str = ""


def _validate_state(state: str) -> str:
    """Reject at the edge. The resolver treats an unrecognised state as `enabled` (fail open), so a
    typo accepted here would read as "switched off" in this console and do nothing to the product —
    the worst of both, and invisible until a customer contradicts the panel."""
    if state not in SWITCH_STATES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"state must be one of {', '.join(SWITCH_STATES)}",
        )
    return state


@router.get("", response_model=FeatureListOut)
async def list_features(
    _: Principal = Depends(require_platform_permission(FEATURES_MANAGE)),
) -> FeatureListOut:
    """Every switchable feature and its current state."""
    async with get_platform_sessionmaker()() as session:
        caps = (await session.scalars(select(BillingCapability))).all()
        switches = {
            s.capability_id: s
            for s in (await session.scalars(select(FeatureSwitch))).all()
        }

    # What each module gates, derived from `depends_on` rather than maintained beside it. A
    # hand-kept list here would be a second source of truth about reach, and the first thing to
    # drift would be exactly the number an operator is relying on mid-incident.
    gated_by: dict[str, list[str]] = {}
    for cap in caps:
        for dep in cap.depends_on or ():
            gated_by.setdefault(dep, []).append(cap.id)

    modules = sorted((c for c in caps if c.category == "module"), key=lambda c: c.id)
    features = []
    for cap in modules:
        sw = switches.get(cap.id)
        features.append(FeatureOut(
            capability_id=cap.id,
            name=cap.name or cap.id,
            # An unrecognised stored state reports as `enabled`, matching what the resolver
            # actually does with it. Showing the raw value would tell the operator the feature is
            # off while every customer keeps using it.
            state=sw.state if sw is not None and sw.state in SWITCH_STATES else "enabled",
            message=(sw.message if sw is not None else "") or "",
            updated_by=(sw.updated_by if sw is not None else "") or "",
            gates=sorted(gated_by.get(cap.id, [])),
        ))
    return FeatureListOut(features=features, states=list(SWITCH_STATES))


@router.put("/{capability_id}", response_model=FeatureOut)
async def set_feature(
    capability_id: str,
    body: FeatureIn,
    principal: Principal = Depends(require_platform_permission(FEATURES_MANAGE)),
) -> FeatureOut:
    """Set one feature's state. Idempotent — the same call twice leaves the same row."""
    state = _validate_state(body.state)

    async with get_platform_sessionmaker()() as session:
        cap = await session.get(BillingCapability, capability_id)
        if cap is None:
            # A switch on a capability that does not exist gates nothing, and reads in this console
            # as a feature that is off. Same argument as `depends_on` validation in capability
            # authoring: a reference to something unresolvable is worse than a refusal.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown capability: {capability_id}",
            )

        row = await session.get(FeatureSwitch, capability_id)
        before = {"state": row.state, "message": row.message} if row else {"state": "enabled"}
        if row is None:
            row = FeatureSwitch(capability_id=capability_id)
            session.add(row)
        row.state = state
        # Cleared on re-enable rather than kept: a stale "back at 14:00 UTC" sitting on a feature
        # that works again is worse than no message, because somebody will believe it.
        row.message = body.message if state != "enabled" else ""
        row.updated_by = principal.user_id or ""
        await session.flush()

        await record_admin_action(
            session, actor=principal.user_id, action="feature.switch",
            target=capability_id, before=before,
            after={"state": row.state, "message": row.message}, note=body.note,
        )
        await session.commit()
        out = FeatureOut(
            capability_id=capability_id, name=cap.name or capability_id,
            state=row.state, message=row.message, updated_by=row.updated_by,
        )

    # Drop THIS process's cache so an operator who flips a switch and reloads sees the result
    # immediately. The 30s TTL exists for the processes that cannot be told — the worker, and the
    # second uvicorn worker — not to make the console that wrote the row feel broken.
    invalidate()
    return out
