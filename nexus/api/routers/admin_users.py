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

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.audit import record_admin_action
from nexus.billing.permissions import USERS_MANAGE
from nexus.core.db import get_sessionmaker

logger = logging.getLogger("nexus.api.admin_users")

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


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
