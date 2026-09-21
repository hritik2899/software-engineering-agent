"""Best-effort distributed metadata cache.

Durable SQL/event state is authoritative. Redis only accelerates hot lookups and
heartbeats; deleting the entire cache must not destroy a task.
"""
from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis

from minion.config import Settings


class MetadataCache:
    def __init__(self, settings: Settings):
        self.redis: Redis | None = (
            Redis.from_url(settings.redis_url, decode_responses=True)
            if settings.redis_url
            else None
        )
        self.local: dict[str, str] = {}

    async def get_json(self, key: str) -> dict[str, Any] | None:
        raw = await self.redis.get(key) if self.redis else self.local.get(key)
        return json.loads(raw) if raw else None

    async def set_json(self, key: str, value: dict[str, Any], ttl: int = 300) -> None:
        raw = json.dumps(value)
        if self.redis:
            await self.redis.set(key, raw, ex=ttl)
        else:
            self.local[key] = raw

    async def delete(self, key: str) -> None:
        if self.redis:
            await self.redis.delete(key)
        else:
            self.local.pop(key, None)

    async def close(self) -> None:
        if self.redis:
            await self.redis.aclose()
