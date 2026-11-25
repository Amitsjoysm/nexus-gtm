"""a one-off send from an app-password mailbox is recorded like any other

`engagement_messages.mailbox_connection_id` was NOT NULL with a foreign key to
`mailbox_connections`, which only an OAuth (Gmail / Microsoft) mailbox has. An app-password SMTP
mailbox is a dict inside `tenants.email_settings`, so there was no id to put in the column and the
SMTP send path wrote no row at all: `send_to_contact` metered `outreach.email_send` and returned.

The consequence was not a missing report. `replies/matching.py` keeps an inbound email only when it
matches a known thread, an `In-Reply-To`/`References` naming one of OUR `rfc_message_id`s, or a
contact with a live enrollment — so a one-off email sent from an app-password mailbox left nothing
for a reply to match, and the buyer's answer was discarded unread, subject and body included. The
rep saw it in their mail client and the product never did.

Nullable, not a second column: the row is otherwise identical, and every reader already treats the
mailbox as optional for a message with no thread. `uq_engagement_message_provider`
(mailbox_connection_id, provider_message_id) keeps working — Postgres and SQLite both count NULLs
as distinct in a unique index, so several SMTP sends do not collide with each other.

DROP NOT NULL takes an ACCESS EXCLUSIVE lock and rewrites nothing, so it is the fast kind of DDL
rather than the kind that stalled a boot on 2026-11-06. SQLite ignores the constraint either way;
the replay test is what proves the two agree.

**Numbering:** `0080_prospector_vocabulary` exists on the unpushed `fix/sdr-data-accuracy` branch.
Whichever lands second renumbers and re-parents, the resolution this repo has used three times
(see CLAUDE.md, Migrations). Check `ScriptDirectory.get_heads()` returns exactly one before merging.

Revision ID: 0080_smtp_sends_recorded
Revises: 0079_mailbox_style
Create Date: 2026-11-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0080_smtp_sends_recorded"
down_revision = "0079_mailbox_style"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("engagement_messages") as batch:
        batch.alter_column("mailbox_connection_id", existing_type=sa.String(32), nullable=True)


def downgrade() -> None:
    # Deliberately not reversed. Rows written by an SMTP send carry NULL here, and restoring the
    # constraint would refuse them — a downgrade that cannot run is worse than one that does less.
    pass
