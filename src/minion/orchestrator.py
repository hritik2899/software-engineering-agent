"""Distributed task orchestration.

The orchestrator owns WHEN/WHERE an agent runs, not HOW code is changed. Durable
state + optimistic transitions make duplicate queue delivery safe. Startup
reconciliation requeues interrupted work and tries to attach to an existing
environment before provisioning a replacement.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress

from minion.auth import AuthorizationPolicy
from minion.config import Settings
from minion.db import SessionFactory
from minion.domain import EventType, TaskCreate, TaskStatus, TaskView
from minion.events import EventBus, EventStore
from minion.github import GitHubPublisher
from minion.logging import logger
from minion.queue import WorkQueue
from minion.repositories import TaskRepository
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
    ):
        self.settings = settings
        self.queue = queue
        self.environments = environments
        self.bus = bus
        self.auth = AuthorizationPolicy()
        self.publisher = GitHubPublisher(settings)
        self._worker: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        await self.environments.prepare()
        await self.reconcile()
        self._worker = asyncio.create_task(self._worker_loop(), name="minion-orchestrator")

    async def stop(self) -> None:
        self._stopping.set()
        if self._worker:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
        for running in list(self._running.values()):
            running.cancel()
        await self.queue.close()

    async def submit(self, request: TaskCreate) -> TaskView:
        await self.auth.authorize_repositories(request.user_id, request.repositories)
        async with SessionFactory() as db:
            tasks, events = TaskRepository(db), EventStore(db, self.bus)
            task = await tasks.create(request)
            await events.append(task.id, task.session_id, EventType.TASK_CREATED, {
                "instruction": task.instruction,
                "repositories": [r.model_dump() for r in task.repositories],
            })
            task = await tasks.transition(task.id, TaskStatus.QUEUED)
            await events.append(task.id, task.session_id, EventType.TASK_QUEUED)
        await self.queue.put(task.id)
        return task

    async def send_instruction(self, task_id: str, message: str) -> None:
        async with SessionFactory() as db:
            task = await TaskRepository(db).require(task_id)
            await EventStore(db, self.bus).append(
                task.id, task.session_id, EventType.USER_MESSAGE, {"message": message}
            )

    async def cancel(self, task_id: str) -> TaskView:
        async with SessionFactory() as db:
            tasks = TaskRepository(db)
            task = await tasks.require(task_id)
            if task.status in {TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.FAILED}:
                return task
            task = await tasks.transition(task.id, TaskStatus.CANCELLING)
            running = self._running.get(task_id)
            if running:
                running.cancel()
            task = await tasks.transition(task.id, TaskStatus.CANCELLED)
            await EventStore(db, self.bus).append(
                task.id, task.session_id, EventType.TASK_CANCELLED
            )
            return task

    async def reconcile(self) -> None:
        """Requeue unfinished tasks after a control-plane restart."""
        async with SessionFactory() as db:
            active = await TaskRepository(db).list_active()
        for task in active:
            if task.status == TaskStatus.CANCELLING:
                await self.cancel(task.id)
            else:
                # _execute() understands RUNNING/PROVISIONING recovery.
                await self.queue.put(task.id)

    async def _worker_loop(self) -> None:
        while not self._stopping.is_set():
            task_id = await self.queue.get()
            if task_id in self._running:
                continue
            execution = asyncio.create_task(self._execute(task_id), name=f"task-{task_id}")
            self._running[task_id] = execution
            execution.add_done_callback(lambda _f, tid=task_id: self._running.pop(tid, None))

    async def _execute(self, task_id: str) -> None:
        workspace: Workspace | None = None
        try:
            async with SessionFactory() as db:
                tasks = TaskRepository(db)
                task = await tasks.require(task_id)
                if task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
                    return

                # Normal delivery starts QUEUED -> PROVISIONING. Recovery may find
                # a task that had already reached PROVISIONING/RUNNING.
                if task.status == TaskStatus.QUEUED:
                    task = await tasks.transition(task.id, TaskStatus.PROVISIONING)

                if task.environment_id:
                    workspace = await self.environments.attach(
                        task.environment_id, task.repositories
                    )

                if workspace is None:
                    workspace = await self.environments.allocate(task.id, task.repositories)
                    await EventStore(db, self.bus).append(
                        task.id, task.session_id, EventType.ENVIRONMENT_ALLOCATED,
                        {"environment_id": workspace.environment_id}
                    )

                if task.status == TaskStatus.PROVISIONING:
                    task = await tasks.transition(
                        task.id, TaskStatus.RUNNING, environment_id=workspace.environment_id
                    )
                elif task.status == TaskStatus.RUNNING and task.environment_id != workspace.environment_id:
                    # Environment replacement after execution-plane failure.
                    # The logical task/session remain unchanged.
                    row = await db.get(__import__("minion.models", fromlist=["TaskRow"]).TaskRow, task.id)
                    row.environment_id = workspace.environment_id
                    await db.commit()
                    task = await tasks.require(task.id)

                await EventStore(db, self.bus).append(
                    task.id, task.session_id, EventType.ENVIRONMENT_READY,
                    {"environment_id": workspace.environment_id}
                )
                agent = build_agent(self.settings, db, self.bus, workspace)
                await EventStore(db, self.bus).append(
                    task.id, task.session_id, EventType.TASK_STARTED
                )
                result = await agent.run(task)

                pr_urls: list[str] = []
                if task.publish_pr:
                    prs = await self.publisher.publish(
                        workspace, task.id, task.instruction, task.repositories
                    )
                    pr_urls = [pr.url for pr in prs]
                    await EventStore(db, self.bus).append(
                        task.id, task.session_id, EventType.PR_CREATED, {"urls": pr_urls}
                    )
                result["pull_requests"] = pr_urls
                await tasks.transition(task.id, TaskStatus.COMPLETED, result=result)
                await EventStore(db, self.bus).append(
                    task.id, task.session_id, EventType.TASK_COMPLETED, result
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("task_execution_failed", task_id=task_id)
            async with SessionFactory() as db:
                tasks = TaskRepository(db)
                try:
                    task = await tasks.require(task_id)
                    if task.status not in {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETED}:
                        await tasks.transition(task.id, TaskStatus.FAILED, error=str(exc))
                        await EventStore(db, self.bus).append(
                            task.id, task.session_id, EventType.TASK_FAILED,
                            {"error": str(exc)}
                        )
                except Exception:
                    log.exception("failed_to_persist_task_failure", task_id=task_id)
        finally:
            # Completed/failed task workspaces are disposable; Git checkpoints/PRs
            # are the durable code boundary.
            if workspace:
                with suppress(Exception):
                    await self.environments.release(workspace)
