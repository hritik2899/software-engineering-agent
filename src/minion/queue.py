"""At-least-once task queue abstraction.

Redis Streams are used instead of a destructive list pop. A task is acknowledged
only after a distributed task lease has been acquired. Duplicate delivery is safe
because the database lease is the execution ownership boundary.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from minion.config import Settings


@dataclass(slots=True)
class QueueMessage:
    task_id: str
    receipt: str | None = None


class WorkQueue(Protocol):
    async def prepare(self) -> None: ...
    async def put(self, task_id: str) -> None: ...
    async def get(self) -> QueueMessage: ...
    async def ack(self, message: QueueMessage) -> None: ...
    async def close(self) -> None: ...


class InMemoryWorkQueue:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[QueueMessage] = asyncio.Queue()

    async def prepare(self) -> None:
        return None

    async def put(self, task_id: str) -> None:
        await self.queue.put(QueueMessage(task_id))

    async def get(self) -> QueueMessage:
        return await self.queue.get()

    async def ack(self, message: QueueMessage) -> None:
        del message

    async def close(self) -> None:
        return None


class RedisStreamWorkQueue:
    STREAM = "minion:tasks"
    GROUP = "minion:orchestrators"

    def __init__(self, url: str):
        self.redis = Redis.from_url(url, decode_responses=True)
        self.consumer = f"worker-{uuid4().hex[:12]}"

    async def prepare(self) -> None:
        try:
            await self.redis.xgroup_create(
                self.STREAM,
                self.GROUP,
                id="0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def put(self, task_id: str) -> None:
        await self.redis.xadd(self.STREAM, {"task_id": task_id})

    async def get(self) -> QueueMessage:
        while True:
            rows = await self.redis.xreadgroup(
                self.GROUP,
                self.consumer,
                {self.STREAM: ">"},
                count=1,
                block=5000,
            )
            if not rows:
                continue
            _stream, messages = rows[0]
            receipt, fields = messages[0]
            return QueueMessage(task_id=fields["task_id"], receipt=receipt)

    async def ack(self, message: QueueMessage) -> None:
        if message.receipt is None:
            return
        await self.redis.xack(self.STREAM, self.GROUP, message.receipt)
        await self.redis.xdel(self.STREAM, message.receipt)

    async def close(self) -> None:
        await self.redis.aclose()


def build_queue(settings: Settings) -> WorkQueue:
    if settings.redis_url:
        return RedisStreamWorkQueue(settings.redis_url)
    return InMemoryWorkQueue()
