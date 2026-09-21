"""Distributed task orchestration and recovery."""
from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from minion.auth import AuthorizationPolicy
from minion.config import Settings
from minion.domain import EnvironmentStatus, EventType, TaskCreate, TaskStatus, TaskView
from minion.errors import InvalidStateTransition
from minion.events import EventBus, EventStore
from minion.github import GitHubPublisher
from minion.logging import logger
from minion.metrics import (
    ACTIVE_TASKS,
    ENVIRONMENT_ALLOCATION,
    TASK_DURATION,
    TASKS_COMPLETED,
    TASKS_FAILED,
    TASKS_RECOVERED,
    TASKS_SUBMITTED,
)
from minion.queue import QueueMessage, WorkQueue
from minion.repositories import (
    CheckpointRepository,
    EnvironmentLeaseRepository,
    SessionRepository,
    TaskLeaseRepository,
    TaskRepository,
)
from minion.runtime.factory import build_agent
from minion.runtime.workspace import EnvironmentProvider, Workspace, restore_binary_patch

log = logger(__name__)


class Orchestrator:
    """Owns task scheduling/lifecycle; the coding agent owns engineering decisions."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        queue: WorkQueue,
        environments: EnvironmentProvider,
        bus: EventBus,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.queue = queue
        self.environments = environments
        self.bus = bus
        self.auth = AuthorizationPolicy(allow_local_repositories=settings.env != "prod")
        self.publisher = GitHubPublisher(settings)
        self.owner_id = f"orchestrator-{uuid4().hex[:12]}"
        self._worker: asyncio.Task[None] | None = None
        self._recovery: asyncio.Task[None] | None = None
        self._running: dict[str, asyncio.Task[None]] = {}
        self._stopping = asyncio.Event()
        self._slots = asyncio.Semaphore(settings.max_concurrent_tasks)
        self._lost_leases: set[str] = set()

    async def start(self) -> None:
        await self.queue.prepare()
        await self.environments.prepare()
        await self.reconcile()
        self._worker = asyncio.create_task(
            self._worker_loop(),
            name="minion-orchestrator",
        )
        self._recovery = asyncio.create_task(
            self._recovery_loop(),
            name="minion-recovery",
        )

    async def stop(self) -> None:
        """Stop this control-plane instance without turning active tasks into cancels."""
        self._stopping.set()
        for background in (self._worker, self._recovery):
            if background is not None:
                background.cancel()
        for background in (self._worker, self._recovery):
            if background is not None:
                with suppress(asyncio.CancelledError):
                    await background

        running = list(self._running.values())
        for task in running:
            task.cancel()
        for task in running:
            with suppress(asyncio.CancelledError):
                await task
        await self.queue.close()

    async def submit(self, request: TaskCreate) -> TaskView:
        await self.auth.authorize_repositories(request.user_id, request.repositories)
        async with self.session_factory() as db:
            tasks = TaskRepository(db)
            events = EventStore(db, self.bus)
            task = await tasks.create(request)
            await events.append(
                task.id,
                task.session_id,
                EventType.TASK_CREATED,
                {
                    "instruction": task.instruction,
                    "repositories": [repo.model_dump() for repo in task.repositories],
                },
            )
            task = await tasks.transition(task.id, TaskStatus.QUEUED)
            await events.append(task.id, task.session_id, EventType.TASK_QUEUED)
        TASKS_SUBMITTED.inc()
        await self.queue.put(task.id)
        return task

    async def send_instruction(self, task_id: str, message: str) -> None:
        async with self.session_factory() as db:
            task = await TaskRepository(db).require(task_id)
            await SessionRepository(db).append_instruction(task.session_id, message)
            await EventStore(db, self.bus).append(
                task.id,
                task.session_id,
                EventType.USER_MESSAGE,
                {"message": message},
            )

    async def pause(self, task_id: str) -> TaskView:
        async with self.session_factory() as db:
            tasks = TaskRepository(db)
            task = await tasks.require(task_id)
            if task.status == TaskStatus.WAITING_FOR_USER:
                return task
            if task.status != TaskStatus.RUNNING:
                raise InvalidStateTransition(
                    f"only running tasks can be paused, got {task.status}"
                )
            task = await tasks.transition(task.id, TaskStatus.WAITING_FOR_USER)
            await EventStore(db, self.bus).append(
                task.id,
                task.session_id,
                EventType.TASK_PAUSED,
            )
            return task

    async def resume(self, task_id: str) -> TaskView:
        async with self.session_factory() as db:
            tasks = TaskRepository(db)
            task = await tasks.require(task_id)
            if task.status == TaskStatus.RUNNING:
                return task
            if task.status != TaskStatus.WAITING_FOR_USER:
                raise InvalidStateTransition(
                    f"only paused tasks can be resumed, got {task.status}"
                )
            task = await tasks.transition(task.id, TaskStatus.RUNNING)
            await EventStore(db, self.bus).append(
                task.id,
                task.session_id,
                EventType.TASK_RESUMED,
            )
            return task

    async def cancel(self, task_id: str) -> TaskView:
        async with self.session_factory() as db:
            tasks = TaskRepository(db)
            task = await tasks.require(task_id)
            if task.status in {
                TaskStatus.CANCELLED,
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
            }:
                return task
            if task.status != TaskStatus.CANCELLING:
                task = await tasks.transition(task.id, TaskStatus.CANCELLING)

            local = self._running.get(task_id)
            if local is not None:
                local.cancel()

            task = await tasks.transition(task.id, TaskStatus.CANCELLED)
            await EventStore(db, self.bus).append(
                task.id,
                task.session_id,
                EventType.TASK_CANCELLED,
            )
            return task

    async def retry(self, task_id: str) -> TaskView:
        async with self.session_factory() as db:
            tasks = TaskRepository(db)
            task = await tasks.require(task_id)
            if task.status != TaskStatus.FAILED:
                return task
            task = await tasks.transition(
                task.id,
                TaskStatus.QUEUED,
                clear_error=True,
            )
            await EventStore(db, self.bus).append(
                task.id,
                task.session_id,
                EventType.TASK_QUEUED,
                {"reason": "manual_retry"},
            )
        await self.queue.put(task.id)
        return task

    async def reconcile(self) -> None:
        async with self.session_factory() as db:
            active = await TaskRepository(db).list_active()
            leases = TaskLeaseRepository(db)
            for task in active:
                if task.status == TaskStatus.CANCELLING:
                    continue
                if not await leases.is_live(task.id):
                    await self.queue.put(task.id)

    async def _recovery_loop(self) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(self.settings.recovery_scan_interval_seconds)
            try:
                await self._recover_stale_tasks()
            except (OSError, RuntimeError):
                log.exception("recovery_scan_failed")

    async def _recover_stale_tasks(self) -> None:
        async with self.session_factory() as db:
            active = await TaskRepository(db).list_active()
            leases = TaskLeaseRepository(db)
            for task in active:
                if task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
                    continue
                if not await leases.is_live(task.id):
                    await self.queue.put(task.id)
                    TASKS_RECOVERED.inc()

    async def _worker_loop(self) -> None:
        while not self._stopping.is_set():
            message = await self.queue.get()
            await self._slots.acquire()

            async with self.session_factory() as db:
                acquired = await TaskLeaseRepository(db).acquire(
                    message.task_id,
                    self.owner_id,
                    self.settings.task_lease_seconds,
                )
            if not acquired:
                self._slots.release()
                await self.queue.ack(message)
                continue

            await self.queue.ack(message)
            execution = asyncio.create_task(
                self._execute(message),
                name=f"task-{message.task_id}",
            )
            self._running[message.task_id] = execution
            execution.add_done_callback(
                lambda _future, task_id=message.task_id: self._execution_done(task_id)
            )

    def _execution_done(self, task_id: str) -> None:
        self._running.pop(task_id, None)
        self._lost_leases.discard(task_id)
        self._slots.release()

    async def _heartbeat_loop(
        self,
        task_id: str,
        environment_id: str,
    ) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_interval_seconds)
            async with self.session_factory() as db:
                task_ok = await TaskLeaseRepository(db).heartbeat(
                    task_id,
                    self.owner_id,
                    self.settings.task_lease_seconds,
                )
                await EnvironmentLeaseRepository(db).heartbeat(environment_id)
            if not task_ok:
                self._lost_leases.add(task_id)
                execution = self._running.get(task_id)
                if execution is not None:
                    execution.cancel()
                return

    async def _should_cancel(self, task_id: str) -> bool:
        async with self.session_factory() as db:
            task = await TaskRepository(db).require(task_id)
            owned = await TaskLeaseRepository(db).owned_by(task_id, self.owner_id)
            return (
                task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}
                or not owned
            )

    async def _should_pause(self, task_id: str) -> bool:
        async with self.session_factory() as db:
            task = await TaskRepository(db).require(task_id)
            return task.status == TaskStatus.WAITING_FOR_USER

    async def _restore_checkpoints(
        self,
        task: TaskView,
        workspace: Workspace,
    ) -> None:
        async with self.session_factory() as db:
            checkpoints = await CheckpointRepository(db).latest_for_task(task.id)
        for repo, checkpoint in checkpoints.items():
            if repo not in workspace.repositories:
                continue
            await restore_binary_patch(
                workspace,
                repo,
                checkpoint.binary_patch,
            )

    async def _execute(self, message: QueueMessage) -> None:
        task_id = message.task_id
        workspace: Workspace | None = None
        heartbeat: asyncio.Task[None] | None = None
        final_status: TaskStatus | None = None
        started = time.monotonic()
        ACTIVE_TASKS.inc()

        try:
            async with self.session_factory() as db:
                tasks = TaskRepository(db)
                events = EventStore(db, self.bus)
                task = await tasks.require(task_id)
                if task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
                    final_status = task.status
                    return

                if task.status == TaskStatus.QUEUED:
                    task = await tasks.transition(task.id, TaskStatus.PROVISIONING)

                old_environment_id = task.environment_id
                allocation_started = time.monotonic()
                if task.environment_id:
                    workspace = await self.environments.attach(
                        task.environment_id,
                        task.repositories,
                    )

                replaced_environment = (
                    workspace is None and old_environment_id is not None
                )
                if workspace is None:
                    workspace = await self.environments.allocate(
                        task.id,
                        task.repositories,
                    )
                    await events.append(
                        task.id,
                        task.session_id,
                        EventType.ENVIRONMENT_ALLOCATED,
                        {"environment_id": workspace.environment_id},
                    )
                ENVIRONMENT_ALLOCATION.observe(time.monotonic() - allocation_started)

                if replaced_environment:
                    await self._restore_checkpoints(task, workspace)

                await EnvironmentLeaseRepository(db).upsert(
                    environment_id=workspace.environment_id,
                    task_id=task.id,
                    provider=self.settings.environment_provider,
                    workspace_path=str(workspace.root),
                    metadata={"container_name": workspace.container_name},
                )

                if task.status == TaskStatus.PROVISIONING:
                    task = await tasks.transition(
                        task.id,
                        TaskStatus.RUNNING,
                        environment_id=workspace.environment_id,
                    )
                elif (
                    task.status in {TaskStatus.RUNNING, TaskStatus.WAITING_FOR_USER}
                    and task.environment_id != workspace.environment_id
                ):
                    task = await tasks.replace_environment(
                        task.id,
                        workspace.environment_id,
                    )
                    await events.append(
                        task.id,
                        task.session_id,
                        EventType.TASK_RECOVERED,
                        {"environment_id": workspace.environment_id},
                    )

                await events.append(
                    task.id,
                    task.session_id,
                    EventType.ENVIRONMENT_READY,
                    {"environment_id": workspace.environment_id},
                )
                heartbeat = asyncio.create_task(
                    self._heartbeat_loop(task.id, workspace.environment_id),
                    name=f"heartbeat-{task.id}",
                )

                agent = await build_agent(
                    self.settings,
                    db,
                    self.bus,
                    workspace,
                    should_cancel=lambda: self._should_cancel(task.id),
                    should_pause=lambda: self._should_pause(task.id),
                )
                await events.append(task.id, task.session_id, EventType.TASK_STARTED)
                result = await agent.run(task)

                pull_requests: list[str] = []
                if task.publish_pr:
                    published = await self.publisher.publish(
                        workspace,
                        task.id,
                        task.instruction,
                        task.repositories,
                    )
                    pull_requests = [item.url for item in published]
                    await events.append(
                        task.id,
                        task.session_id,
                        EventType.PR_CREATED,
                        {"urls": pull_requests},
                    )

                result["pull_requests"] = pull_requests
                await tasks.transition(task.id, TaskStatus.COMPLETED, result=result)
                await events.append(
                    task.id,
                    task.session_id,
                    EventType.TASK_COMPLETED,
                    result,
                )
                TASKS_COMPLETED.inc()
                final_status = TaskStatus.COMPLETED
        except asyncio.CancelledError:
            async with self.session_factory() as db:
                with suppress(KeyError):
                    persisted = await TaskRepository(db).require(task_id)
                    if persisted.status in {
                        TaskStatus.CANCELLED,
                        TaskStatus.CANCELLING,
                    }:
                        final_status = TaskStatus.CANCELLED
            raise
        except Exception as exc:
            log.exception("task_execution_failed", task_id=task_id)
            async with self.session_factory() as db:
                tasks = TaskRepository(db)
                try:
                    task = await tasks.require(task_id)
                    if task.status not in {
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                        TaskStatus.COMPLETED,
                    }:
                        await tasks.transition(
                            task.id,
                            TaskStatus.FAILED,
                            error=str(exc),
                        )
                        await EventStore(db, self.bus).append(
                            task.id,
                            task.session_id,
                            EventType.TASK_FAILED,
                            {"error": str(exc)},
                        )
                        TASKS_FAILED.inc()
                    final_status = TaskStatus.FAILED
                except (KeyError, InvalidStateTransition):
                    log.exception("failed_to_persist_task_failure", task_id=task_id)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

            recoverable_interruption = final_status is None
            retained_failure = (
                final_status == TaskStatus.FAILED
                and self.settings.retain_failed_workspaces
            )
            preserve_workspace = recoverable_interruption or retained_failure

            async with self.session_factory() as db:
                await TaskLeaseRepository(db).release(task_id, self.owner_id)
                if workspace is not None:
                    await EnvironmentLeaseRepository(db).mark(
                        workspace.environment_id,
                        (
                            EnvironmentStatus.READY
                            if preserve_workspace
                            else EnvironmentStatus.DELETED
                        ),
                    )

            if workspace is not None and not preserve_workspace:
                with suppress(OSError, RuntimeError):
                    await self.environments.release(workspace)

            ACTIVE_TASKS.dec()
            TASK_DURATION.observe(time.monotonic() - started)
