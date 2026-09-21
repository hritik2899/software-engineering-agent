"""SQLAlchemy persistence models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from minion.domain import TaskStatus, utcnow


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    environment_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    user_id: Mapped[str] = mapped_column(String(120), index=True)
    instruction: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default=TaskStatus.CREATED.value, index=True)
    repositories: Mapped[list[dict]] = mapped_column(JSON)
    publish_pr: Mapped[bool] = mapped_column(default=False)
    version: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[SessionRow] = relationship(back_populates="task", uselist=False)


class SessionRow(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), unique=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    current_plan: Mapped[list[str]] = mapped_column(JSON, default=list)
    active_constraints: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    last_compacted_sequence: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped[TaskRow] = relationship(back_populates="session")


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_task_sequence", "task_id", "sequence", unique=True),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EnvironmentLeaseRow(Base):
    __tablename__ = "environment_leases"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), index=True)
    workspace_path: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskLeaseRow(Base):
    __tablename__ = "task_leases"

    task_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(120), index=True)
    expires_at_epoch: Mapped[float] = mapped_column(Float, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CheckpointRow(Base):
    __tablename__ = "checkpoints"
    __table_args__ = (
        Index("ix_checkpoints_task_repo_created", "task_id", "repo_name", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    repo_name: Mapped[str] = mapped_column(String(240))
    base_branch: Mapped[str] = mapped_column(String(240))
    commit_sha: Mapped[str] = mapped_column(String(80))
    binary_patch: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
