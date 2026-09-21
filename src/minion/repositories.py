"""Repositories around durable control-plane state."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from minion.domain import (
    RepositorySpec,
    SessionView,
    TaskCreate,
    TaskStatus,
    TaskView,
    new_id,
    utcnow,
)
from minion.errors import InvalidStateTransition
from minion.models import SessionRow, TaskRow
from minion.state_machine import validate_transition


def _task_view(row: TaskRow) -> TaskView:
    return TaskView(
        id=row.id,
        session_id=row.session_id,
        environment_id=row.environment_id,
        user_id=row.user_id,
        instruction=row.instruction,
        status=TaskStatus(row.status),
        repositories=[RepositorySpec.model_validate(r) for r in row.repositories],
        publish_pr=row.publish_pr,
        created_at=row.created_at,
        updated_at=row.updated_at,
        error=row.error,
        result=row.result,
    )


class TaskRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, request: TaskCreate) -> TaskView:
        task_id, session_id = new_id("task"), new_id("session")
        task = TaskRow(
            id=task_id,
            session_id=session_id,
            user_id=request.user_id,
            instruction=request.instruction,
            status=TaskStatus.CREATED.value,
            repositories=[r.model_dump() for r in request.repositories],
            publish_pr=request.publish_pr,
        )
        session = SessionRow(id=session_id, task_id=task_id)
        self.db.add_all([task, session])
        await self.db.commit()
        await self.db.refresh(task)
        return _task_view(task)

    async def get(self, task_id: str) -> TaskView | None:
        row = await self.db.get(TaskRow, task_id)
        return _task_view(row) if row else None

    async def require(self, task_id: str) -> TaskView:
        task = await self.get(task_id)
        if not task:
            raise KeyError(task_id)
        return task

    async def transition(
        self,
        task_id: str,
        target: TaskStatus,
        *,
        environment_id: str | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> TaskView:
        """Optimistic state transition.

        The WHERE version=... guard prevents two orchestrator workers from both
        successfully claiming the same task after a duplicate delivery.
        """
        row = await self.db.get(TaskRow, task_id)
        if not row:
            raise KeyError(task_id)

        current = TaskStatus(row.status)
        validate_transition(current, target)
        expected_version = row.version
        values: dict[str, Any] = {
            "status": target.value,
            "version": expected_version + 1,
            "updated_at": utcnow(),
        }
        if environment_id is not None:
            values["environment_id"] = environment_id
        if error is not None:
            values["error"] = error
        if result is not None:
            values["result"] = result

        stmt = (
            update(TaskRow)
            .where(TaskRow.id == task_id, TaskRow.version == expected_version)
            .values(**values)
        )
        result_proxy = await self.db.execute(stmt)
        if result_proxy.rowcount != 1:
            await self.db.rollback()
            raise InvalidStateTransition("task changed concurrently; retry from fresh state")
        await self.db.commit()
        return await self.require(task_id)

    async def list_active(self) -> list[TaskView]:
        stmt = select(TaskRow).where(
            TaskRow.status.in_(
                [
                    TaskStatus.QUEUED.value,
                    TaskStatus.PROVISIONING.value,
                    TaskStatus.RUNNING.value,
                    TaskStatus.WAITING_FOR_USER.value,
                    TaskStatus.CANCELLING.value,
                ]
            )
        )
        rows = (await self.db.scalars(stmt)).all()
        return [_task_view(row) for row in rows]


class SessionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, session_id: str) -> SessionView | None:
        row = await self.db.get(SessionRow, session_id)
        if not row:
            return None
        return SessionView(
            id=row.id,
            task_id=row.task_id,
            summary=row.summary,
            current_plan=row.current_plan or [],
            active_constraints=row.active_constraints or [],
            last_event_sequence=row.last_event_sequence,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def update_memory(
        self,
        session_id: str,
        *,
        summary: str | None = None,
        current_plan: list[str] | None = None,
        active_constraints: list[str] | None = None,
    ) -> None:
        values: dict[str, Any] = {"updated_at": utcnow()}
        if summary is not None:
            values["summary"] = summary
        if current_plan is not None:
            values["current_plan"] = current_plan
        if active_constraints is not None:
            values["active_constraints"] = active_constraints
        await self.db.execute(
            update(SessionRow).where(SessionRow.id == session_id).values(**values)
        )
        await self.db.commit()
