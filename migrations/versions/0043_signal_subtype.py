"""Signal subtype — the finer grain inside a kind.

Additive and nullable. Every existing row keeps `subtype = NULL`, which reads as "no finer grain
known", and every existing query, scoring weight, play condition and alert rule is untouched
because they all key on `kind`.

M17 uses it for hiring: `surge` / `new_function` / `seniority_shift` are subtypes of `hiring`
rather than new entries in `SIGNAL_KINDS`. Extending the kind vocabulary would have forced every
consumer to learn three new strings at once — a new INTENT_WEIGHT, an alert rule and a play
condition each — whereas a subtype is opt-in for whoever wants it. Promoting a subtype to a kind
later is easy; demoting one is a data migration, so this is the reversible direction.

Indexed because the query it will serve is "show me the seniority shifts", which is a filter on a
column with very low cardinality alongside `kind` — the same shape as `ix_signal_events_kind`.

Revision ID: 0043_signal_subtype
Revises: 0042_account_next_refresh
Create Date: 2026-08-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043_signal_subtype"
down_revision = "0042_account_next_refresh"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signal_events", sa.Column("subtype", sa.String(length=40), nullable=True))
    op.create_index("ix_signal_events_subtype", "signal_events", ["subtype"])


def downgrade() -> None:
    op.drop_index("ix_signal_events_subtype", table_name="signal_events")
    op.drop_column("signal_events", "subtype")
