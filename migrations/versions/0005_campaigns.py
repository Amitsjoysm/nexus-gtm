"""Segment Campaign Engine: campaigns + campaign_targets tables.

Revision ID: 0005_campaigns
Revises: 0004_outcomes
Create Date: 2026-06-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_campaigns"
down_revision = "0004_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("list_id", sa.String(length=32),
                  sa.ForeignKey("prospect_lists.id"), nullable=False),
        sa.Column("icp", sa.JSON(), nullable=True),
        sa.Column("sequence", sa.String(length=120), nullable=False,
                  server_default="ai-orchestrated-outbound"),
        sa.Column("status", sa.String(length=24), nullable=False,
                  server_default="draft_pending"),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=32),
                  sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_campaigns_tenant_id", "campaigns", ["tenant_id"])
    op.create_index("ix_campaigns_list_id", "campaigns", ["list_id"])
    op.create_index("ix_campaigns_status", "campaigns", ["status"])
    op.create_index("ix_campaign_tenant_status", "campaigns", ["tenant_id", "status"])

    op.create_table(
        "campaign_targets",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("campaign_id", sa.String(length=32),
                  sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("account_id", sa.String(length=32),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False,
                  server_default="pending"),
        sa.Column("skip_reason", sa.String(length=40), nullable=True),
        sa.Column("draft", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_campaign_targets_tenant_id", "campaign_targets", ["tenant_id"])
    op.create_index("ix_campaign_targets_campaign_id", "campaign_targets", ["campaign_id"])
    op.create_index("ix_campaign_targets_account_id", "campaign_targets", ["account_id"])
    op.create_index("ix_campaign_targets_status", "campaign_targets", ["status"])
    op.create_index(
        "ix_camptarget_campaign_status", "campaign_targets", ["campaign_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_camptarget_campaign_status", table_name="campaign_targets")
    op.drop_index("ix_campaign_targets_status", table_name="campaign_targets")
    op.drop_index("ix_campaign_targets_account_id", table_name="campaign_targets")
    op.drop_index("ix_campaign_targets_campaign_id", table_name="campaign_targets")
    op.drop_index("ix_campaign_targets_tenant_id", table_name="campaign_targets")
    op.drop_table("campaign_targets")
    op.drop_index("ix_campaign_tenant_status", table_name="campaigns")
    op.drop_index("ix_campaigns_status", table_name="campaigns")
    op.drop_index("ix_campaigns_list_id", table_name="campaigns")
    op.drop_index("ix_campaigns_tenant_id", table_name="campaigns")
    op.drop_table("campaigns")
