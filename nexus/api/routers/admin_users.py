# nexus/api/routers/admin_users.py
"""Platform-admin user administration.

Currently one operation: clearing a user's MFA. It is the account-recovery path — a customer who
loses their authenticator and their recovery codes is otherwise permanently locked out, and
"contact support" has to mean support can actually do something.

That makes it a privileged, abusable action: clearing MFA removes a factor from someone else's
account. So it is gated on platform admin (no tenant role reaches it), it is audited with
before/after, and it never returns anything that would help an attacker enumerate users.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.audit import record_admin_action
from nexus.billing.permissions import USERS_IMPERSONATE, USERS_MANAGE
from nexus.core.db import get_sessionmaker

logger = logging.getLogger("nexus.api.admin_users")

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


class SuspendIn(BaseModel):
    """Why. Optional, but it lands in the audit log — a suspension nobody can explain three months
    later is the one that gets reversed by mistake."""

    model_config = {"extra": "forbid"}

    reason: str = ""


@router.post("/{email}/suspend")
async def suspend_user(
    email: str,
    body: SuspendIn | None = None,
    principal: Principal = Depends(require_platform_permission(USERS_MANAGE)),
) -> dict:
    """Stop this person logging in anywhere, without destroying what they did.

    The alternative before this existed was deletion, which takes the audit trail with it and
    orphans every account they owned. Suspension is reversible and leaves the history intact.

    Suspends the **user**, not one membership: a compromised account must stop working in every
    workspace at once. Removing someone from a single workspace is what deleting a membership does.
    """
    from sqlalchemy import select

    from nexus.core.db import utcnow
    from nexus.models.identity import User

    target = (email or "").strip().lower()
    reason = (body.reason if body else "") or ""
    async with get_sessionmaker()() as session:
        user = (await session.scalars(select(User).where(User.email == target))).first()
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
        if user.suspended_at is not None:
            # Idempotent: a double-clicked button is a success, not a confusing error.
            return {"email": target, "suspended": True,
                    "suspended_at": user.suspended_at.isoformat()}

        before = {"suspended": False}
        user.suspended_at = utcnow()
        user.suspended_reason = reason[:300]
        user.suspended_by = principal.user_id
        await session.flush()
        await record_admin_action(
            session, actor=principal.user_id, action="user.suspend", target=target,
            before=before, after={"suspended": True}, note=reason,
        )
        await session.commit()
        return {"email": target, "suspended": True,
                "suspended_at": user.suspended_at.isoformat()}


@router.post("/{email}/reactivate")
async def reactivate_user(
    email: str,
    body: SuspendIn | None = None,
    principal: Principal = Depends(require_platform_permission(USERS_MANAGE)),
) -> dict:
    """Undo a suspension. Clears the reason too — a stale one reads as still-suspended."""
    from sqlalchemy import select

    from nexus.models.identity import User

    target = (email or "").strip().lower()
    async with get_sessionmaker()() as session:
        user = (await session.scalars(select(User).where(User.email == target))).first()
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
        if user.suspended_at is None:
            return {"email": target, "suspended": False}

        was = {"suspended": True, "reason": user.suspended_reason}
        user.suspended_at = None
        user.suspended_reason = None
        user.suspended_by = None
        await session.flush()
        await record_admin_action(
            session, actor=principal.user_id, action="user.reactivate", target=target,
            before=was, after={"suspended": False},
            note=(body.reason if body else "") or "",
        )
        await session.commit()
        return {"email": target, "suspended": False}


@router.delete("/{email}/mfa")
async def reset_user_mfa(
    email: str,
    principal: Principal = Depends(require_platform_permission(USERS_MANAGE)),
) -> dict:
    """Clear every MFA factor and recovery code for one user.

    Deletes rather than deactivates: a stale sealed secret is a liability with no use, and the
    user must re-enrol from scratch anyway. The audit row is what preserves the history.
    """
    from sqlalchemy import select

    from nexus.models.identity import User
    from nexus.models.mfa import MFARecoveryCode, UserMFA

    target = (email or "").strip().lower()
    async with get_sessionmaker()() as session:
        user = (await session.scalars(select(User).where(User.email == target))).first()
        if user is None:
            # 404 on an unknown email is an enumeration oracle, but this endpoint is already
            # restricted to platform admins, who can list users anyway. Clarity wins here.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")

        factors = list(
            (await session.scalars(select(UserMFA).where(UserMFA.user_id == user.id))).all()
        )
        codes = list(
            (
                await session.scalars(
                    select(MFARecoveryCode).where(MFARecoveryCode.user_id == user.id)
                )
            ).all()
        )
        if not factors and not codes:
            return {"email": target, "cleared": False, "reason": "no MFA enrolled"}

        before = {
            "methods": sorted(f.method for f in factors),
            "confirmed": sorted(f.method for f in factors if f.confirmed_at is not None),
            "recovery_codes": len(codes),
        }
        for row in (*factors, *codes):
            await session.delete(row)
        await session.flush()

        await record_admin_action(
            session,
            actor=principal.user_id,
            action="user.mfa_reset",
            target=target,
            before=before,
            after={"methods": [], "recovery_codes": 0},
            note="account recovery",
        )
        await session.commit()

    logger.warning("MFA reset for %s by %s", target, principal.user_id)
    return {"email": target, "cleared": True, "removed": before}


class ImpersonateIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Why. Required, not optional: an impersonation with no stated reason is indistinguishable
    # from curiosity, and the audit row is worthless without it.
    reason: str = Field(min_length=8, max_length=500)
    ttl_min: int = Field(default=30, ge=1, le=120)


@router.post("/{email}/impersonate")
async def impersonate_user(
    email: str,
    body: ImpersonateIn,
    principal: Principal = Depends(require_platform_permission(USERS_IMPERSONATE)),
) -> dict:
    """Mint a time-boxed, **read-only** session as a tenant user.

    Support cannot diagnose "the inbox looks wrong for me" from the outside, and asking a customer
    for their password is the alternative this replaces.

    Four constraints make it defensible, and all four are enforced here rather than in the UI:

    * **Read-only.** The token carries ``ro``; every RBAC-gated mutation refuses it. An admin
      changing a customer's data unnoticed is the worst thing this could enable.
    * **Time-boxed.** Minutes, capped at two hours. A standing key to every account is not a
      support tool.
    * **Attributable.** The token names the impersonator, so every request traces to a person.
    * **Audited with a reason.** Recorded before the token is returned, so the trail exists even
      if the session is never used.

    It is gated on `users.impersonate`, which is deliberately NOT part of `users.manage`: resetting
    someone's MFA and becoming them are different powers.
    """
    from sqlalchemy import select

    from nexus.core.security import create_impersonation_token
    from nexus.models.identity import Membership, User

    target = email.strip().lower()
    async with get_sessionmaker()() as session:
        user = (
            await session.scalars(select(User).where(User.email == target))
        ).first()
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no user {target}")
        membership = (
            await session.scalars(
                select(Membership).where(Membership.user_id == user.id)
            )
        ).first()
        if membership is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"{target} belongs to no workspace"
            )

        # Audited BEFORE the token is minted: if the write fails, no session is handed out. The
        # reverse order would allow an unlogged impersonation whenever the audit table is down.
        await record_admin_action(
            session, actor=principal.user_id, action="user.impersonate",
            target=target, subject_tenant_id=membership.tenant_id,
            after={"ttl_min": body.ttl_min, "role": membership.role, "read_only": True},
            note=body.reason,
        )
        await session.commit()

        token = create_impersonation_token(
            user_id=user.id, tenant_id=membership.tenant_id, role=membership.role,
            impersonator_id=principal.user_id, ttl_min=body.ttl_min,
        )
        logger.warning(
            "impersonation: %s acting as %s (tenant %s) for %s min — %s",
            principal.user_id, target, membership.tenant_id, body.ttl_min, body.reason,
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in_min": body.ttl_min,
            "read_only": True,
            "impersonating": target,
            "tenant_id": membership.tenant_id,
        }


@router.get("/{email}/activity")
async def user_activity(
    email: str,
    limit: int = 50,
    principal: Principal = Depends(require_platform_permission(USERS_MANAGE)),
) -> dict:
    """What this person has been doing, for the support question "what did they actually do?"

    **Attribution here is genuinely partial, and the response says so rather than implying
    completeness.** Only `billing_usage_events` carries a `user_id`; signals, agent runs, inbox
    tasks, calls and alerts are tenant-scoped with no actor column, and on the live data even the
    usage events are only sometimes attributed (measured: 1 of 7 rows had a `user_id`). A console
    that silently merged tenant-wide activity into "this user's activity" would let a support
    agent tell a customer that a named person did something a colleague did, which is worse than
    admitting the gap.

    So the payload is three clearly separated lists:

    * ``metered_actions`` — actions attributed to THIS user. The only true user-level trail.
    * ``admin_actions`` — what platform staff did TO this account. Answers "why can't I log in?",
      which is the other half of most tickets, and it is the trail that must never be missing.
    * ``workspace_activity`` — recent tenant-wide events, labelled as such. Context, not attribution.

    Read-only and `users.manage`-gated: it exposes one person's behaviour, so it sits behind the
    same permission as suspending them rather than behind plain `billing.read`.
    """
    from sqlalchemy import desc, or_, select

    from nexus.models.billing import BillingAuditLog, BillingUsageEvent
    from nexus.models.identity import Membership, Tenant, User

    target = (email or "").strip().lower()
    limit = max(1, min(int(limit or 50), 200))

    # Platform session throughout: this reads ACROSS tenants by design, and the app's RLS-bound
    # role would silently return zero rows rather than an error.
    from nexus.core.db import get_platform_sessionmaker

    async with get_platform_sessionmaker()() as session:
        user = (await session.scalars(select(User).where(User.email == target))).first()
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")

        rows = (
            await session.execute(
                select(Membership.role, Tenant.id, Tenant.name, Tenant.slug)
                .join(Tenant, Tenant.id == Membership.tenant_id)
                .where(Membership.user_id == user.id)
            )
        ).all()
        memberships = [
            {"tenant_id": tid, "tenant_name": name, "slug": slug, "role": role}
            for role, tid, name, slug in rows
        ]
        tenant_ids = [m["tenant_id"] for m in memberships]

        metered = (
            await session.scalars(
                select(BillingUsageEvent)
                .where(BillingUsageEvent.user_id == user.id)
                .order_by(desc(BillingUsageEvent.occurred_at))
                .limit(limit)
            )
        ).all()

        # Actions taken against this account. `target` is the email for user-scoped actions.
        admin_rows = (
            await session.scalars(
                select(BillingAuditLog)
                .where(or_(BillingAuditLog.target == target,
                           BillingAuditLog.target == f"user:{target}"))
                .order_by(desc(BillingAuditLog.created_at))
                .limit(limit)
            )
        ).all()

        workspace = []
        if tenant_ids:
            workspace = list(
                (
                    await session.scalars(
                        select(BillingUsageEvent)
                        .where(BillingUsageEvent.tenant_id.in_(tenant_ids))
                        .order_by(desc(BillingUsageEvent.occurred_at))
                        .limit(limit)
                    )
                ).all()
            )

    def _iso(value) -> str | None:
        return value.isoformat() if value else None

    return {
        "email": target,
        "suspended": user.suspended_at is not None,
        "suspended_at": _iso(user.suspended_at),
        "suspended_reason": user.suspended_reason or "",
        "memberships": memberships,
        "metered_actions": [
            {
                "capability_id": e.capability_id, "quantity": float(e.quantity or 0),
                "unit": e.unit, "source": e.source, "occurred_at": _iso(e.occurred_at),
                "tenant_id": e.tenant_id, "attrs": e.attrs or {},
            }
            for e in metered
        ],
        "admin_actions": [
            {
                "action": a.action, "actor": a.actor, "note": a.note or "",
                "at": _iso(a.created_at),
            }
            for a in admin_rows
        ],
        "workspace_activity": [
            {
                "capability_id": e.capability_id, "user_id": e.user_id or "",
                "attributed": bool(e.user_id), "source": e.source,
                "occurred_at": _iso(e.occurred_at), "tenant_id": e.tenant_id,
            }
            for e in workspace
        ],
        # Stated in the payload, not just the docstring: whoever renders this has to be able to
        # tell the operator that "no activity" may mean "not recorded against a user".
        "attribution_note": (
            "Only metered actions carry a user id. Signals, agent runs, inbox tasks and calls are "
            "recorded per workspace with no actor, so workspace_activity is context, not proof "
            "that this person did it."
        ),
    }
