"""App passwords of SMTP mailboxes: sealed at rest, sent only where they were saved (2026-11-20).

A mailbox's app password used to sit in ``Tenant.email_settings`` as plain JSON, readable by anyone
with the database, a backup, or a log line that printed the settings. It is now Fernet-sealed under
``password_sealed`` (``core/crypto``, key derived from ``secret_key`` like the CRM tokens in
``ingestion/crm_crypto.py``), and ``password`` is never written again.

* **Old rows keep working.** ``mailbox_password`` reads ``password_sealed`` first, then a plaintext
  ``password``, so nothing breaks between the deploy and the first save. The workspace router seals
  on every write (`_save_accounts`), the API lifespan seals every workspace once at boot
  (`seal_all_workspaces`), and ``scripts/seal_mailbox_passwords.py`` does the same on demand.
* **An unreadable seal reads as "no password"**, as an unreadable CRM token does: the mailbox shows
  "missing its password" and its owner types it again. Rotating ``secret_key`` therefore asks every
  SMTP mailbox for its password once, which is the price of not keeping the plaintext.
* **The platform's own mailboxes are not here.** ``platform_mail`` builds its settings from Provider
  keys (sealed there) and passes a plaintext ``password`` in memory, which ``mailbox_password``
  still reads.
"""
from __future__ import annotations

from nexus.core.crypto import seal_text, unseal_text

SEALED = "password_sealed"
_BACKGROUND: set = set()


def _key() -> str:
    """The sealing key. Empty derives one from ``secret_key`` (see module docstring)."""
    return ""


def mailbox_password(account: dict | None) -> str:
    """The password to sign in with, sealed or (stored before sealing) plain."""
    a = account or {}
    sealed = a.get(SEALED)
    if sealed:
        return unseal_text(str(sealed), key=_key())
    return str(a.get("password") or "")


def has_mailbox_password(account: dict | None) -> bool:
    a = account or {}
    return bool(a.get(SEALED) or a.get("password"))


def set_password(account: dict, password: str) -> None:
    """Store ``password`` sealed, in place. Empty clears it."""
    account.pop("password", None)
    if password:
        account[SEALED] = seal_text(password, key=_key())
    else:
        account.pop(SEALED, None)


def clear_password(account: dict) -> None:
    account.pop("password", None)
    account.pop(SEALED, None)


def seal_account(account: dict) -> bool:
    """Move a plaintext password into the sealed field, in place. True when something changed."""
    plain = account.get("password")
    if not plain:
        if "password" in account:
            account.pop("password")
            return True
        return False
    set_password(account, str(plain))
    return True


def sealed_settings(settings: dict | None) -> tuple[dict, int]:
    """A copy of a workspace's ``email_settings`` with every password sealed, and how many were
    sealed. Covers the mailbox list and the legacy single mailbox at the top level."""
    out = dict(settings or {})
    changed = 0
    if out.get("password"):
        changed += int(seal_account(out))
    elif "password" in out:
        out.pop("password")
    accounts = out.get("accounts")
    if isinstance(accounts, list):
        fresh = []
        for raw in accounts:
            account = dict(raw) if isinstance(raw, dict) else raw
            if isinstance(account, dict) and account.get("password"):
                changed += int(seal_account(account))
            elif isinstance(account, dict):
                account.pop("password", None)
            fresh.append(account)
        out["accounts"] = fresh
    return out, changed


def seal_workspace_passwords(tenant) -> int:
    """Seal every plaintext password on one workspace, in place. Returns how many. Idempotent."""
    settings, changed = sealed_settings(getattr(tenant, "email_settings", None))
    if changed:
        tenant.email_settings = settings
    return changed


async def seal_all_workspaces(*, dry_run: bool = False) -> dict:
    """Seal every workspace's plaintext mailbox passwords. Idempotent; never raises: a failure
    leaves the passwords readable as they were, which is the behaviour before sealing existed.

    Runs on the platform session (one sweep across tenants). Each workspace that needs it is
    re-read under a row lock in its own short transaction and only its passwords change, so an
    admin saving email settings at the same moment loses nothing: ``email_settings`` is one JSON
    value, and writing back a copy read earlier would undo whatever was saved in between."""
    import logging

    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.identity import Tenant

    logger = logging.getLogger("nexus.integrations.mailbox_secrets")
    report = {"workspaces": 0, "passwords": 0}
    try:
        maker = get_platform_sessionmaker()
        async with maker() as session:
            rows = (await session.execute(select(Tenant.id, Tenant.email_settings))).all()
        candidates = [tid for tid, settings in rows if sealed_settings(settings)[1]]
        for tenant_id in candidates:
            async with maker() as session:
                tenant = (await session.scalars(
                    select(Tenant).where(Tenant.id == tenant_id).with_for_update())).first()
                if tenant is None:
                    continue
                settings, changed = sealed_settings(tenant.email_settings)
                if not changed:
                    continue
                report["workspaces"] += 1
                report["passwords"] += changed
                if dry_run:
                    await session.rollback()
                    continue
                tenant.email_settings = settings
                await session.commit()
    except Exception:  # noqa: BLE001 - a boot step must never stop the process
        logger.warning("sealing mailbox passwords failed; they stay readable as before",
                       exc_info=True)
    if report["passwords"] and not dry_run:
        logger.info("sealed %d mailbox password(s) in %d workspace(s)",
                    report["passwords"], report["workspaces"])
    return report


def schedule_sealing(logger) -> None:
    """Seal in the background at boot, so a large estate never delays the app's startup probe."""
    import asyncio

    async def _run():
        await seal_all_workspaces()

    try:
        task = asyncio.get_running_loop().create_task(_run())
        _BACKGROUND.add(task)  # the loop holds tasks weakly; keep it until it finishes
        task.add_done_callback(_BACKGROUND.discard)
    except RuntimeError:
        logger.warning("could not schedule mailbox password sealing (no running loop)")
