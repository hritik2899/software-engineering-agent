"""FastAPI control-plane boundary.

HTTP endpoints create, read and control logical tasks while WebSocket streams the
same durable event sequence used for recovery. The API never executes repository
commands itself: execution is delegated to Orchestrator. PostgreSQL is authoritative;
Redis provides queueing and live fan-out acceleration.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from prometheus_client import make_asgi_app
from sqlalchemy import text

from minion.config import get_settings
from minion.db import SessionFactory, init_db
from minion.domain import RepositorySpec, TaskCreate, TaskView, UserInstruction
from minion.events import EventBus, EventStore
from minion.logging import configure_logging
from minion.orchestrator import Orchestrator
from minion.queue import build_queue
from minion.repositories import TaskRepository
from minion.runtime.workspace import build_environment_provider
from minion.security import require_api_token, websocket_authorized

settings = get_settings()
configure_logging(settings.log_level)
bus = EventBus(settings.redis_url)
orchestrator = Orchestrator(
    settings, build_queue(settings), build_environment_provider(settings), bus
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    await orchestrator.start()
    try:
        yield
    finally:
        await orchestrator.stop()
        await bus.close()


app = FastAPI(
    title="Minion-Style Software Engineering Agent",
    version="0.2.0",
    lifespan=lifespan,
)
app.mount("/metrics", make_asgi_app())


@app.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness() -> dict[str, str]:
    try:
        async with SessionFactory() as db:
            await db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    return {"status": "ready"}


@app.post(
    "/tasks",
    response_model=TaskView,
    status_code=202,
    dependencies=[Depends(require_api_token)],
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


@app.get(
    "/tasks/{task_id}",
    response_model=TaskView,
    dependencies=[Depends(require_api_token)],
)
async def get_task(task_id: str) -> TaskView:
    async with SessionFactory() as db:
        task = await TaskRepository(db).get(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    return task


@app.post(
    "/tasks/{task_id}/messages",
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
async def send_message(task_id: str, body: UserInstruction) -> dict[str, str]:
    try:
        await orchestrator.send_instruction(task_id, body.message)
    except KeyError:
        raise HTTPException(404, "task not found") from None
    return {"status": "accepted"}


@app.post(
    "/tasks/{task_id}/retry",
    response_model=TaskView,
    dependencies=[Depends(require_api_token)],
)
async def retry_task(task_id: str) -> TaskView:
    try:
        return await orchestrator.retry(task_id)
    except KeyError:
        raise HTTPException(404, "task not found") from None


@app.post(
    "/tasks/{task_id}/cancel",
    response_model=TaskView,
    dependencies=[Depends(require_api_token)],
)
async def cancel_task(task_id: str) -> TaskView:
    try:
        return await orchestrator.cancel(task_id)
    except KeyError:
        raise HTTPException(404, "task not found") from None


@app.get(
    "/tasks/{task_id}/events",
    dependencies=[Depends(require_api_token)],
)
async def list_events(task_id: str, after: int = 0):
    async with SessionFactory() as db:
        task = await TaskRepository(db).get(task_id)
        if not task:
            raise HTTPException(404, "task not found")
        events = await EventStore(db, bus).list_after(task_id, after)
    return [event.model_dump(mode="json") for event in events]


@app.websocket("/tasks/{task_id}/events/ws")
async def task_events(websocket: WebSocket, task_id: str, after: int = 0):
    if not websocket_authorized(websocket):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    try:
        async with SessionFactory() as db:
            if not await TaskRepository(db).get(task_id):
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
