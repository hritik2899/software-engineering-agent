"""Domain contracts shared by API, orchestration and runtime layers."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"


class EnvironmentStatus(StrEnum):
    REQUESTED = "requested"
    PROVISIONING = "provisioning"
    READY = "ready"
    BUSY = "busy"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"
    DELETED = "deleted"


class EventType(StrEnum):
    TASK_CREATED = "task.created"
    TASK_QUEUED = "task.queued"
    TASK_STARTED = "task.started"
    TASK_PAUSED = "task.paused"
    TASK_RESUMED = "task.resumed"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    TASK_RECOVERED = "task.recovered"
    ENVIRONMENT_ALLOCATED = "environment.allocated"
    ENVIRONMENT_READY = "environment.ready"
    ENVIRONMENT_HEARTBEAT = "environment.heartbeat"
    AGENT_STEP = "agent.step"
    AGENT_MESSAGE = "agent.message"
    USER_MESSAGE = "user.message"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    CONTEXT_COMPACTED = "context.compacted"
    CHECKPOINT_CREATED = "checkpoint.created"
    PR_CREATED = "pr.created"


class RepositorySpec(BaseModel):
    url: str
    base_branch: str = "main"
    name: str | None = None


class TaskCreate(BaseModel):
    instruction: str = Field(min_length=3)
    repositories: list[RepositorySpec] = Field(default_factory=list)
    user_id: str = "local-user"
    publish_pr: bool = False


class TaskView(BaseModel):
    id: str
    session_id: str
    environment_id: str | None
    user_id: str
    instruction: str
    status: TaskStatus
    repositories: list[RepositorySpec]
    publish_pr: bool
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    result: dict[str, Any] | None = None


class SessionView(BaseModel):
    id: str
    task_id: str
    summary: str = ""
    current_plan: list[str] = Field(default_factory=list)
    active_constraints: list[str] = Field(default_factory=list)
    last_event_sequence: int = 0
    last_compacted_sequence: int = 0
    created_at: datetime
    updated_at: datetime


class AgentEvent(BaseModel):
    id: str
    task_id: str
    session_id: str
    sequence: int
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class UserInstruction(BaseModel):
    message: str = Field(min_length=1)
