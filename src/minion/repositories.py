"""Repositories around durable control-plane state."""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from minion.domain import (
    EnvironmentStatus,
    RepositorySpec,
    SessionView,
    TaskCreate,
    TaskStatus,
    TaskView,
    new_id,
    utcnow,
)
from minion.errors import InvalidStateTransition
from minion.models import (
    CheckpointRow,
    EnvironmentLeaseRow,
    SessionRow,
    TaskLeaseRow,
    TaskRow,
)
from minion.state_machine import validate_transition


def _task_view(row: TaskRow) -> TaskView:
    return TaskView(
        id=row.id,
        session_id=row.session_id,
        environment_id=row.environment_id,
        user_id=row.user_id,
        instruction=row.instruction,
        status=TaskStatus(row.status),
        repositories=[RepositorySpec.model_validate(item) for item in row.repositories],
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
            repositories=[repo.model_dump() for repo in request.repositories],
            publish_pr=request.publish_pr,
        )
        self.db.add_all([task, SessionRow(id=session_id, task_id=task_id)])
        await self.db.commit()
        await self.db.refresh(task)
        return _task_view(task)

    async def get(self, task_id: str) -> TaskView | None:
        row = await self.db.get(TaskRow, task_id)
        return _task_view(row) if row else None

    async def require(self, task_id: str) -> TaskView:
        task = await self.get(task_id)
        if task is None:
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
        clear_error: bool = False,
    ) -> TaskView:
        row = await self.db.get(TaskRow, task_id)
        if row is None:
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
        if clear_error:
            values["error"] = None
        elif error is not None:
            values["error"] = error
        if result is not None:
            values["result"] = result

        result_proxy = await self.db.execute(
            update(TaskRow)
            .where(TaskRow.id == task_id, TaskRow.version == expected_version)
            .values(**values)
        )
        if result_proxy.rowcount != 1:
            await self.db.rollback()
            raise InvalidStateTransition("task changed concurrently; retry from fresh state")
        await self.db.commit()
        return await self.require(task_id)

    async def replace_environment(self, task_id: str, environment_id: str) -> TaskView:
        await self.db.execute(
            update(TaskRow)
            .where(TaskRow.id == task_id)
            .values(environment_id=environment_id, updated_at=utcnow())
        )
        await self.db.commit()
        return await self.require(task_id)

    async def list_active(self) -> list[TaskView]:
        statement = select(TaskRow).where(
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
        rows = (await self.db.scalars(statement)).all()
        return [_task_view(row) for row in rows]


class SessionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, session_id: str) -> SessionView | None:
        row = await self.db.get(SessionRow, session_id)
        if row is None:
            return None
        return SessionView(
            id=row.id,
            task_id=row.task_id,
            summary=row.summary,
            current_plan=row.current_plan or [],
            active_constraints=row.active_constraints or [],
            last_event_sequence=row.last_event_sequence,
            last_compacted_sequence=row.last_compacted_sequence,
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
        last_compacted_sequence: int | None = None,
    ) -> None:
        values: dict[str, Any] = {"updated_at": utcnow()}
        if summary is not None:
            values["summary"] = summary
        if current_plan is not None:
            values["current_plan"] = current_plan
        if active_constraints is not None:
            values["active_constraints"] = active_constraints
        if last_compacted_sequence is not None:
            values["last_compacted_sequence"] = last_compacted_sequence
        await self.db.execute(
            update(SessionRow).where(SessionRow.id == session_id).values(**values)
        )
        await self.db.commit()

    async def append_instruction(self, session_id: str, instruction: str) -> None:
        row = await self.db.get(SessionRow, session_id)
        if row is None:
            raise KeyError(session_id)
        constraints = list(row.active_constraints or [])
        if instruction not in constraints:
            constraints.append(instruction)
        row.active_constraints = constraints[-20:]
        row.updated_at = utcnow()
        await self.db.commit()


class TaskLeaseRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def acquire(self, task_id: str, owner_id: str, ttl_seconds: int) -> bool:
        now = time.time()
        row = await self.db.get(TaskLeaseRow, task_id)
        if row is None:
            self.db.add(
                TaskLeaseRow(
                    task_id=task_id,
                    owner_id=owner_id,
                    expires_at_epoch=now + ttl_seconds,
                )
            )
            try:
                await self.db.commit()
                return True
            except IntegrityError:
                await self.db.rollback()
                return False

        result = await self.db.execute(
            update(TaskLeaseRow)
            .where(
                TaskLeaseRow.task_id == task_id,
                or_(
                    TaskLeaseRow.owner_id == owner_id,
                    TaskLeaseRow.expires_at_epoch <= now,
                ),
            )
            .values(
                owner_id=owner_id,
                expires_at_epoch=now + ttl_seconds,
                updated_at=utcnow(),
            )
        )
        await self.db.commit()
        return result.rowcount == 1

    async def heartbeat(self, task_id: str, owner_id: str, ttl_seconds: int) -> bool:
        result = await self.db.execute(
            update(TaskLeaseRow)
            .where(TaskLeaseRow.task_id == task_id, TaskLeaseRow.owner_id == owner_id)
            .values(
                expires_at_epoch=time.time() + ttl_seconds,
                updated_at=utcnow(),
            )
        )
        await self.db.commit()
        return result.rowcount == 1

    async def is_live(self, task_id: str) -> bool:
        row = await self.db.get(TaskLeaseRow, task_id)
        return bool(row and row.expires_at_epoch > time.time())

    async def owned_by(self, task_id: str, owner_id: str) -> bool:
        row = await self.db.get(TaskLeaseRow, task_id)
        return bool(
            row
            and row.owner_id == owner_id
            and row.expires_at_epoch > time.time()
        )

    async def release(self, task_id: str, owner_id: str) -> None:
        await self.db.execute(
            delete(TaskLeaseRow).where(
                TaskLeaseRow.task_id == task_id,
                TaskLeaseRow.owner_id == owner_id,
            )
        )
        await self.db.commit()


class EnvironmentLeaseRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def upsert(
        self,
        *,
        environment_id: str,
        task_id: str,
        provider: str,
        workspace_path: str,
        metadata: dict[str, Any],
        status: EnvironmentStatus = EnvironmentStatus.READY,
    ) -> None:
        row = await self.db.get(EnvironmentLeaseRow, environment_id)
        if row is None:
            row = EnvironmentLeaseRow(
                id=environment_id,
                task_id=task_id,
                provider=provider,
                workspace_path=workspace_path,
                metadata_json=metadata,
                status=status.value,
            )
            self.db.add(row)
        else:
            row.task_id = task_id
            row.provider = provider
            row.workspace_path = workspace_path
            row.metadata_json = metadata
            row.status = status.value
            row.updated_at = utcnow()
            row.last_heartbeat_at = utcnow()
        await self.db.commit()

    async def heartbeat(self, environment_id: str) -> None:
        await self.db.execute(
            update(EnvironmentLeaseRow)
            .where(EnvironmentLeaseRow.id == environment_id)
            .values(last_heartbeat_at=utcnow(), updated_at=utcnow())
        )
        await self.db.commit()

    async def mark(self, environment_id: str, status: EnvironmentStatus) -> None:
        await self.db.execute(
            update(EnvironmentLeaseRow)
            .where(EnvironmentLeaseRow.id == environment_id)
            .values(status=status.value, updated_at=utcnow())
        )
        await self.db.commit()


class CheckpointRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def save(
        self,
        *,
        task_id: str,
        session_id: str,
        repo_name: str,
        base_branch: str,
        commit_sha: str,
        binary_patch: str,
    ) -> None:
        self.db.add(
            CheckpointRow(
                id=new_id("checkpoint"),
                task_id=task_id,
                session_id=session_id,
                repo_name=repo_name,
                base_branch=base_branch,
                commit_sha=commit_sha,
                binary_patch=binary_patch,
            )
        )
        await self.db.commit()

    async def latest_for_task(self, task_id: str) -> dict[str, CheckpointRow]:
        statement = (
            select(CheckpointRow)
            .where(CheckpointRow.task_id == task_id)
            .order_by(CheckpointRow.created_at.desc())
        )
        rows = (await self.db.scalars(statement)).all()
        latest: dict[str, CheckpointRow] = {}
        for row in rows:
            latest.setdefault(row.repo_name, row)
        return latest
