"""LLM boundary with OpenAI and deterministic local mock providers."""
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
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelTurn: ...


class OpenAIClient:
    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for the OpenAI provider")
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.model = settings.llm_model

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
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


class MockLLMClient:
    """Deterministic provider for smoke tests and architecture demos."""

    def __init__(self, default_repo: str):
        self.turn = 0
        self.default_repo = default_repo

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelTurn:
        del messages, tools
        self.turn += 1
        repo = self.default_repo
        if self.turn == 1:
            return ModelTurn(
                "I will inspect the repository.",
                [ToolCall("mock-1", "list_files", {"repo": repo})],
            )
        if self.turn == 2:
            return ModelTurn(
                "I will create a verifiable demonstration artifact.",
                [
                    ToolCall(
                        "mock-2",
                        "write_file",
                        {
                            "repo": repo,
                            "path": "MINION_AGENT_DEMO.txt",
                            "content": "created by the Minion architecture smoke test\n",
                        },
                    )
                ],
            )
        if self.turn == 3:
            return ModelTurn(
                "I will verify the change.",
                [
                    ToolCall(
                        "mock-3",
                        "run_command",
                        {
                            "repo": repo,
                            "command": "test -f MINION_AGENT_DEMO.txt",
                        },
                    )
                ],
            )
        if self.turn == 4:
            return ModelTurn(
                "I will inspect the final diff.",
                [ToolCall("mock-4", "git_diff", {"repo": repo})],
            )
        if self.turn == 5:
            return ModelTurn(
                "I will create the durable checkpoint.",
                [
                    ToolCall(
                        "mock-5",
                        "checkpoint",
                        {
                            "repo": repo,
                            "message": "verify Minion smoke path",
                        },
                    )
                ],
            )
        return ModelTurn(
            "Verified, inspected, and checkpointed the change. Task complete.",
            [],
        )
