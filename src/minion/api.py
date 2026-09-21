"""FastAPI control-plane API and replayable WebSocket event stream."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from minion.config import Settings, get_settings
from minion.db import Database
from minion.domain import RepositorySpec, TaskCreate, TaskView, UserInstruction
from minion.errors import InvalidStateTransition
from minion.events import EventBus, EventStore
from minion.logging import configure_logging
from minion.orchestrator import Orchestrator
from minion.queue import build_queue
from minion.repositories import TaskRepository
from minion.runtime.workspace import build_environment_provider


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_directories()
    configure_logging(settings.log_level)

    database = Database(settings.database_url)
    bus = EventBus(settings)
    orchestrator = Orchestrator(
        settings,
        database.session_factory,
        build_queue(settings),
        build_environment_provider(settings),
        bus,
    )

    async def authenticate(
        x_minion_api_key: str | None = Header(default=None),
    ) -> None:
        if settings.api_key and x_minion_api_key != settings.api_key:
            raise HTTPException(status_code=401, detail="invalid API key")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await database.initialize()
        await orchestrator.start()
        try:
            yield
        finally:
            await orchestrator.stop()
            await bus.close()
            await database.close()

    application = FastAPI(
        title="Minion-Style Software Engineering Agent",
        version="0.2.0",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.database = database
    application.state.bus = bus
    application.state.orchestrator = orchestrator

    @application.middleware("http")
    async def request_timing(request: Request, call_next):
        started = time.monotonic()
        response = await call_next(request)
        response.headers["x-process-time-ms"] = (
            f"{(time.monotonic() - started) * 1000:.2f}"
        )
        return response

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready")
    async def ready() -> dict[str, str]:
        if not await database.ready():
            raise HTTPException(status_code=503, detail="database unavailable")
        return {"status": "ready"}

    @application.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            generate_latest().decode(),
            media_type=CONTENT_TYPE_LATEST,
        )

    @application.post(
        "/tasks",
        response_model=TaskView,
        status_code=202,
        dependencies=[Depends(authenticate)],
    )
    async def create_task(request: TaskCreate) -> TaskView:
        if not request.repositories:
            request.repositories = [
                RepositorySpec(
                    url=settings.default_repo_url,
                    base_branch=settings.default_base_branch,
                )
            ]
        return await orchestrator.submit(request)

    @application.get(
        "/tasks/{task_id}",
        response_model=TaskView,
        dependencies=[Depends(authenticate)],
    )
    async def get_task(task_id: str) -> TaskView:
        async with database.session_factory() as db:
            task = await TaskRepository(db).get(task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        return task

    @application.post(
        "/tasks/{task_id}/messages",
        status_code=202,
        dependencies=[Depends(authenticate)],
    )
    async def send_message(
        task_id: str,
        body: UserInstruction,
    ) -> dict[str, str]:
        try:
            await orchestrator.send_instruction(task_id, body.message)
        except KeyError:
            raise HTTPException(404, "task not found") from None
        return {"status": "accepted"}

    async def _control(
        operation,
        task_id: str,
    ) -> TaskView:
        try:
            return await operation(task_id)
        except KeyError:
            raise HTTPException(404, "task not found") from None
        except InvalidStateTransition as exc:
            raise HTTPException(409, str(exc)) from exc

    @application.post(
        "/tasks/{task_id}/pause",
        response_model=TaskView,
        dependencies=[Depends(authenticate)],
    )
    async def pause_task(task_id: str) -> TaskView:
        return await _control(orchestrator.pause, task_id)

    @application.post(
        "/tasks/{task_id}/resume",
        response_model=TaskView,
        dependencies=[Depends(authenticate)],
    )
    async def resume_task(task_id: str) -> TaskView:
        return await _control(orchestrator.resume, task_id)

    @application.post(
        "/tasks/{task_id}/cancel",
        response_model=TaskView,
        dependencies=[Depends(authenticate)],
    )
    async def cancel_task(task_id: str) -> TaskView:
        return await _control(orchestrator.cancel, task_id)

    @application.post(
        "/tasks/{task_id}/retry",
        response_model=TaskView,
        dependencies=[Depends(authenticate)],
    )
    async def retry_task(task_id: str) -> TaskView:
        return await _control(orchestrator.retry, task_id)

    @application.get(
        "/tasks/{task_id}/events",
        dependencies=[Depends(authenticate)],
    )
    async def list_events(task_id: str, after: int = 0):
        async with database.session_factory() as db:
            task = await TaskRepository(db).get(task_id)
            if task is None:
                raise HTTPException(404, "task not found")
            events = await EventStore(db, bus).list_after(task_id, after)
        return [event.model_dump(mode="json") for event in events]

    @application.websocket("/tasks/{task_id}/events/ws")
    async def task_events(
        websocket: WebSocket,
        task_id: str,
        after: int = 0,
    ):
        if (
            settings.api_key
            and websocket.headers.get("x-minion-api-key") != settings.api_key
        ):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            async with database.session_factory() as db:
                if await TaskRepository(db).get(task_id) is None:
                    await websocket.close(code=4404)
                    return
                replay = await EventStore(db, bus).list_after(task_id, after)

            last = after
            for event in replay:
                await websocket.send_json(event.model_dump(mode="json"))
                last = max(last, event.sequence)

            async for event in bus.subscribe(task_id):
                if event.sequence <= last:
                    continue
                await websocket.send_json(event.model_dump(mode="json"))
                last = event.sequence
        except WebSocketDisconnect:
            return

    return application


app = create_app()
