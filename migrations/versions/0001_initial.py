"""Initial Minion control-plane schema.

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("session_id", sa.String(length=80), nullable=False),
        sa.Column("environment_id", sa.String(length=120)),
        sa.Column("user_id", sa.String(length=120), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("repositories", sa.JSON(), nullable=False),
        sa.Column("publish_pr", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id"),
    )
    op.create_index("ix_tasks_environment_id", "tasks", ["environment_id"])
    op.create_index("ix_tasks_session_id", "tasks", ["session_id"], unique=True)
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_user_id", "tasks", ["user_id"])

    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("current_plan", sa.JSON(), nullable=False),
        sa.Column("active_constraints", sa.JSON(), nullable=False),
        sa.Column("last_event_sequence", sa.Integer(), nullable=False),
        sa.Column("last_compacted_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_id"),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("session_id", sa.String(length=80), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_events_session_id", "events", ["session_id"])
    op.create_index("ix_events_task_id", "events", ["task_id"])
    op.create_index(
        "ix_events_task_sequence",
        "events",
        ["task_id", "sequence"],
        unique=True,
    )
    op.create_index("ix_events_type", "events", ["type"])

    op.create_table(
        "environment_leases",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column("task_id", sa.String(length=80)),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_environment_leases_status", "environment_leases", ["status"])
    op.create_index("ix_environment_leases_task_id", "environment_leases", ["task_id"])

    op.create_table(
        "task_leases",
        sa.Column("task_id", sa.String(length=80), primary_key=True),
        sa.Column("owner_id", sa.String(length=120), nullable=False),
        sa.Column("expires_at_epoch", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_leases_expires_at_epoch", "task_leases", ["expires_at_epoch"])
    op.create_index("ix_task_leases_owner_id", "task_leases", ["owner_id"])

    op.create_table(
        "checkpoints",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("session_id", sa.String(length=80), nullable=False),
        sa.Column("repo_name", sa.String(length=240), nullable=False),
        sa.Column("base_branch", sa.String(length=240), nullable=False),
        sa.Column("commit_sha", sa.String(length=80), nullable=False),
        sa.Column("binary_patch", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_checkpoints_session_id", "checkpoints", ["session_id"])
    op.create_index("ix_checkpoints_task_id", "checkpoints", ["task_id"])
    op.create_index(
        "ix_checkpoints_task_repo_created",
        "checkpoints",
        ["task_id", "repo_name", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("checkpoints")
    op.drop_table("task_leases")
    op.drop_table("environment_leases")
    op.drop_table("events")
    op.drop_table("agent_sessions")
    op.drop_table("tasks")
