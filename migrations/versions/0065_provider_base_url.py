"""provider settings: the endpoint of an OpenAI-compatible LLM

The Superadmin panel took a key and a model for "OpenAI-compatible" and had nowhere to say which
endpoint they belong to; the base URL was ``NEXUS_LLM_BASE_URL`` only, so pointing at OpenRouter, a
self-hosted vLLM or any other compatible service needed a redeploy.

Empty means "no override": the environment value applies, exactly as ``model`` does on this table.

Revision ID: 0065_provider_base_url
Revises: 0064_list_kinds
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0065_provider_base_url"
down_revision = "0064_list_kinds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("provider_settings", sa.Column(
        "base_url", sa.String(length=300), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("provider_settings", "base_url")
