"""persist active agent skills

Revision ID: 0002_active_skills
Revises: 0001_initial
Create Date: 2026-09-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_active_skills"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A server default makes this migration safe for existing session rows.
    op.add_column(
        "agent_sessions",
        sa.Column(
            "active_skills",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "agent_sessions",
        "active_skills",
    )
