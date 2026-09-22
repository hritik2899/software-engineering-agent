"""Distributed task orchestration."""
from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import Any

from minion.auth import AuthorizationPolicy
from minion.config import Settings
from minion.db import SessionFactory
from minion.domain import (
    EnvironmentStatus,
    EventType,
    TaskCreate,
    TaskStatus,
    TaskView,
)
from minion.events import EventBus, EventStore
from minion.github import GitHubPublisher
from minion.logging import logger
from minion.metrics import (
    ENVIRONMENT_ALLOCATIONS,
    TASK_DURATION,
    TASKS_COMPLETED,
    TASKS_FAILED,
    TASKS_RUNNING,
    TASKS_SUBMITTED,
)
from minion.queue import WorkQueue
from minion.repositories import (
    EnvironmentRepository,
    SessionRepository,
    TaskRepository,
)
from minion.runtime.code_index import RepositoryContextIndex
from minion.runtime.factory import build_agent
from minion.runtime.workspace import EnvironmentProvider, Workspace

log = logger(__name__)


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        queue: WorkQueue,
        environments: EnvironmentProvider,
        bus: EventBus,
        *,
        session_factory: Any = SessionFactory,
        agent_factory: Any = build_agent,
    ):
        self.settings = settings
        self.queue = queue
        self.environments = environments
        self.bus = bus
        self.session_factory = session_factory
        self.agent_factory = agent_factory
        self.auth = AuthorizationPolicy(settings)
        self.publisher = GitHubPublisher(settings)
        # Derived repository intelligence is shared across tasks through cache_root.
        self.repo_index = RepositoryContextIndex(settings.cache_root)
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[str, asyncio.Task[None]] = {}
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        await self.queue.prepare()
        await self.environments.prepare()
        await self.reconcile()
        self._workers = [
            asyncio.create_task(self._worker_loop(i), name=f"minion-worker-{i}")
            for i in range(max(1, self.settings.worker_concurrency))
        ]

    async def stop(self) -> None:
        self._stopping.set()
        for running in list(self._running.values()):
            running.cancel()
        for worker in self._workers:
            worker.cancel()
        for running in list(self._running.values()):
            with suppress(asyncio.CancelledError):
                await running
        for worker in self._workers:
            with suppress(asyncio.CancelledError):
                await worker
        await self.environments.shutdown()
        await self.queue.close()

    async def submit(self, request: TaskCreate) -> TaskView:
        await self.auth.authorize_repositories(request.user_id, request.repositories)
        async with self.session_factory() as db:
            tasks, events = TaskRepository(db), EventStore(db, self.bus)
            task = await tasks.create(request)
            await events.append(
                task.id,
                task.session_id,
                EventType.TASK_CREATED,
                {
                    "instruction": task.instruction,
                    "repositories": [r.model_dump() for r in task.repositories],
                },
            )
            task = await tasks.transition(task.id, TaskStatus.QUEUED)
            await events.append(task.id, task.session_id, EventType.TASK_QUEUED)
        await self.queue.put(task.id)
        TASKS_SUBMITTED.inc()
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

    async def retry(self, task_id: str) -> TaskView:
        async with self.session_factory() as db:
            tasks = TaskRepository(db)
            task = await tasks.require(task_id)
            if task.status != TaskStatus.FAILED:
                return task
            task = await tasks.transition(task.id, TaskStatus.QUEUED)
            await EventStore(db, self.bus).append(
                task.id, task.session_id, EventType.TASK_QUEUED, {"retry": True}
            )
        await self.queue.put(task.id)
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
            task = await tasks.transition(task.id, TaskStatus.CANCELLING)
            running = self._running.get(task_id)
            if running:
                running.cancel()
            else:
                task = await tasks.transition(task.id, TaskStatus.CANCELLED)
                await EventStore(db, self.bus).append(
                    task.id, task.session_id, EventType.TASK_CANCELLED
                )
            return task

    async def reconcile(self) -> None:
        async with self.session_factory() as db:
            active = await TaskRepository(db).list_active()
        for task in active:
            if task.status == TaskStatus.CANCELLING:
                await self.cancel(task.id)
            else:
                await self.queue.put(task.id)

    async def _worker_loop(self, worker_id: int) -> None:
        del worker_id
        while not self._stopping.is_set():
            item = await self.queue.get()
            if item.task_id in self._running:
                await self.queue.ack(item)
                continue

            execution = asyncio.create_task(
                self._execute(item.task_id), name=f"task-{item.task_id}"
            )
            self._running[item.task_id] = execution
            try:
                await execution
            except asyncio.CancelledError:
                if self._stopping.is_set():
                    raise
                # User cancellation: state transition is handled inside _execute.
                await self.queue.ack(item)
            else:
                await self.queue.ack(item)
            finally:
                self._running.pop(item.task_id, None)

    async def _preindex_workspace(
        self,
        task: TaskView,
        workspace: Workspace,
        events: EventStore,
    ) -> None:
        """Pre-index repositories before the first model turn.

        Indexing is an accelerator, not authoritative state. A damaged cache or a
        parser miss must not make an otherwise runnable engineering task fail.
        """
        async def index_one(name: str, path):
            try:
                indexed = await self.repo_index.ensure_index(path)
                await events.append(
                    task.id,
                    task.session_id,
                    EventType.REPOSITORY_INDEXED,
                    {
                        "repository": name,
                        "head": indexed.head,
                        "parsed_files": indexed.parsed_files,
                        "reused_files": indexed.reused_files,
                        "status": "ready",
                    },
                )
            except Exception as exc:
                log.warning(
                    "repository_preindex_failed",
                    task_id=task.id,
                    repository=name,
                    error=str(exc),
                )
                await events.append(
                    task.id,
                    task.session_id,
                    EventType.REPOSITORY_INDEXED,
                    {
                        "repository": name,
                        "status": "degraded",
                        "error": str(exc),
                    },
                )

        await asyncio.gather(
            *(
                index_one(name, path)
                for name, path in workspace.repositories.items()
            )
        )

    async def _heartbeat(self, environment_id: str) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_interval_seconds)
            async with self.session_factory() as db:
                await EnvironmentRepository(db).heartbeat(environment_id)

    async def _execute(self, task_id: str) -> None:
        workspace: Workspace | None = None
        heartbeat: asyncio.Task[None] | None = None
        started = time.monotonic()
        TASKS_RUNNING.inc()

        try:
            async with self.session_factory() as db:
                tasks = TaskRepository(db)
                task = await tasks.require(task_id)
                if task.status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.CANCELLED,
                    TaskStatus.FAILED,
                }:
                    return

                if task.status == TaskStatus.QUEUED:
                    task = await tasks.transition(task.id, TaskStatus.PROVISIONING)

                if task.environment_id:
                    workspace = await self.environments.attach(
                        task.environment_id, task.repositories
                    )

                if workspace is None:
                    workspace = await self.environments.allocate(
                        task.id, task.repositories
                    )
                    ENVIRONMENT_ALLOCATIONS.labels(
                        provider=type(self.environments).__name__
                    ).inc()
                    await EventStore(db, self.bus).append(
                        task.id,
                        task.session_id,
                        EventType.ENVIRONMENT_ALLOCATED,
                        {"environment_id": workspace.environment_id},
                    )

                if task.status == TaskStatus.PROVISIONING:
                    task = await tasks.transition(
                        task.id,
                        TaskStatus.RUNNING,
                        environment_id=workspace.environment_id,
                    )
                elif (
                    task.status == TaskStatus.RUNNING
                    and task.environment_id != workspace.environment_id
                ):
                    task = await tasks.replace_environment(
                        task.id, workspace.environment_id
                    )

                env_repo = EnvironmentRepository(db)
                await env_repo.upsert(
                    environment_id=workspace.environment_id,
                    task_id=task.id,
                    provider=type(self.environments).__name__,
                    workspace_path=str(workspace.root),
                    status=EnvironmentStatus.BUSY,
                    metadata={"container_name": workspace.container_name},
                )
                await EventStore(db, self.bus).append(
                    task.id,
                    task.session_id,
                    EventType.ENVIRONMENT_READY,
                    {"environment_id": workspace.environment_id},
                )

                heartbeat = asyncio.create_task(
                    self._heartbeat(workspace.environment_id),
                    name=f"heartbeat-{workspace.environment_id}",
                )

                events = EventStore(db, self.bus)
                await self._preindex_workspace(task, workspace, events)

                agent = self.agent_factory(
                    self.settings, db, self.bus, workspace
                )
                await EventStore(db, self.bus).append(
                    task.id, task.session_id, EventType.TASK_STARTED
                )
                result = await agent.run(task)

                pr_urls: list[str] = []
                if task.publish_pr:
                    prs = await self.publisher.publish(
                        workspace,
                        task.id,
                        task.instruction,
                        task.repositories,
                    )
                    pr_urls = [pr.url for pr in prs]
                    await EventStore(db, self.bus).append(
                        task.id,
                        task.session_id,
                        EventType.PR_CREATED,
                        {"urls": pr_urls},
                    )

                result["pull_requests"] = pr_urls
                await tasks.transition(
                    task.id, TaskStatus.COMPLETED, result=result
                )
                await env_repo.mark(
                    workspace.environment_id, EnvironmentStatus.STOPPED
                )
                await EventStore(db, self.bus).append(
                    task.id,
                    task.session_id,
                    EventType.TASK_COMPLETED,
                    result,
                )
                TASKS_COMPLETED.inc()

        except asyncio.CancelledError:
            if self._stopping.is_set():
                raise
            async with self.session_factory() as db:
                tasks = TaskRepository(db)
                task = await tasks.require(task_id)
                if task.status == TaskStatus.CANCELLING:
                    task = await tasks.transition(task.id, TaskStatus.CANCELLED)
                    await EventStore(db, self.bus).append(
                        task.id, task.session_id, EventType.TASK_CANCELLED
                    )
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
                            task.id, TaskStatus.FAILED, error=str(exc)
                        )
                        if workspace:
                            await EnvironmentRepository(db).mark(
                                workspace.environment_id,
                                EnvironmentStatus.READY,
                            )
                        await EventStore(db, self.bus).append(
                            task.id,
                            task.session_id,
                            EventType.TASK_FAILED,
                            {"error": str(exc)},
                        )
                        TASKS_FAILED.inc()
                except (KeyError, RuntimeError):
                    log.exception(
                        "failed_to_persist_task_failure", task_id=task_id
                    )

        finally:
            if heartbeat:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

            if workspace:
                destroy = False
                async with self.session_factory() as db:
                    final = await TaskRepository(db).get(task_id)
                    destroy = bool(
                        final
                        and final.status
                        in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
                    )
                with suppress(Exception):
                    await self.environments.release(
                        workspace, destroy_workspace=destroy
                    )

            TASKS_RUNNING.dec()
            TASK_DURATION.observe(time.monotonic() - started)
