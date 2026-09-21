"""Autonomous coding-agent loop.

The Agent decides *what to do next*. ToolRegistry/Workspace perform the action.
The surrounding runtime owns persistence, context, retries and observability.
"""
from __future__ import annotations

from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from minion.domain import EventType, TaskView
from minion.events import EventStore
from minion.runtime.context import ContextManager
from minion.runtime.llm import LLMClient, ModelTurn
from minion.runtime.tools import ToolRegistry


class CodingAgent:
    def __init__(
        self,
        *,
        llm: LLMClient,
        tools: ToolRegistry,
        context: ContextManager,
        events: EventStore,
        max_steps: int,
    ):
        self.llm = llm
        self.tools = tools
        self.context = context
        self.events = events
        self.max_steps = max_steps

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    async def _model_turn(self, messages: list[dict[str, Any]]) -> ModelTurn:
        return await self.llm.complete(messages, self.tools.schemas)

    async def run(self, task: TaskView) -> dict[str, Any]:
        for step in range(1, self.max_steps + 1):
            await self.events.append(
                task.id, task.session_id, EventType.AGENT_STEP, {"step": step}
            )
            messages = await self.context.build_messages(task)
            turn = await self._model_turn(messages)

            if turn.content:
                await self.events.append(
                    task.id,
                    task.session_id,
                    EventType.AGENT_MESSAGE,
                    {"content": turn.content, "step": step},
                )

            # No tool call means the model is declaring the engineering task done.
            if not turn.tool_calls:
                return {"summary": turn.content, "steps": step}

            for call in turn.tool_calls:
                await self.events.append(
                    task.id,
                    task.session_id,
                    EventType.TOOL_STARTED,
                    {"tool": call.name, "arguments": call.arguments, "tool_call_id": call.id},
                )
                result = await self.tools.execute(call.name, call.arguments)
                await self.events.append(
                    task.id,
                    task.session_id,
                    EventType.TOOL_COMPLETED if result.ok else EventType.TOOL_FAILED,
                    {
                        "tool": call.name,
                        "tool_call_id": call.id,
                        "ok": result.ok,
                        "output": result.output,
                    },
                )

        raise RuntimeError(f"agent exceeded max steps ({self.max_steps})")
