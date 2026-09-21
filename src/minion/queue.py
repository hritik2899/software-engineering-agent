"""At-least-once task queue abstraction.

Redis Streams provide durable delivery with acknowledgement and stale-message
reclaim. The in-memory implementation keeps local development dependency-free.
"""
from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from minion.config import Settings


@dataclass(slots=True)
class WorkItem:
    task_id: str
    receipt: str


class WorkQueue(Protocol):
    async def prepare(self) -> None: ...
    async def put(self, task_id: str) -> None: ...
    async def get(self) -> WorkItem: ...
    async def ack(self, item: WorkItem) -> None: ...
    async def close(self) -> None: ...


class InMemoryWorkQueue:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[WorkItem] = asyncio.Queue()

    async def prepare(self) -> None:
        return None

    async def put(self, task_id: str) -> None:
        await self.queue.put(WorkItem(task_id, uuid4().hex))

    async def get(self) -> WorkItem:
        return await self.queue.get()

    async def ack(self, item: WorkItem) -> None:
        del item

    async def close(self) -> None:
        return None


class RedisStreamWorkQueue:
    STREAM = "minion:tasks"
    GROUP = "minion:orchestrators"

    def __init__(self, url: str, visibility_timeout_seconds: int):
        self.redis = Redis.from_url(url, decode_responses=True)
        self.visibility_ms = visibility_timeout_seconds * 1000
        self.consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"

    async def prepare(self) -> None:
        try:
            await self.redis.xgroup_create(
                self.STREAM, self.GROUP, id="0-0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def put(self, task_id: str) -> None:
        await self.redis.xadd(self.STREAM, {"task_id": task_id})

    async def _claim_stale(self) -> WorkItem | None:
        result = await self.redis.xautoclaim(
            self.STREAM,
            self.GROUP,
            self.consumer,
            min_idle_time=self.visibility_ms,
            start_id="0-0",
            count=1,
        )
        messages = result[1] if len(result) > 1 else []
        if not messages:
            return None
        receipt, fields = messages[0]
        return WorkItem(fields["task_id"], receipt)

    async def get(self) -> WorkItem:
        while True:
            reclaimed = await self._claim_stale()
            if reclaimed:
                return reclaimed
            response = await self.redis.xreadgroup(
                self.GROUP,
                self.consumer,
                {self.STREAM: ">"},
                count=1,
                block=5000,
            )
            if not response:
                continue
            _, messages = response[0]
            receipt, fields = messages[0]
            return WorkItem(fields["task_id"], receipt)

    async def ack(self, item: WorkItem) -> None:
        await self.redis.xack(self.STREAM, self.GROUP, item.receipt)
        await self.redis.xdel(self.STREAM, item.receipt)

    async def close(self) -> None:
        await self.redis.aclose()


def build_queue(settings: Settings) -> WorkQueue:
    if settings.redis_url:
        return RedisStreamWorkQueue(
            settings.redis_url, settings.queue_visibility_timeout_seconds
        )
    return InMemoryWorkQueue()
