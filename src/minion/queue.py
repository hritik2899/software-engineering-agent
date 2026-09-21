"""Task queue abstraction.

Redis provides multi-process delivery in production. The in-memory queue keeps the
project zero-setup for local learning. Duplicate delivery is safe because task
state transitions are optimistic/idempotent.
"""
from __future__ import annotations

import asyncio
from typing import Protocol

from redis.asyncio import Redis

from minion.config import Settings


class WorkQueue(Protocol):
    async def put(self, task_id: str) -> None: ...
    async def get(self) -> str: ...
    async def close(self) -> None: ...


class InMemoryWorkQueue:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    async def put(self, task_id: str) -> None:
        await self.queue.put(task_id)

    async def get(self) -> str:
        return await self.queue.get()

    async def close(self) -> None:
        return None


class RedisWorkQueue:
    KEY = "minion:tasks:ready"

    def __init__(self, url: str):
        self.redis = Redis.from_url(url, decode_responses=True)

    async def put(self, task_id: str) -> None:
        await self.redis.lpush(self.KEY, task_id)

    async def get(self) -> str:
        item = await self.redis.brpop(self.KEY, timeout=5)
        if item is None:
            await asyncio.sleep(0.1)
            return await self.get()
        return item[1]

    async def close(self) -> None:
        await self.redis.aclose()


def build_queue(settings: Settings) -> WorkQueue:
    return RedisWorkQueue(settings.redis_url) if settings.redis_url else InMemoryWorkQueue()
