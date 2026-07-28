"""Frozen baseline schema (everything before billing).

This single revision replaces the former 0001-0020 chain. The old ``0001_initial`` called
``Base.metadata.create_all()``, which materialized whatever models happened to be registered at
the time it ran rather than the schema as of 0001 — so it drifted forward with every new model
and pre-created tables that later revisions then failed to create ("table chat_sessions already
exists"). The chain could therefore never be replayed onto an empty database, which is why
``scripts/bootstrap_db.py`` had to special-case a fresh database with create_all + ``stamp head``.

The DDL below is LITERAL and FROZEN: it does not read ``Base.metadata``, so it cannot drift
again. New tables and columns belong in new revisions, never here.

The revision identifier is deliberately kept as ``0020_account_archived_at`` — the last revision
this squash absorbs — so that ``0021_billing_foundation.down_revision`` still resolves and any
database already stamped at 0020 or later (production is well past it) upgrades exactly as
before. Databases stamped at 0001-0019 are not upgradable across this squash; none exist.

Revision ID: 0020_account_archived_at
Revises:
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import nexus.core.db  # noqa: F401  (TZDateTime is referenced by the DDL below)

revision = "0020_account_archived_at"
down_revision = None
branch_labels = None
depends_on = None

# Creation order, so downgrade can drop in reverse without tripping foreign keys.
_TABLES = (
    "accounts",
    "custom_field_defs",
    "network_persons",
    "pending_registrations",
    "plays",
    "relevance_profiles",
    "tenants",
    "users",
    "workspaces",
    "account_scores",
    "agent_runs",
    "cadences",
    "chat_sessions",
    "contacts",
    "memberships",
    "password_resets",
    "prospect_lists",
    "cadence_steps",
    "campaigns",
    "chat_messages",
    "list_items",
    "network_source_accounts",
    "orchestration_runs",
    "signal_events",
    "alerts",
    "campaign_targets",
    "inbox_tasks",
    "network_edges",
    "network_identities",
    "outcomes",
    "play_runs",
    "run_events",
    "run_steps",
    "approvals",
    "cadence_enrollments",
    "cadence_touches",
    "call_tasks",
    "call_activities",
)


def upgrade() -> None:
    op.create_table('accounts',
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('domain', sa.String(length=255), nullable=True),
    sa.Column('industry', sa.String(length=120), nullable=True),
    sa.Column('employee_count', sa.Integer(), nullable=True),
    sa.Column('country', sa.String(length=80), nullable=True),
    sa.Column('tech_stack', sa.JSON(), nullable=False),
    sa.Column('crm_id', sa.String(length=120), nullable=True),
    sa.Column('crm_source', sa.String(length=40), nullable=True),
    sa.Column('custom_fields', sa.JSON(), nullable=False),
    sa.Column('source', sa.String(length=40), nullable=True),
    sa.Column('last_refreshed_at', nexus.core.db.TZDateTime(timezone=True), nullable=True),
    sa.Column('crm_synced_at', nexus.core.db.TZDateTime(timezone=True), nullable=True),
    sa.Column('archived_at', nexus.core.db.TZDateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'domain', name='uq_account_domain')
    )
    op.create_index(op.f('ix_accounts_archived_at'), 'accounts', ['archived_at'], unique=False)
    op.create_index(op.f('ix_accounts_crm_synced_at'), 'accounts', ['crm_synced_at'], unique=False)
    op.create_index(op.f('ix_accounts_domain'), 'accounts', ['domain'], unique=False)
    op.create_index(op.f('ix_accounts_last_refreshed_at'), 'accounts', ['last_refreshed_at'], unique=False)
    op.create_index(op.f('ix_accounts_name'), 'accounts', ['name'], unique=False)
    op.create_index(op.f('ix_accounts_tenant_id'), 'accounts', ['tenant_id'], unique=False)
    op.create_table('custom_field_defs',
    sa.Column('entity', sa.String(length=12), nullable=False),
    sa.Column('key', sa.String(length=60), nullable=False),
    sa.Column('label', sa.String(length=120), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'entity', 'key', name='uq_custom_field_key')
    )
    op.create_index(op.f('ix_custom_field_defs_tenant_id'), 'custom_field_defs', ['tenant_id'], unique=False)
    op.create_table('network_persons',
    sa.Column('primary_email', sa.String(length=255), nullable=True),
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('first_name', sa.String(length=100), nullable=False),
    sa.Column('last_name', sa.String(length=100), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('company', sa.String(length=200), nullable=False),
    sa.Column('company_domain', sa.String(length=200), nullable=False),
    sa.Column('location', sa.String(length=200), nullable=False),
    sa.Column('country', sa.String(length=100), nullable=False),
    sa.Column('linkedin_url', sa.String(length=300), nullable=True),
    sa.Column('twitter_handle', sa.String(length=100), nullable=True),
    sa.Column('photo_url', sa.String(length=500), nullable=True),
    sa.Column('profile', sa.JSON(), nullable=False),
    sa.Column('search_text', sa.String(length=600), nullable=False),
    sa.Column('identity_count', sa.Integer(), nullable=False),
    sa.Column('edge_count', sa.Integer(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_network_person_domain', 'network_persons', ['tenant_id', 'company_domain'], unique=False)
    op.create_index('ix_network_person_email', 'network_persons', ['tenant_id', 'primary_email'], unique=False)
    op.create_index(op.f('ix_network_persons_search_text'), 'network_persons', ['search_text'], unique=False)
    op.create_index(op.f('ix_network_persons_tenant_id'), 'network_persons', ['tenant_id'], unique=False)
    op.create_table('pending_registrations',
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('company_name', sa.String(length=200), nullable=False),
    sa.Column('company_slug', sa.String(length=80), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('otp_hash', sa.String(length=128), nullable=False),
    sa.Column('expires_at', nexus.core.db.TZDateTime(timezone=True), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('resends', sa.Integer(), nullable=False),
    sa.Column('last_sent_at', nexus.core.db.TZDateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_pending_registrations_email'), 'pending_registrations', ['email'], unique=True)
    op.create_table('plays',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('trigger', sa.JSON(), nullable=False),
    sa.Column('actions', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_plays_tenant_id'), 'plays', ['tenant_id'], unique=False)
    op.create_table('relevance_profiles',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('icp', sa.JSON(), nullable=False),
    sa.Column('value_props', sa.JSON(), nullable=False),
    sa.Column('product_context', sa.Text(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_relevance_profiles_tenant_id'), 'relevance_profiles', ['tenant_id'], unique=True)
    op.create_table('tenants',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('slug', sa.String(length=80), nullable=False),
    sa.Column('automation_enabled', sa.Boolean(), nullable=False),
    sa.Column('icp_discovery_last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('icp_daily_count', sa.Integer(), nullable=True),
    sa.Column('email_settings', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('users',
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_table('workspaces',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_workspaces_tenant_id'), 'workspaces', ['tenant_id'], unique=False)
    op.create_table('account_scores',
    sa.Column('account_id', sa.String(length=32), nullable=False),
    sa.Column('icp_fit', sa.Integer(), nullable=False),
    sa.Column('intent', sa.Integer(), nullable=False),
    sa.Column('health', sa.Integer(), nullable=False),
    sa.Column('composite', sa.Integer(), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('model_version', sa.String(length=40), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_account_scores_account_id'), 'account_scores', ['account_id'], unique=False)
    op.create_index(op.f('ix_account_scores_composite'), 'account_scores', ['composite'], unique=False)
    op.create_index(op.f('ix_account_scores_tenant_id'), 'account_scores', ['tenant_id'], unique=False)
    op.create_index('ix_score_tenant_account_computed', 'account_scores', ['tenant_id', 'account_id', 'computed_at'], unique=False)
    op.create_index('ix_score_tenant_computed', 'account_scores', ['tenant_id', 'computed_at'], unique=False)
    op.create_table('agent_runs',
    sa.Column('agent', sa.String(length=40), nullable=False),
    sa.Column('account_id', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('input', sa.JSON(), nullable=False),
    sa.Column('output', sa.JSON(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('tokens', sa.Integer(), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_agent_runs_agent'), 'agent_runs', ['agent'], unique=False)
    op.create_index(op.f('ix_agent_runs_tenant_id'), 'agent_runs', ['tenant_id'], unique=False)
    op.create_index('ix_agentrun_tenant_created', 'agent_runs', ['tenant_id', 'created_at'], unique=False)
    op.create_table('cadences',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_by_user_id', sa.String(length=32), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cadence_tenant', 'cadences', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_cadences_tenant_id'), 'cadences', ['tenant_id'], unique=False)
    op.create_table('chat_sessions',
    sa.Column('created_by', sa.String(length=32), nullable=True),
    sa.Column('account_id', sa.String(length=32), nullable=True),
    sa.Column('parent_session_id', sa.String(length=32), nullable=True),
    sa.Column('title', sa.String(length=160), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('target', sa.String(length=16), nullable=True),
    sa.Column('icp_state', sa.JSON(), nullable=False),
    sa.Column('missing_slots', sa.JSON(), nullable=False),
    sa.Column('context_summary', sa.Text(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['parent_session_id'], ['chat_sessions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_chat_session_tenant_account', 'chat_sessions', ['tenant_id', 'account_id'], unique=False)
    op.create_index('ix_chat_session_tenant_status', 'chat_sessions', ['tenant_id', 'status'], unique=False)
    op.create_index(op.f('ix_chat_sessions_account_id'), 'chat_sessions', ['account_id'], unique=False)
    op.create_index(op.f('ix_chat_sessions_status'), 'chat_sessions', ['status'], unique=False)
    op.create_index(op.f('ix_chat_sessions_tenant_id'), 'chat_sessions', ['tenant_id'], unique=False)
    op.create_table('contacts',
    sa.Column('account_id', sa.String(length=32), nullable=False),
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('seniority', sa.String(length=40), nullable=True),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('phone', sa.String(length=40), nullable=True),
    sa.Column('linkedin_url', sa.String(length=255), nullable=True),
    sa.Column('email_confidence', sa.Float(), nullable=False),
    sa.Column('email_status', sa.String(length=20), nullable=True),
    sa.Column('email_checked_at', nexus.core.db.TZDateTime(timezone=True), nullable=True),
    sa.Column('phone_confidence', sa.Float(), nullable=False),
    sa.Column('enrichment_source', sa.String(length=60), nullable=True),
    sa.Column('custom_fields', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_contact_tenant_created', 'contacts', ['tenant_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_contacts_account_id'), 'contacts', ['account_id'], unique=False)
    op.create_index(op.f('ix_contacts_tenant_id'), 'contacts', ['tenant_id'], unique=False)
    op.create_table('memberships',
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('workspace_id', sa.String(length=32), nullable=True),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'user_id', name='uq_membership_user')
    )
    op.create_index(op.f('ix_memberships_tenant_id'), 'memberships', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_memberships_user_id'), 'memberships', ['user_id'], unique=False)
    op.create_table('password_resets',
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('token_hash', sa.String(length=128), nullable=False),
    sa.Column('expires_at', nexus.core.db.TZDateTime(timezone=True), nullable=False),
    sa.Column('used', sa.Boolean(), nullable=False),
    sa.Column('last_sent_at', nexus.core.db.TZDateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_password_resets_email'), 'password_resets', ['email'], unique=True)
    op.create_index(op.f('ix_password_resets_user_id'), 'password_resets', ['user_id'], unique=False)
    op.create_table('prospect_lists',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('owner_user_id', sa.String(length=32), nullable=True),
    sa.Column('filter', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_prospect_lists_tenant_id'), 'prospect_lists', ['tenant_id'], unique=False)
    op.create_table('cadence_steps',
    sa.Column('cadence_id', sa.String(length=32), nullable=False),
    sa.Column('step_index', sa.Integer(), nullable=False),
    sa.Column('delay_days', sa.Integer(), nullable=False),
    sa.Column('angle', sa.Text(), nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['cadence_id'], ['cadences.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('cadence_id', 'step_index', name='uq_cadence_step_index')
    )
    op.create_index(op.f('ix_cadence_steps_cadence_id'), 'cadence_steps', ['cadence_id'], unique=False)
    op.create_index(op.f('ix_cadence_steps_tenant_id'), 'cadence_steps', ['tenant_id'], unique=False)
    op.create_table('campaigns',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('list_id', sa.String(length=32), nullable=False),
    sa.Column('icp', sa.JSON(), nullable=False),
    sa.Column('sequence', sa.String(length=120), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('report', sa.JSON(), nullable=False),
    sa.Column('send_risky', sa.Boolean(), nullable=False),
    sa.Column('cadence_id', sa.String(length=32), nullable=True),
    sa.Column('review_each_touch', sa.Boolean(), nullable=False),
    sa.Column('created_by_user_id', sa.String(length=32), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['cadence_id'], ['cadences.id'], ),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['list_id'], ['prospect_lists.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_campaign_tenant_status', 'campaigns', ['tenant_id', 'status'], unique=False)
    op.create_index(op.f('ix_campaigns_list_id'), 'campaigns', ['list_id'], unique=False)
    op.create_index(op.f('ix_campaigns_status'), 'campaigns', ['status'], unique=False)
    op.create_index(op.f('ix_campaigns_tenant_id'), 'campaigns', ['tenant_id'], unique=False)
    op.create_table('chat_messages',
    sa.Column('session_id', sa.String(length=32), nullable=False),
    sa.Column('seq', sa.BigInteger(), nullable=False),
    sa.Column('role', sa.String(length=12), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('session_id', 'seq', name='uq_chat_msg_seq')
    )
    op.create_index(op.f('ix_chat_messages_session_id'), 'chat_messages', ['session_id'], unique=False)
    op.create_index(op.f('ix_chat_messages_tenant_id'), 'chat_messages', ['tenant_id'], unique=False)
    op.create_index('ix_chat_msg_session_seq', 'chat_messages', ['session_id', 'seq'], unique=False)
    op.create_table('list_items',
    sa.Column('list_id', sa.String(length=32), nullable=False),
    sa.Column('account_id', sa.String(length=32), nullable=False),
    sa.Column('contact_id', sa.String(length=32), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], ),
    sa.ForeignKeyConstraint(['list_id'], ['prospect_lists.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_list_items_account_id'), 'list_items', ['account_id'], unique=False)
    op.create_index(op.f('ix_list_items_list_id'), 'list_items', ['list_id'], unique=False)
    op.create_index(op.f('ix_list_items_tenant_id'), 'list_items', ['tenant_id'], unique=False)
    op.create_table('network_source_accounts',
    sa.Column('member_id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('provider', sa.String(length=16), nullable=False),
    sa.Column('external_account_id', sa.String(length=255), nullable=False),
    sa.Column('display_email', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('pooling_enabled', sa.Boolean(), nullable=False),
    sa.Column('oauth', sa.JSON(), nullable=False),
    sa.Column('sync_cursor', sa.String(length=255), nullable=True),
    sa.Column('last_synced_at', nexus.core.db.TZDateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.String(length=500), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['member_id'], ['memberships.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'member_id', 'provider', 'external_account_id', name='uq_network_source')
    )
    op.create_index(op.f('ix_network_source_accounts_member_id'), 'network_source_accounts', ['member_id'], unique=False)
    op.create_index(op.f('ix_network_source_accounts_tenant_id'), 'network_source_accounts', ['tenant_id'], unique=False)
    op.create_index('ix_network_source_member', 'network_source_accounts', ['tenant_id', 'member_id'], unique=False)
    op.create_table('orchestration_runs',
    sa.Column('goal', sa.String(length=60), nullable=False),
    sa.Column('goal_input', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('plan', sa.JSON(), nullable=False),
    sa.Column('blackboard', sa.JSON(), nullable=False),
    sa.Column('account_id', sa.String(length=32), nullable=True),
    sa.Column('created_by', sa.String(length=32), nullable=True),
    sa.Column('chat_session_id', sa.String(length=32), nullable=True),
    sa.Column('idempotency_key', sa.String(length=120), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['chat_session_id'], ['chat_sessions.id'], ),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'idempotency_key', name='uq_run_idempotency')
    )
    op.create_index(op.f('ix_orchestration_runs_account_id'), 'orchestration_runs', ['account_id'], unique=False)
    op.create_index(op.f('ix_orchestration_runs_chat_session_id'), 'orchestration_runs', ['chat_session_id'], unique=False)
    op.create_index(op.f('ix_orchestration_runs_goal'), 'orchestration_runs', ['goal'], unique=False)
    op.create_index(op.f('ix_orchestration_runs_status'), 'orchestration_runs', ['status'], unique=False)
    op.create_index(op.f('ix_orchestration_runs_tenant_id'), 'orchestration_runs', ['tenant_id'], unique=False)
    op.create_index('ix_run_tenant_status', 'orchestration_runs', ['tenant_id', 'status'], unique=False)
    op.create_table('signal_events',
    sa.Column('account_id', sa.String(length=32), nullable=True),
    sa.Column('contact_id', sa.String(length=32), nullable=True),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=60), nullable=False),
    sa.Column('title', sa.String(length=400), nullable=False),
    sa.Column('body', sa.Text(), nullable=True),
    sa.Column('url', sa.String(length=500), nullable=True),
    sa.Column('strength', sa.Float(), nullable=False),
    sa.Column('dedupe_key', sa.String(length=200), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'dedupe_key', name='uq_signal_dedupe')
    )
    op.create_index(op.f('ix_signal_events_account_id'), 'signal_events', ['account_id'], unique=False)
    op.create_index(op.f('ix_signal_events_dedupe_key'), 'signal_events', ['dedupe_key'], unique=False)
    op.create_index(op.f('ix_signal_events_kind'), 'signal_events', ['kind'], unique=False)
    op.create_index(op.f('ix_signal_events_tenant_id'), 'signal_events', ['tenant_id'], unique=False)
    op.create_index('ix_signal_tenant_occurred', 'signal_events', ['tenant_id', 'occurred_at'], unique=False)
    op.create_table('alerts',
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('account_id', sa.String(length=32), nullable=True),
    sa.Column('signal_id', sa.String(length=32), nullable=True),
    sa.Column('source', sa.String(length=40), nullable=False),
    sa.Column('meta', sa.JSON(), nullable=False),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('acked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('acked_by', sa.String(length=32), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['acked_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['signal_id'], ['signal_events.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_alert_tenant_created', 'alerts', ['tenant_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_alerts_account_id'), 'alerts', ['account_id'], unique=False)
    op.create_index(op.f('ix_alerts_severity'), 'alerts', ['severity'], unique=False)
    op.create_index(op.f('ix_alerts_status'), 'alerts', ['status'], unique=False)
    op.create_index(op.f('ix_alerts_tenant_id'), 'alerts', ['tenant_id'], unique=False)
    op.create_table('campaign_targets',
    sa.Column('campaign_id', sa.String(length=32), nullable=False),
    sa.Column('account_id', sa.String(length=32), nullable=False),
    sa.Column('run_id', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('skip_reason', sa.String(length=40), nullable=True),
    sa.Column('draft', sa.JSON(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_campaign_targets_account_id'), 'campaign_targets', ['account_id'], unique=False)
    op.create_index(op.f('ix_campaign_targets_campaign_id'), 'campaign_targets', ['campaign_id'], unique=False)
    op.create_index(op.f('ix_campaign_targets_status'), 'campaign_targets', ['status'], unique=False)
    op.create_index(op.f('ix_campaign_targets_tenant_id'), 'campaign_targets', ['tenant_id'], unique=False)
    op.create_index('ix_camptarget_campaign_status', 'campaign_targets', ['campaign_id', 'status'], unique=False)
    op.create_table('inbox_tasks',
    sa.Column('owner_user_id', sa.String(length=32), nullable=True),
    sa.Column('account_id', sa.String(length=32), nullable=True),
    sa.Column('contact_id', sa.String(length=32), nullable=True),
    sa.Column('signal_id', sa.String(length=32), nullable=True),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('suggested_action', sa.JSON(), nullable=False),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], ),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['signal_id'], ['signal_events.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_inbox_tasks_account_id'), 'inbox_tasks', ['account_id'], unique=False)
    op.create_index(op.f('ix_inbox_tasks_owner_user_id'), 'inbox_tasks', ['owner_user_id'], unique=False)
    op.create_index(op.f('ix_inbox_tasks_priority'), 'inbox_tasks', ['priority'], unique=False)
    op.create_index(op.f('ix_inbox_tasks_tenant_id'), 'inbox_tasks', ['tenant_id'], unique=False)
    op.create_table('network_edges',
    sa.Column('owner_member_id', sa.String(length=32), nullable=False),
    sa.Column('owner_user_id', sa.String(length=32), nullable=False),
    sa.Column('person_id', sa.String(length=32), nullable=False),
    sa.Column('source_account_id', sa.String(length=32), nullable=False),
    sa.Column('provider', sa.String(length=16), nullable=False),
    sa.Column('relation', sa.String(length=16), nullable=False),
    sa.Column('strength', sa.Integer(), nullable=False),
    sa.Column('email_count', sa.Integer(), nullable=False),
    sa.Column('sent_count', sa.Integer(), nullable=False),
    sa.Column('received_count', sa.Integer(), nullable=False),
    sa.Column('meeting_count', sa.Integer(), nullable=False),
    sa.Column('first_touch_at', nexus.core.db.TZDateTime(timezone=True), nullable=True),
    sa.Column('last_touch_at', nexus.core.db.TZDateTime(timezone=True), nullable=True),
    sa.Column('mutual_count', sa.Integer(), nullable=False),
    sa.Column('pooling_enabled', sa.Boolean(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['owner_member_id'], ['memberships.id'], ),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['person_id'], ['network_persons.id'], ),
    sa.ForeignKeyConstraint(['source_account_id'], ['network_source_accounts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'owner_member_id', 'person_id', 'provider', name='uq_network_edge')
    )
    op.create_index('ix_network_edge_owner', 'network_edges', ['tenant_id', 'owner_member_id'], unique=False)
    op.create_index('ix_network_edge_person', 'network_edges', ['tenant_id', 'person_id', 'pooling_enabled', 'strength'], unique=False)
    op.create_index(op.f('ix_network_edges_owner_member_id'), 'network_edges', ['owner_member_id'], unique=False)
    op.create_index(op.f('ix_network_edges_person_id'), 'network_edges', ['person_id'], unique=False)
    op.create_index(op.f('ix_network_edges_tenant_id'), 'network_edges', ['tenant_id'], unique=False)
    op.create_table('network_identities',
    sa.Column('source_account_id', sa.String(length=32), nullable=False),
    sa.Column('person_id', sa.String(length=32), nullable=True),
    sa.Column('provider', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('name', sa.String(length=200), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('company', sa.String(length=200), nullable=True),
    sa.Column('handle', sa.String(length=100), nullable=True),
    sa.Column('raw', sa.JSON(), nullable=False),
    sa.Column('resolution_key', sa.String(length=255), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['person_id'], ['network_persons.id'], ),
    sa.ForeignKeyConstraint(['source_account_id'], ['network_source_accounts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'source_account_id', 'external_id', name='uq_network_identity')
    )
    op.create_index(op.f('ix_network_identities_source_account_id'), 'network_identities', ['source_account_id'], unique=False)
    op.create_index(op.f('ix_network_identities_tenant_id'), 'network_identities', ['tenant_id'], unique=False)
    op.create_index('ix_network_identity_key', 'network_identities', ['tenant_id', 'resolution_key'], unique=False)
    op.create_index('ix_network_identity_person', 'network_identities', ['tenant_id', 'person_id'], unique=False)
    op.create_table('outcomes',
    sa.Column('stage', sa.String(length=20), nullable=False),
    sa.Column('account_id', sa.String(length=32), nullable=True),
    sa.Column('contact_id', sa.String(length=32), nullable=True),
    sa.Column('campaign_id', sa.String(length=32), nullable=True),
    sa.Column('industry', sa.String(length=120), nullable=True),
    sa.Column('employee_count', sa.Integer(), nullable=True),
    sa.Column('country', sa.String(length=80), nullable=True),
    sa.Column('tech_count', sa.Integer(), nullable=False),
    sa.Column('meta', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_outcomes_account_id'), 'outcomes', ['account_id'], unique=False)
    op.create_index(op.f('ix_outcomes_campaign_id'), 'outcomes', ['campaign_id'], unique=False)
    op.create_index(op.f('ix_outcomes_stage'), 'outcomes', ['stage'], unique=False)
    op.create_index(op.f('ix_outcomes_tenant_id'), 'outcomes', ['tenant_id'], unique=False)
    op.create_table('play_runs',
    sa.Column('play_id', sa.String(length=32), nullable=False),
    sa.Column('account_id', sa.String(length=32), nullable=True),
    sa.Column('signal_id', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['play_id'], ['plays.id'], ),
    sa.ForeignKeyConstraint(['signal_id'], ['signal_events.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_play_runs_play_id'), 'play_runs', ['play_id'], unique=False)
    op.create_index(op.f('ix_play_runs_tenant_id'), 'play_runs', ['tenant_id'], unique=False)
    op.create_table('run_events',
    sa.Column('run_id', sa.String(length=32), nullable=False),
    sa.Column('seq', sa.BigInteger(), nullable=False),
    sa.Column('type', sa.String(length=60), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['orchestration_runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'seq', name='uq_event_run_seq')
    )
    op.create_index('ix_event_run_seq', 'run_events', ['run_id', 'seq'], unique=False)
    op.create_index(op.f('ix_run_events_run_id'), 'run_events', ['run_id'], unique=False)
    op.create_index(op.f('ix_run_events_tenant_id'), 'run_events', ['tenant_id'], unique=False)
    op.create_table('run_steps',
    sa.Column('run_id', sa.String(length=32), nullable=False),
    sa.Column('idx', sa.Integer(), nullable=False),
    sa.Column('tool', sa.String(length=60), nullable=False),
    sa.Column('inputs', sa.JSON(), nullable=False),
    sa.Column('depends_on', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('requires_approval', sa.Boolean(), nullable=False),
    sa.Column('approval_id', sa.String(length=32), nullable=True),
    sa.Column('output', sa.JSON(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['orchestration_runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'idx', name='uq_step_run_idx')
    )
    op.create_index(op.f('ix_run_steps_run_id'), 'run_steps', ['run_id'], unique=False)
    op.create_index(op.f('ix_run_steps_status'), 'run_steps', ['status'], unique=False)
    op.create_index(op.f('ix_run_steps_tenant_id'), 'run_steps', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_run_steps_tool'), 'run_steps', ['tool'], unique=False)
    op.create_index('ix_step_run_status', 'run_steps', ['run_id', 'status'], unique=False)
    op.create_table('approvals',
    sa.Column('run_id', sa.String(length=32), nullable=False),
    sa.Column('step_id', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=60), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('decided_by', sa.String(length=32), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('edits', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['orchestration_runs.id'], ),
    sa.ForeignKeyConstraint(['step_id'], ['run_steps.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_approval_tenant_status', 'approvals', ['tenant_id', 'status'], unique=False)
    op.create_index(op.f('ix_approvals_run_id'), 'approvals', ['run_id'], unique=False)
    op.create_index(op.f('ix_approvals_status'), 'approvals', ['status'], unique=False)
    op.create_index(op.f('ix_approvals_step_id'), 'approvals', ['step_id'], unique=False)
    op.create_index(op.f('ix_approvals_tenant_id'), 'approvals', ['tenant_id'], unique=False)
    op.create_table('cadence_enrollments',
    sa.Column('campaign_id', sa.String(length=32), nullable=False),
    sa.Column('campaign_target_id', sa.String(length=32), nullable=True),
    sa.Column('account_id', sa.String(length=32), nullable=False),
    sa.Column('contact_id', sa.String(length=32), nullable=True),
    sa.Column('cadence_id', sa.String(length=32), nullable=False),
    sa.Column('current_step_index', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('stop_reason', sa.String(length=16), nullable=True),
    sa.Column('next_touch_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['cadence_id'], ['cadences.id'], ),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ),
    sa.ForeignKeyConstraint(['campaign_target_id'], ['campaign_targets.id'], ),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_cadence_enrollments_account_id'), 'cadence_enrollments', ['account_id'], unique=False)
    op.create_index(op.f('ix_cadence_enrollments_cadence_id'), 'cadence_enrollments', ['cadence_id'], unique=False)
    op.create_index(op.f('ix_cadence_enrollments_campaign_id'), 'cadence_enrollments', ['campaign_id'], unique=False)
    op.create_index(op.f('ix_cadence_enrollments_status'), 'cadence_enrollments', ['status'], unique=False)
    op.create_index(op.f('ix_cadence_enrollments_tenant_id'), 'cadence_enrollments', ['tenant_id'], unique=False)
    op.create_index('ix_enrollment_campaign', 'cadence_enrollments', ['campaign_id'], unique=False)
    op.create_index('ix_enrollment_status_due', 'cadence_enrollments', ['status', 'next_touch_at'], unique=False)
    op.create_table('cadence_touches',
    sa.Column('enrollment_id', sa.String(length=32), nullable=False),
    sa.Column('step_index', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('skip_reason', sa.String(length=40), nullable=True),
    sa.Column('draft', sa.JSON(), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['enrollment_id'], ['cadence_enrollments.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('enrollment_id', 'step_index', name='uq_touch_enrollment_step')
    )
    op.create_index(op.f('ix_cadence_touches_enrollment_id'), 'cadence_touches', ['enrollment_id'], unique=False)
    op.create_index(op.f('ix_cadence_touches_tenant_id'), 'cadence_touches', ['tenant_id'], unique=False)
    op.create_table('call_tasks',
    sa.Column('account_id', sa.String(length=32), nullable=False),
    sa.Column('contact_id', sa.String(length=32), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('owner_user_id', sa.String(length=32), nullable=True),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cadence_enrollment_id', sa.String(length=32), nullable=True),
    sa.Column('cadence_step_index', sa.Integer(), nullable=True),
    sa.Column('script_cache', sa.JSON(), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['cadence_enrollment_id'], ['cadence_enrollments.id'], ),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], ),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_call_task_contact', 'call_tasks', ['contact_id'], unique=False)
    op.create_index('ix_call_task_status', 'call_tasks', ['tenant_id', 'status'], unique=False)
    op.create_index(op.f('ix_call_tasks_account_id'), 'call_tasks', ['account_id'], unique=False)
    op.create_index(op.f('ix_call_tasks_tenant_id'), 'call_tasks', ['tenant_id'], unique=False)
    op.create_table('call_activities',
    sa.Column('call_task_id', sa.String(length=32), nullable=True),
    sa.Column('account_id', sa.String(length=32), nullable=False),
    sa.Column('contact_id', sa.String(length=32), nullable=True),
    sa.Column('disposition', sa.String(length=24), nullable=False),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('duration_s', sa.Integer(), nullable=True),
    sa.Column('next_step', sa.Text(), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('recording_url', sa.Text(), nullable=True),
    sa.Column('transcript', sa.Text(), nullable=True),
    sa.Column('ai_summary', sa.Text(), nullable=True),
    sa.Column('sentiment', sa.String(length=16), nullable=True),
    sa.Column('provider_call_id', sa.String(length=64), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['call_task_id'], ['call_tasks.id'], ),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_call_activities_account_id'), 'call_activities', ['account_id'], unique=False)
    op.create_index(op.f('ix_call_activities_tenant_id'), 'call_activities', ['tenant_id'], unique=False)
    op.create_index('ix_call_activity_account', 'call_activities', ['account_id'], unique=False)
    op.create_index('ix_call_activity_contact', 'call_activities', ['contact_id'], unique=False)


def downgrade() -> None:
    for name in reversed(_TABLES):
        op.drop_table(name)
