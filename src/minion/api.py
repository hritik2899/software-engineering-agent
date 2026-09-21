"""FastAPI control-plane API and replayable WebSocket event stream."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from minion.config import get_settings
from minion.db import SessionFactory, init_db
from minion.domain import TaskCreate, TaskView, UserInstruction
from minion.events import EventBus, EventStore
from minion.logging import configure_logging
from minion.orchestrator import Orchestrator
from minion.queue import build_queue
from minion.repositories import TaskRepository
from minion.runtime.workspace import build_environment_provider

settings = get_settings()
configure_logging(settings.log_level)
bus = EventBus()
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


app = FastAPI(
    title="Minion-Style Software Engineering Agent",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tasks", response_model=TaskView, status_code=202)
async def create_task(request: TaskCreate) -> TaskView:
    if not request.repositories:
        request.repositories = [
            {"url": settings.default_repo_url, "base_branch": settings.default_base_branch}
        ]
    return await orchestrator.submit(request)


@app.get("/tasks/{task_id}", response_model=TaskView)
async def get_task(task_id: str) -> TaskView:
    async with SessionFactory() as db:
        task = await TaskRepository(db).get(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    return task


@app.post("/tasks/{task_id}/messages", status_code=202)
async def send_message(task_id: str, body: UserInstruction) -> dict[str, str]:
    try:
        await orchestrator.send_instruction(task_id, body.message)
    except KeyError:
        raise HTTPException(404, "task not found")
    return {"status": "accepted"}


@app.post("/tasks/{task_id}/cancel", response_model=TaskView)
async def cancel_task(task_id: str) -> TaskView:
    try:
        return await orchestrator.cancel(task_id)
    except KeyError:
        raise HTTPException(404, "task not found")


@app.get("/tasks/{task_id}/events")
async def list_events(task_id: str, after: int = 0):
    async with SessionFactory() as db:
        task = await TaskRepository(db).get(task_id)
        if not task:
            raise HTTPException(404, "task not found")
        events = await EventStore(db, bus).list_after(task_id, after)
    return [event.model_dump(mode="json") for event in events]


@app.websocket("/tasks/{task_id}/events/ws")
async def task_events(websocket: WebSocket, task_id: str, after: int = 0):
    await websocket.accept()
    try:
        # Replay first, then join live stream. Sequence numbers let clients dedupe
        # an event that races between replay and subscription.
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
