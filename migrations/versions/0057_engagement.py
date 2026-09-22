"""engagement engine + training ledger: mailboxes, sequences, conversations, replies, consent

Additive only. Creates every table the SDR engagement engine and the training & insights ledger
need (spec §4), plus three columns on existing tables:

* ``pending_registrations.training_consent`` — the sign-up choice has to survive the OTP step,
  because the tenant (and so the consent row) is only created after the code is verified. Server
  default true, matching the pre-selected box (D24).
* ``call_tasks.engagement_enrollment_id`` — call steps link to the new enrollments; the old
  ``cadence_enrollment_id`` column stays for history.

Nothing reads these tables until the engagement code ships; every one carries ``tenant_id`` so
``scripts/apply_rls.py`` enrols it on deploy. The old campaign and cadence tables are untouched —
they remain as read-only history (D13).

Revision ID: 0057_engagement
Revises: 0056_alert_routing
Create Date: 2026-09-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_engagement"
down_revision = "0056_alert_routing"
branch_labels = None
depends_on = None


def _id() -> sa.Column:
    return sa.Column("id", sa.String(length=32), primary_key=True)


def _tenant() -> sa.Column:
    return sa.Column("tenant_id", sa.String(length=32), nullable=False)


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    ]


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "mailbox_connections",
        _id(), _tenant(),
        sa.Column("owner_user_id", sa.String(length=32), sa.ForeignKey("users.id"),
                  nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("tokens", sa.JSON(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="connected"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sync_cursor", sa.Text(), nullable=True),
        _ts("last_synced_at"),
        _ts("notifications_expire_at"),
        sa.Column("notification_subscription_id", sa.String(length=255), nullable=True),
        _ts("paused_until"),
        sa.Column("signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("reply_confidence", sa.Float(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        *_stamps(),
        sa.UniqueConstraint("tenant_id", "email", name="uq_mailbox_connection_email"),
    )
    op.create_index("ix_mailbox_connections_tenant_id", "mailbox_connections", ["tenant_id"])
    op.create_index("ix_mailbox_connections_owner_user_id", "mailbox_connections",
                    ["owner_user_id"])
    op.create_index("ix_mailbox_connections_email", "mailbox_connections", ["email"])
    op.create_index("ix_mailbox_connections_status", "mailbox_connections", ["status"])
    op.create_index("ix_mailbox_connections_notification_subscription_id",
                    "mailbox_connections", ["notification_subscription_id"])

    op.create_table(
        "sequence_templates",
        _id(), _tenant(),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=32), nullable=True),
        _ts("archived_at"),
        sa.Column("legacy_cadence_id", sa.String(length=32), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_sequence_templates_tenant_id", "sequence_templates", ["tenant_id"])
    op.create_index("ix_sequence_templates_legacy_cadence_id", "sequence_templates",
                    ["legacy_cadence_id"])

    op.create_table(
        "engagement_campaigns",
        _id(), _tenant(),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), sa.ForeignKey("users.id"),
                  nullable=False),
        sa.Column("mailbox_connection_id", sa.String(length=32),
                  sa.ForeignKey("mailbox_connections.id"), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("pause_reason", sa.String(length=40), nullable=True),
        sa.Column("review_every_touch", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("first_send_mode", sa.String(length=16), nullable=False,
                  server_default="on_approval"),
        _ts("first_send_at"),
        sa.Column("timezone_mode", sa.String(length=10), nullable=False,
                  server_default="contact"),
        sa.Column("source_list_id", sa.String(length=32), sa.ForeignKey("prospect_lists.id"),
                  nullable=True),
        sa.Column("template_id", sa.String(length=32), nullable=True),
        sa.Column("credit_estimate", sa.JSON(), nullable=False),
        _ts("launched_at"),
        _ts("completed_at"),
        sa.Column("legacy_campaign_id", sa.String(length=32), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_engagement_campaigns_tenant_id", "engagement_campaigns", ["tenant_id"])
    op.create_index("ix_engagement_campaign_tenant_status", "engagement_campaigns",
                    ["tenant_id", "status"])
    op.create_index("ix_engagement_campaigns_owner_user_id", "engagement_campaigns",
                    ["owner_user_id"])
    op.create_index("ix_engagement_campaigns_mailbox_connection_id", "engagement_campaigns",
                    ["mailbox_connection_id"])
    op.create_index("ix_engagement_campaigns_legacy_campaign_id", "engagement_campaigns",
                    ["legacy_campaign_id"])

    op.create_table(
        "engagement_steps",
        _id(), _tenant(),
        sa.Column("campaign_id", sa.String(length=32), sa.ForeignKey("engagement_campaigns.id"),
                  nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=False, server_default="email"),
        sa.Column("angle", sa.Text(), nullable=False, server_default=""),
        sa.Column("timing_mode", sa.String(length=10), nullable=False, server_default="auto"),
        sa.Column("delay_business_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("send_time_local", sa.String(length=5), nullable=True),
        sa.Column("allowed_weekdays", sa.JSON(), nullable=False),
        *_stamps(),
        sa.UniqueConstraint("campaign_id", "step_index", name="uq_engagement_step_index"),
    )
    op.create_index("ix_engagement_steps_tenant_id", "engagement_steps", ["tenant_id"])
    op.create_index("ix_engagement_steps_campaign_id", "engagement_steps", ["campaign_id"])

    op.create_table(
        "engagement_enrollments",
        _id(), _tenant(),
        sa.Column("campaign_id", sa.String(length=32), sa.ForeignKey("engagement_campaigns.id"),
                  nullable=False),
        sa.Column("contact_id", sa.String(length=32), sa.ForeignKey("contacts.id"),
                  nullable=False),
        sa.Column("account_id", sa.String(length=32), sa.ForeignKey("accounts.id"),
                  nullable=False),
        sa.Column("mailbox_connection_id", sa.String(length=32),
                  sa.ForeignKey("mailbox_connections.id"), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="awaiting_review"),
        sa.Column("status_reason", sa.String(length=40), nullable=True),
        sa.Column("current_step_index", sa.Integer(), nullable=False, server_default="0"),
        _ts("next_action_at"),
        sa.Column("next_action_override", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        _ts("snoozed_until"),
        sa.Column("current_thread_id", sa.String(length=32), nullable=True),
        sa.Column("contact_timezone", sa.String(length=64), nullable=False,
                  server_default="UTC"),
        _ts("started_at"),
        _ts("finished_at"),
        sa.Column("legacy_enrollment_id", sa.String(length=32), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("campaign_id", "contact_id", name="uq_engagement_enrollment_contact"),
    )
    op.create_index("ix_engagement_enrollments_tenant_id", "engagement_enrollments",
                    ["tenant_id"])
    op.create_index("ix_engagement_enrollment_due", "engagement_enrollments",
                    ["status", "next_action_at"])
    for col in ("campaign_id", "contact_id", "account_id", "mailbox_connection_id",
                "current_thread_id", "legacy_enrollment_id"):
        op.create_index(f"ix_engagement_enrollments_{col}", "engagement_enrollments", [col])

    op.create_table(
        "engagement_threads",
        _id(), _tenant(),
        sa.Column("mailbox_connection_id", sa.String(length=32),
                  sa.ForeignKey("mailbox_connections.id"), nullable=False),
        sa.Column("provider_thread_id", sa.String(length=255), nullable=False),
        sa.Column("contact_id", sa.String(length=32), sa.ForeignKey("contacts.id"),
                  nullable=True),
        sa.Column("account_id", sa.String(length=32), sa.ForeignKey("accounts.id"),
                  nullable=True),
        sa.Column("enrollment_id", sa.String(length=32),
                  sa.ForeignKey("engagement_enrollments.id"), nullable=True),
        sa.Column("base_subject", sa.Text(), nullable=False, server_default=""),
        _ts("last_message_at"),
        *_stamps(),
        sa.UniqueConstraint("mailbox_connection_id", "provider_thread_id",
                            name="uq_engagement_thread_provider"),
    )
    op.create_index("ix_engagement_threads_tenant_id", "engagement_threads", ["tenant_id"])
    for col in ("mailbox_connection_id", "contact_id", "account_id", "enrollment_id"):
        op.create_index(f"ix_engagement_threads_{col}", "engagement_threads", [col])

    op.create_table(
        "engagement_messages",
        _id(), _tenant(),
        sa.Column("mailbox_connection_id", sa.String(length=32),
                  sa.ForeignKey("mailbox_connections.id"), nullable=False),
        sa.Column("thread_id", sa.String(length=32), sa.ForeignKey("engagement_threads.id"),
                  nullable=True),
        sa.Column("enrollment_id", sa.String(length=32),
                  sa.ForeignKey("engagement_enrollments.id"), nullable=True),
        sa.Column("contact_id", sa.String(length=32), sa.ForeignKey("contacts.id"),
                  nullable=True),
        sa.Column("direction", sa.String(length=3), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False, server_default="step"),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("rfc_message_id", sa.String(length=255), nullable=True),
        sa.Column("in_reply_to", sa.String(length=255), nullable=True),
        sa.Column("references_header", sa.Text(), nullable=False, server_default=""),
        sa.Column("ref_header", sa.String(length=40), nullable=True),
        sa.Column("from_addr", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("to_addrs", sa.JSON(), nullable=False),
        sa.Column("cc_addrs", sa.JSON(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("body_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_subject", sa.Text(), nullable=True),
        sa.Column("ai_body", sa.Text(), nullable=True),
        sa.Column("quality_problems", sa.JSON(), nullable=False),
        sa.Column("inbound_kind", sa.String(length=12), nullable=True),
        sa.Column("approved_by_user_id", sa.String(length=32), nullable=True),
        _ts("approved_at"),
        _ts("sent_at"),
        _ts("received_at"),
        sa.Column("error", sa.Text(), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("mailbox_connection_id", "provider_message_id",
                            name="uq_engagement_message_provider"),
    )
    op.create_index("ix_engagement_messages_tenant_id", "engagement_messages", ["tenant_id"])
    for col in ("mailbox_connection_id", "enrollment_id", "contact_id", "rfc_message_id",
                "ref_header"):
        op.create_index(f"ix_engagement_messages_{col}", "engagement_messages", [col])
    op.create_index("ix_engagement_message_thread_time", "engagement_messages",
                    ["thread_id", "created_at"])
    op.create_index(
        "uq_engagement_message_out_step", "engagement_messages", ["enrollment_id", "step_index"],
        unique=True, postgresql_where=sa.text("direction = 'out'"),
        sqlite_where=sa.text("direction = 'out'"),
    )
    op.create_index(
        "uq_engagement_message_idempotency", "engagement_messages",
        ["tenant_id", "idempotency_key"], unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "reply_classifications",
        _id(), _tenant(),
        sa.Column("message_id", sa.String(length=32), sa.ForeignKey("engagement_messages.id"),
                  nullable=False),
        sa.Column("mailbox_connection_id", sa.String(length=32),
                  sa.ForeignKey("mailbox_connections.id"), nullable=False),
        sa.Column("enrollment_id", sa.String(length=32),
                  sa.ForeignKey("engagement_enrollments.id"), nullable=True),
        sa.Column("contact_id", sa.String(length=32), sa.ForeignKey("contacts.id"),
                  nullable=True),
        sa.Column("account_id", sa.String(length=32), sa.ForeignKey("accounts.id"),
                  nullable=True),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("date_phrase", sa.String(length=200), nullable=True),
        sa.Column("resolved_date", sa.Date(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=False, server_default=""),
        sa.Column("label_source", sa.String(length=16), nullable=False, server_default="ai"),
        sa.Column("corrected_category", sa.String(length=20), nullable=True),
        sa.Column("action_taken", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("decided_by_user_id", sa.String(length=32), nullable=True),
        _ts("decided_at"),
        sa.Column("suggested_response", sa.Text(), nullable=True),
        sa.Column("assigned_user_id", sa.String(length=32), nullable=True),
        _ts("responded_at"),
        _ts("reminded_at"),
        sa.Column("status", sa.String(length=8), nullable=False, server_default="open"),
        *_stamps(),
        sa.UniqueConstraint("message_id", name="uq_reply_classification_message"),
    )
    op.create_index("ix_reply_classifications_tenant_id", "reply_classifications", ["tenant_id"])
    op.create_index("ix_reply_classification_desk", "reply_classifications",
                    ["tenant_id", "status", "category"])
    for col in ("mailbox_connection_id", "enrollment_id", "assigned_user_id"):
        op.create_index(f"ix_reply_classifications_{col}", "reply_classifications", [col])

    op.create_table(
        "do_not_contact",
        _id(), _tenant(),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("contact_id", sa.String(length=32), sa.ForeignKey("contacts.id"),
                  nullable=True),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("source_message_id", sa.String(length=32), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=32), nullable=True),
        _ts("lifted_at"),
        sa.Column("lifted_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("lift_note", sa.Text(), nullable=False, server_default=""),
        *_stamps(),
    )
    op.create_index("ix_do_not_contact_tenant_id", "do_not_contact", ["tenant_id"])
    op.create_index("ix_do_not_contact_email", "do_not_contact", ["email"])
    op.create_index(
        "uq_do_not_contact_active", "do_not_contact", ["tenant_id", "email"], unique=True,
        postgresql_where=sa.text("lifted_at IS NULL"), sqlite_where=sa.text("lifted_at IS NULL"),
    )

    op.create_table(
        "training_consents",
        _id(), _tenant(),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("terms_version", sa.String(length=20), nullable=False),
        sa.Column("decided_by_user_id", sa.String(length=32), nullable=True),
        _ts("decided_at", nullable=False),
        *_stamps(),
    )
    op.create_index("ix_training_consents_tenant_id", "training_consents", ["tenant_id"])
    op.create_index("ix_training_consent_tenant_decided", "training_consents",
                    ["tenant_id", "decided_at"])

    op.create_table(
        "ledger_outbox",
        _id(), _tenant(),
        sa.Column("event_id", sa.String(length=26), nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        _ts("occurred_at", nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        _ts("shipped_archive_at"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_ledger_outbox_tenant_id", "ledger_outbox", ["tenant_id"])
    op.create_index("ix_ledger_outbox_event_type", "ledger_outbox", ["event_type"])
    op.create_index("uq_ledger_outbox_event", "ledger_outbox", ["event_id"], unique=True)
    op.create_index("ix_ledger_outbox_shipping", "ledger_outbox",
                    ["shipped_archive_at", "occurred_at"])

    op.add_column(
        "pending_registrations",
        sa.Column("training_consent", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # batch mode: SQLite cannot add a foreign key in place (same as 0056).
    with op.batch_alter_table("call_tasks") as batch:
        batch.add_column(
            sa.Column("engagement_enrollment_id", sa.String(length=32), nullable=True)
        )
        batch.create_foreign_key(
            "fk_call_tasks_engagement_enrollment_id", "engagement_enrollments",
            ["engagement_enrollment_id"], ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("call_tasks") as batch:
        batch.drop_constraint("fk_call_tasks_engagement_enrollment_id", type_="foreignkey")
        batch.drop_column("engagement_enrollment_id")
    with op.batch_alter_table("pending_registrations") as batch:
        batch.drop_column("training_consent")
    for table in (
        "ledger_outbox", "training_consents", "do_not_contact", "reply_classifications",
        "engagement_messages", "engagement_threads", "engagement_enrollments",
        "engagement_steps", "engagement_campaigns", "sequence_templates", "mailbox_connections",
    ):
        op.drop_table(table)
