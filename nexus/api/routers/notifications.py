# nexus/api/routers/notifications.py
"""Per-user alert delivery preferences: which categories, on which channels, and when not to.

`notification_preferences` has had a table, a model, a unique constraint and a reader in the digest
sweep since migration 0032 — and no endpoint. Built, stored, and unreachable: a customer could not
choose where their alerts go, which is the one thing the table exists to record.

Rep-level, deliberately. These are a person's OWN delivery settings, not a workspace policy, so
every member sets their own and the permission required is the one every member already has.

**The absence of a row means "no preference expressed"**, and the tenant-level configuration
continues to apply — the model's own contract. So this endpoint returns the catalogue of what CAN
be chosen alongside whatever the user has actually chosen, rather than inventing a default row per
category and turning "I have not decided" into "I decided the default".
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.notification_preference import NotificationPreference

router = APIRouter(prefix="/notifications", tags=["notifications"])

#: Delivery modes the digest sweep understands. `off` is a real choice and not the absence of one:
#: a row saying "never send me hiring alerts" must outrank the tenant default, which is exactly
#: what "no row" would fall back to.
MODES = ("immediate", "digest", "off")


def _channels() -> list[str]:
    """Channels this deployment can actually deliver on.

    Read from the live registry rather than a hard-coded list, so a channel that is compiled in but
    unconfigured is still offered (it reports "no url configured" rather than vanishing), and a
    channel added later appears without touching this file.
    """
    from nexus.alerts.channels import get_alert_channels

    reg = get_alert_channels()
    return sorted(getattr(reg, "_channels", {}).keys())


class PreferenceOut(BaseModel):
    category: str
    channel: str
    mode: str
    quiet_from_min: int | None = None
    quiet_to_min: int | None = None
    utc_offset_min: int = 0
    quiet_hours_allow_critical: bool = True


class PreferencesOut(BaseModel):
    #: What the user has actually chosen. An empty list means every category still follows the
    #: workspace default — the model's "absence means no preference" contract, surfaced honestly
    #: rather than hidden behind a screenful of invented defaults.
    preferences: list[PreferenceOut] = Field(default_factory=list)
    #: The vocabulary, so the client never hard-codes it and cannot drift from the server.
    categories: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    modes: list[str] = Field(default_factory=list)


class PreferenceIn(BaseModel):
    model_config = {"extra": "forbid"}

    category: str
    channel: str = "in_app"
    mode: str = "immediate"
    # Minutes from local midnight. Stored this way so the overnight wrap (22:00 -> 07:00) is
    # arithmetic rather than a special case — a naive `start <= now < end` disables quiet hours for
    # exactly the people who set them overnight.
    quiet_from_min: int | None = Field(default=None, ge=0, le=1439)
    quiet_to_min: int | None = Field(default=None, ge=0, le=1439)
    utc_offset_min: int = Field(default=0, ge=-840, le=840)
    quiet_hours_allow_critical: bool = True


@router.get("", response_model=PreferencesOut)
async def get_preferences(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> PreferencesOut:
    """This user's own delivery preferences, plus the vocabulary to choose from."""
    from nexus.alerts.rules import ALERT_CATEGORIES

    rows = await ts.list(
        NotificationPreference, NotificationPreference.user_id == principal.user_id, limit=200
    )
    return PreferencesOut(
        preferences=[
            PreferenceOut(
                category=r.category, channel=r.channel, mode=r.mode,
                quiet_from_min=r.quiet_from_min, quiet_to_min=r.quiet_to_min,
                utc_offset_min=r.utc_offset_min,
                quiet_hours_allow_critical=r.quiet_hours_allow_critical,
            )
            for r in rows
        ],
        categories=sorted(ALERT_CATEGORIES),
        channels=_channels(),
        modes=list(MODES),
    )


@router.put("", response_model=PreferenceOut)
async def set_preference(
    body: PreferenceIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> PreferenceOut:
    """Set one category/channel preference. Idempotent — one row per user, category and channel.

    Upserts rather than inserting, because the table's unique constraint exists precisely to stop a
    UI that saves twice producing two contradictory preferences and making delivery a coin flip.
    """
    from nexus.alerts.rules import ALERT_CATEGORIES

    if body.category not in ALERT_CATEGORIES:
        # Validated against the live category list, which is DERIVED from the alert rules — so a
        # category nothing can ever emit cannot be subscribed to. A preference for an event that
        # never fires is silence the user believes is a setting.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown category: {body.category}",
        )
    if body.channel not in _channels():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown channel: {body.channel}",
        )
    if body.mode not in MODES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"mode must be one of {', '.join(MODES)}",
        )
    # Half a quiet-hours window is not a window. Accepting one would silently disable the feature
    # for someone who believes they configured it.
    if (body.quiet_from_min is None) != (body.quiet_to_min is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "quiet hours need both a start and an end, or neither",
        )

    row = await ts.first(
        NotificationPreference,
        NotificationPreference.user_id == principal.user_id,
        NotificationPreference.category == body.category,
        NotificationPreference.channel == body.channel,
    )
    if row is None:
        row = NotificationPreference(
            tenant_id=ts.tenant_id, user_id=principal.user_id,
            category=body.category, channel=body.channel,
        )
        ts.add(row)
    row.mode = body.mode
    row.quiet_from_min = body.quiet_from_min
    row.quiet_to_min = body.quiet_to_min
    row.utc_offset_min = body.utc_offset_min
    row.quiet_hours_allow_critical = body.quiet_hours_allow_critical
    await ts.flush()

    return PreferenceOut(
        category=row.category, channel=row.channel, mode=row.mode,
        quiet_from_min=row.quiet_from_min, quiet_to_min=row.quiet_to_min,
        utc_offset_min=row.utc_offset_min,
        quiet_hours_allow_critical=row.quiet_hours_allow_critical,
    )


@router.delete("/{category}/{channel}", status_code=status.HTTP_204_NO_CONTENT,
               response_class=Response)
async def clear_preference(
    category: str,
    channel: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> Response:
    """Remove a preference, returning this category to the workspace default.

    Deleting the row is NOT the same as setting `mode="off"`, and both are offered on purpose: off
    means "never send me this", no row means "whatever the workspace decides". Collapsing them
    would take away the only way back to the default.
    """
    row = await ts.first(
        NotificationPreference,
        NotificationPreference.user_id == principal.user_id,
        NotificationPreference.category == category,
        NotificationPreference.channel == channel,
    )
    if row is not None:
        await ts.session.delete(row)
        await ts.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
