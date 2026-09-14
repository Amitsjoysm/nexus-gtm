# nexus/accounts/merge.py
"""Merge duplicate accounts, and transfer ownership.

`dedupe.py` stops new duplicates. This cleans up the ones already in a workspace — every account
created before that check existed, plus the ones a CSV import made while the dedupe only compared
raw strings.

**Merging moves references; it never copies data and never hard-deletes.** The loser is archived,
not removed, because signals, alerts, inbox tasks, cadence enrolments and call activity all point
at it, and the timeline that explains why somebody was contacted has to survive the tidy-up. A
merge that destroyed evidence to make a list look neat would be the worse outcome.

**Winner fields only ever fill blanks.** A merge is not an opportunity to overwrite a rep's
corrections with data from the row they are discarding — if the winner already has an industry, the
loser's does not replace it. The exception is explicit: nothing on the loser wins by default.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("nexus.accounts.merge")

# Tables that carry an `account_id` and must follow the winner. Missing one silently strands a
# record on an archived row, where the rep never sees it again — which is why this list is explicit
# rather than derived from a relationship scan that could quietly return nothing.
_ACCOUNT_REFERENCES = (
    ("nexus.models.signal", "SignalEvent"),
    ("nexus.models.alerts", "Alert"),
    ("nexus.models.workflow", "InboxTask"),
    ("nexus.models.account", "Contact"),
)


@dataclass(slots=True)
class MergeReport:
    winner_id: str = ""
    loser_id: str = ""
    moved: dict = field(default_factory=dict)
    filled: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "winner_id": self.winner_id, "loser_id": self.loser_id,
            "moved": self.moved, "filled": self.filled,
            "total_moved": sum(self.moved.values()),
        }


async def merge_accounts(ts, *, winner_id: str, loser_id: str) -> MergeReport:
    """Fold ``loser`` into ``winner``. Raises ``ValueError`` on a bad request.

    Both must belong to the caller's tenant — enforced by construction, since every read goes
    through ``ts``. Merging across tenants would be a data leak, not a feature.
    """
    from nexus.models.account import Account

    if winner_id == loser_id:
        raise ValueError("an account cannot be merged into itself")

    winner = await ts.get(Account, winner_id)
    loser = await ts.get(Account, loser_id)
    if winner is None or loser is None:
        raise ValueError("both accounts must exist in this workspace")

    report = MergeReport(winner_id=winner_id, loser_id=loser_id)

    # 1. Move every reference. Done before archiving, so a failure leaves both rows visible and the
    #    merge can simply be retried — half a merge with the loser already hidden would strand
    #    whatever had not moved yet.
    for module_path, class_name in _ACCOUNT_REFERENCES:
        model = _load(module_path, class_name)
        if model is None:
            continue
        rows = await ts.list(model, model.account_id == loser_id, limit=10_000)
        for row in rows:
            row.account_id = winner_id
        if rows:
            report.moved[class_name] = len(rows)
    await ts.flush()

    # 2. Fill blanks on the winner. Never overwrite: a merge must not undo a rep's corrections.
    #    The owner is a blank like any other: an unowned winner takes the loser's owner, so folding a
    #    duplicate in never leaves nobody responsible for the account that survives.
    for attr in (
        "domain", "industry", "employee_count", "country", "description", "crm_id", "owner_user_id",
    ):
        loser_value = getattr(loser, attr, None)
        if loser_value and not getattr(winner, attr, None):
            setattr(winner, attr, loser_value)
            report.filled.append(attr)

    # Tech stack is a set, so union rather than replace — the losing row may know something.
    loser_stack = list(getattr(loser, "tech_stack", None) or [])
    if loser_stack:
        merged = list(dict.fromkeys([*(winner.tech_stack or []), *loser_stack]))
        if merged != list(winner.tech_stack or []):
            winner.tech_stack = merged
            report.filled.append("tech_stack")

    # 3. Archive the loser, never delete it. The row is the anchor for anything this list missed,
    #    and for anyone reading an old link.
    # Through `set_archived`, the model's single write-point: it also mirrors the legacy
    # `custom_fields['archived']` boolean, and setting the column directly would leave the two
    # disagreeing for any reader still on the old field.
    loser.set_archived(True, reason=f"merged into {winner_id}")
    loser.custom_fields = {**(loser.custom_fields or {}), "merged_into": winner_id}
    await ts.flush()

    logger.info("merged account %s into %s (%s)", loser_id, winner_id, report.moved)
    return report


def _load(module_path: str, class_name: str):
    """Import a model, tolerating one that does not exist in this build."""
    try:
        module = __import__(module_path, fromlist=[class_name])
        return getattr(module, class_name, None)
    except Exception:
        logger.debug("merge: model %s.%s unavailable", module_path, class_name)
        return None


async def transfer_ownership(ts, *, from_user_id: str, to_user_id: str) -> dict:
    """Move one person's open work, and the accounts they own, to another.

    The case this exists for: somebody leaves. Their queue must not become work nobody is
    accountable for, and reassigning it one task at a time through the UI is how half a book gets
    missed.

    Only OPEN tasks move: reassigning someone's completed history would rewrite who did what, which
    is the audit trail, not a queue. **All** of their accounts move, archived ones included, since
    accounts gained an owner (migration 0056): ownership is not history, it is who to ask today. An
    account left owned by somebody who has gone is one whose "only my accounts" alerts reach nobody,
    with a name on the page pointing at a person who is not there.

    Idempotent, and a no-op when the two are the same person. ``moved`` still counts tasks, so
    existing callers read the same number they always did; accounts are ``accounts_moved``.
    """
    from nexus.models.account import Account
    from nexus.models.workflow import InboxTask

    if from_user_id == to_user_id:
        return {"moved": 0, "accounts_moved": 0, "reason": "same_user"}

    open_tasks = await ts.list(
        InboxTask,
        InboxTask.owner_user_id == from_user_id,
        InboxTask.status.in_(("open", "pending", "snoozed")),
        limit=10_000,
    )
    for task in open_tasks:
        task.owner_user_id = to_user_id

    owned = await ts.list(Account, Account.owner_user_id == from_user_id, limit=100_000)
    for account in owned:
        account.owner_user_id = to_user_id
    await ts.flush()
    logger.info("transferred %d open tasks and %d accounts from %s to %s",
                len(open_tasks), len(owned), from_user_id, to_user_id)
    return {
        "moved": len(open_tasks), "accounts_moved": len(owned),
        "from_user_id": from_user_id, "to_user_id": to_user_id,
    }
