"""LLM boundary.

The agent depends on this protocol rather than a vendor SDK. Tests can inject a
scripted model, and production can swap providers without changing orchestration.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from openai import AsyncOpenAI

from minion.config import Settings


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ModelTurn:
    content: str
    tool_calls: list[ToolCall]


class LLMClient(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn: ...


class OpenAIClient:
    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for the OpenAI provider")
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.model = settings.llm_model

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        message = response.choices[0].message
        calls = [
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=json.loads(call.function.arguments or "{}"),
            )
            for call in (message.tool_calls or [])
        ]
        return ModelTurn(content=message.content or "", tool_calls=calls)
