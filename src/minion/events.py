"""Durable event log plus local/Redis live fan-out."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from minion.domain import AgentEvent, EventType, new_id, utcnow
from minion.models import EventRow, SessionRow


class EventBus:
    """Live transport.

    SQL events remain authoritative. With Redis configured, Pub/Sub lets a UI
    connected to API instance A receive events produced by worker instance B.
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis = (
            Redis.from_url(redis_url, decode_responses=True) if redis_url else None
        )
        self._subscribers: dict[str, set[asyncio.Queue[AgentEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    @staticmethod
    def _channel(task_id: str) -> str:
        return f"minion:events:{task_id}"

    async def publish_live(self, event: AgentEvent) -> None:
        if self.redis:
            await self.redis.publish(self._channel(event.task_id), event.model_dump_json())
            return

        async with self._lock:
            subscribers = list(self._subscribers[event.task_id])
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, task_id: str) -> AsyncIterator[AgentEvent]:
        if self.redis:
            pubsub = self.redis.pubsub()
            await pubsub.subscribe(self._channel(task_id))
            try:
                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                    if message and message.get("data"):
                        yield AgentEvent.model_validate_json(message["data"])
                    else:
                        await asyncio.sleep(0.05)
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
        if self.redis:
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
