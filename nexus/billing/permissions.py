# nexus/billing/permissions.py
"""Platform-admin permissions.

Before this, ``require_platform_admin`` checked only that an *active* ``platform_admins`` row
existed and never read ``platform_role``. A "support" admin could therefore reprice every plan
and mint unlimited credits — the role column was decoration.

Two design choices worth stating:

* **Presets expand to permission sets, they are not checked directly.** Storing the expanded set
  on the row means changing what "support" means tomorrow does not silently re-grant power to
  people provisioned today. What an admin can do is visible in their own row.
* **The bootstrap allowlist keeps full power.** ``NEXUS_PLATFORM_ADMIN_EMAILS`` exists to solve
  "nobody can reach the console yet"; narrowing it would reintroduce exactly the lockout it was
  added to prevent.
"""
from __future__ import annotations

# Individual capabilities. Names are stable strings — they are stored in the database and appear
# in audit rows, so renaming one is a migration, not a refactor.
BILLING_READ = "billing.read"            # the read console: capabilities, plans, rates, subs
PRICING_WRITE = "pricing.write"          # reprice plans, edit entitlements, set rate cards
SUBSCRIPTIONS_WRITE = "subscriptions.write"   # move a tenant between plans, custom plans
CREDITS_GRANT = "credits.grant"          # unlimited credit grants
CREDITS_GRANT_CAPPED = "credits.grant.capped"  # goodwill grants up to a configured ceiling
INVOICES_COLLECT = "invoices.collect"    # charge a finalized invoice
JOBS_MANAGE = "jobs.manage"              # dead-letter triage and replay
ADMINS_MANAGE = "admins.manage"          # grant/revoke platform admins
USERS_MANAGE = "users.manage"            # reset a user's MFA, account recovery
# Deliberately NOT part of users.manage. Resetting someone's MFA and *becoming* them are different
# powers: the first is account recovery a support agent does daily, the second is reading a
# customer's data as them. Bundling would grant the second to everyone who needs the first.
USERS_IMPERSONATE = "users.impersonate"  # time-boxed, read-only, audited session as a user

ALL_PERMISSIONS = (
    BILLING_READ, PRICING_WRITE, SUBSCRIPTIONS_WRITE, CREDITS_GRANT,
    CREDITS_GRANT_CAPPED, INVOICES_COLLECT, JOBS_MANAGE, ADMINS_MANAGE, USERS_MANAGE,
    USERS_IMPERSONATE,
)

# Role presets. `superadmin` is everything; the other two are deliberately narrower.
#
# `support` gets CREDITS_GRANT_CAPPED rather than CREDITS_GRANT: goodwill credits are the single
# most common support action, and forcing an escalation for every one of them means the
# escalation path becomes a rubber stamp. A ceiling keeps the blast radius small while leaving
# the workflow usable.
ROLE_PRESETS: dict[str, tuple[str, ...]] = {
    "superadmin": ALL_PERMISSIONS,
    "finance": (
        BILLING_READ, PRICING_WRITE, SUBSCRIPTIONS_WRITE, CREDITS_GRANT, INVOICES_COLLECT,
    ),
    "support": (BILLING_READ, CREDITS_GRANT_CAPPED, USERS_MANAGE),
}


def permissions_for_role(role: str) -> list[str]:
    """Expand a preset. An unknown role gets read-only rather than nothing.

    Failing closed to *no* access would lock out an admin whose role string was mistyped, with no
    way to fix it through the product. Read-only is the safe middle: they can see the console and
    ask someone with `admins.manage` to correct the row.
    """
    return list(ROLE_PRESETS.get(role, (BILLING_READ,)))


def effective_permissions(row) -> set[str]:
    """The permissions actually granted by a ``platform_admins`` row.

    An explicit ``permissions`` list wins; an empty one falls back to the role preset. That
    fallback is what makes migration 0029 safe: every pre-existing row has no explicit list, so
    it keeps behaving exactly as its role implies without a data backfill.
    """
    explicit = list(getattr(row, "permissions", None) or [])
    if explicit:
        return set(explicit)
    return set(permissions_for_role(getattr(row, "platform_role", "") or ""))
