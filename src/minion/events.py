"""Durable event log plus live fan-out.

WebSockets are transport, not truth. Events are persisted with monotonically
increasing sequence numbers so clients can reconnect and replay missed history.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from minion.domain import AgentEvent, EventType, new_id, utcnow
from minion.models import EventRow, SessionRow


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[AgentEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish_live(self, event: AgentEvent) -> None:
        async with self._lock:
            subscribers = list(self._subscribers[event.task_id])
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow clients recover from the durable log on reconnect.
                pass

    async def subscribe(self, task_id: str) -> AsyncIterator[AgentEvent]:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers[task_id].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers[task_id].discard(queue)


class EventStore:
    def __init__(self, db: AsyncSession, bus: EventBus):
        self.db = db
        self.bus = bus

    async def append(
        self,
        task_id: str,
        session_id: str,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent:
        # Row locking serializes sequence allocation in PostgreSQL. SQLite ignores
        # FOR UPDATE but serializes writes at the database level.
        stmt = select(SessionRow).where(SessionRow.id == session_id).with_for_update()
        session = (await self.db.scalars(stmt)).one_or_none()
        if not session:
            raise KeyError(session_id)
        sequence = session.last_event_sequence + 1
        event = AgentEvent(
            id=new_id("evt"),
            task_id=task_id,
            session_id=session_id,
            sequence=sequence,
            type=event_type,
            payload=payload or {},
            created_at=utcnow(),
        )
        self.db.add(
            EventRow(
                id=event.id,
                task_id=event.task_id,
                session_id=event.session_id,
                sequence=event.sequence,
                type=event.type.value,
                payload=event.payload,
                created_at=event.created_at,
            )
        )
        await self.db.execute(
            update(SessionRow)
            .where(SessionRow.id == session_id)
            .values(last_event_sequence=sequence, updated_at=utcnow())
        )
        await self.db.commit()
        await self.bus.publish_live(event)
        return event

    async def list_after(
        self, task_id: str, sequence: int = 0, limit: int = 500
    ) -> list[AgentEvent]:
        stmt = (
            select(EventRow)
            .where(EventRow.task_id == task_id, EventRow.sequence > sequence)
            .order_by(EventRow.sequence)
            .limit(limit)
        )
        rows = (await self.db.scalars(stmt)).all()
        return [
            AgentEvent(
                id=row.id,
                task_id=row.task_id,
                session_id=row.session_id,
                sequence=row.sequence,
                type=EventType(row.type),
                payload=row.payload or {},
                created_at=row.created_at,
            )
            for row in rows
        ]
