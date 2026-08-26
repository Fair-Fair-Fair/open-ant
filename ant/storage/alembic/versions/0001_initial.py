"""initial tables

Revision ID: 0001
Revises:
Create Date: 2026-08-26

Phase 1 foundation: conversation history (sessions/messages), reliable
delivery outbox, dedupe registry, audit log and usage accounting.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sessions_agent_id"), "sessions", ["agent_id"])
    op.create_index(op.f("ix_sessions_updated_at"), "sessions", ["updated_at"])

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("timestamp", sa.String(length=40), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column("tool_call_id", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_messages_session_id"), "messages", ["session_id"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id"),
    )

    op.create_table(
        "processed_messages",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "usage_records",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("agent_id", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("usage_records")
    op.drop_table("audit_log")
    op.drop_table("processed_messages")
    op.drop_table("outbox_events")
    op.drop_index(op.f("ix_messages_session_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_sessions_updated_at"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_agent_id"), table_name="sessions")
    op.drop_table("sessions")
