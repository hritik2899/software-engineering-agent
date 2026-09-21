"""Durable event log plus local/Redis live fan-out."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from minion.config import Settings
from minion.domain import AgentEvent, EventType, new_id, utcnow
from minion.models import EventRow, SessionRow


class EventBus:
    """Live transport; SQL remains the source of truth."""

    def __init__(self, settings: Settings):
        self.redis: Redis | None = (
            Redis.from_url(settings.redis_url, decode_responses=True)
            if settings.redis_url
            else None
        )
        self._subscribers: dict[str, set[asyncio.Queue[AgentEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    @staticmethod
    def _channel(task_id: str) -> str:
        return f"minion:events:{task_id}"

    async def publish_live(self, event: AgentEvent) -> None:
        if self.redis is not None:
            await self.redis.publish(
                self._channel(event.task_id),
                event.model_dump_json(),
            )
            return

        async with self._lock:
            subscribers = list(self._subscribers[event.task_id])
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue

    async def subscribe(self, task_id: str) -> AsyncIterator[AgentEvent]:
        if self.redis is not None:
            pubsub = self.redis.pubsub()
            await pubsub.subscribe(self._channel(task_id))
            try:
                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=1.0,
                    )
                    if message is None:
                        await asyncio.sleep(0)
                        continue
                    yield AgentEvent.model_validate_json(message["data"])
            finally:
                await pubsub.unsubscribe(self._channel(task_id))
                await pubsub.aclose()
            return

        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers[task_id].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers[task_id].discard(queue)

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()


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
        """Atomically allocate a session sequence, then persist the event.

        Using UPDATE ... RETURNING avoids relying on SELECT FOR UPDATE semantics,
        which SQLite does not implement the same way PostgreSQL does.
        """
        result = await self.db.execute(
            update(SessionRow)
            .where(SessionRow.id == session_id)
            .values(
                last_event_sequence=SessionRow.last_event_sequence + 1,
                updated_at=utcnow(),
            )
            .returning(SessionRow.last_event_sequence)
        )
        sequence = result.scalar_one_or_none()
        if sequence is None:
            await self.db.rollback()
            raise KeyError(session_id)

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
        await self.db.commit()
        await self.bus.publish_live(event)
        return event

    async def list_after(
        self,
        task_id: str,
        sequence: int = 0,
        limit: int = 500,
    ) -> list[AgentEvent]:
        statement = (
            select(EventRow)
            .where(EventRow.task_id == task_id, EventRow.sequence > sequence)
            .order_by(EventRow.sequence)
            .limit(limit)
        )
        rows = (await self.db.scalars(statement)).all()
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
