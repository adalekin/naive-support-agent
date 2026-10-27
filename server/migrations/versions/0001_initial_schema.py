"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-27 21:24:17.422279

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_login",
        sa.Column("telegram_user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_username", sa.String(length=64), nullable=True),
        sa.Column("device_code", sa.LargeBinary(), nullable=False),
        sa.Column("verification_uri_complete", sa.String(length=1024), nullable=False),
        sa.Column("interval_seconds", sa.BigInteger(), nullable=False),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("telegram_user_id", name=op.f("pk_device_login")),
    )
    op.create_table(
        "incident",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("cause", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incident")),
        sa.UniqueConstraint("cause", name=op.f("uq_incident_cause")),
    )
    op.create_table(
        "staff_binding",
        sa.Column("telegram_user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("account_name", sa.String(length=255), nullable=False),
        sa.Column("offline_token", sa.LargeBinary(), nullable=False),
        sa.Column("telegram_username", sa.String(length=64), nullable=True),
        sa.Column("bound_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("telegram_user_id", name=op.f("pk_staff_binding")),
        sa.UniqueConstraint("subject", name=op.f("uq_staff_binding_subject")),
    )
    op.create_table(
        "telegram_offset",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("next_update_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_offset")),
    )
    op.create_table(
        "case",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("root_message_id", sa.BigInteger(), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False),
        sa.Column("staff_subject", sa.String(length=255), nullable=False),
        sa.Column("client_account", sa.String(length=255), nullable=True),
        sa.Column(
            "state",
            sa.Enum(
                "investigating",
                "waiting_client",
                "waiting_fix",
                "waiting_owner",
                "resolved",
                name="casestate",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("waiting_for", sa.Text(), nullable=True),
        sa.Column("owner_took_over", sa.Boolean(), nullable=False),
        sa.Column("conversation", sa.LargeBinary(), nullable=False),
        sa.Column("labels", sa.LargeBinary(), nullable=False),
        sa.Column("incident_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["incident_id"], ["incident.id"], name=op.f("fk_case_incident_id_incident")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case")),
        sa.UniqueConstraint("chat_id", "root_message_id", name=op.f("uq_case_chat_id")),
    )
    op.create_table(
        "case_message",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("message_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("case_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "role",
            sa.Enum("request", "answer", "feedback_question", "note", name="messagerole", native_enum=False, length=32),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["case_id"], ["case.id"], name=op.f("fk_case_message_case_id_case"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("chat_id", "message_id", name=op.f("pk_case_message")),
    )
    op.create_table(
        "case_turn",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("case_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "request",
                "owner_mention",
                "retry",
                "owner_answer",
                "follow_up",
                name="turnkind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["case_id"], ["case.id"], name=op.f("fk_case_turn_case_id_case"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_turn")),
    )
    op.create_index(op.f("ix_case_turn_case_id"), "case_turn", ["case_id"], unique=False)
    op.create_table(
        "journal_entry",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "request",
                "refused",
                "bound",
                "unbound",
                "escalated",
                "source_call",
                "usage",
                "feedback",
                name="journalkind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("case_id", sa.BigInteger(), nullable=True),
        sa.Column("client_account", sa.String(length=255), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"], ["case.id"], name=op.f("fk_journal_entry_case_id_case"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_journal_entry")),
    )


def downgrade() -> None:
    op.drop_table("journal_entry")
    op.drop_index(op.f("ix_case_turn_case_id"), table_name="case_turn")
    op.drop_table("case_turn")
    op.drop_table("case_message")
    op.drop_table("case")
    op.drop_table("telegram_offset")
    op.drop_table("staff_binding")
    op.drop_table("incident")
    op.drop_table("device_login")
