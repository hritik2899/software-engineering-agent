"""Autonomous coding-agent loop."""
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
        schemas = [*self.tools.schemas, *self.context.control_tool_schemas]
        return await self.llm.complete(messages, schemas)

    async def run(self, task: TaskView) -> dict[str, Any]:
        for step in range(1, self.max_steps + 1):
            await self.context.maybe_compact(task)
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

            if not turn.tool_calls:
                await self.events.append(
                    task.id,
                    task.session_id,
                    EventType.AGENT_MESSAGE,
                    {
                        "content": (
                            "The model returned no tool call. Continue working and "
                            "finish explicitly with finish_task after verification."
                        ),
                        "step": step,
                    },
                )
                continue

            for call in turn.tool_calls:
                await self.events.append(
                    task.id,
                    task.session_id,
                    EventType.TOOL_STARTED,
                    {
                        "tool": call.name,
                        "arguments": call.arguments,
                        "tool_call_id": call.id,
                    },
                )

                if call.name in self.context.CONTROL_TOOLS:
                    result = await self.context.execute_control_tool(
                        task, call.name, call.arguments
                    )
                    await self.events.append(
                        task.id,
                        task.session_id,
                        EventType.TOOL_COMPLETED,
                        {
                            "tool": call.name,
                            "tool_call_id": call.id,
                            "ok": True,
                            "output": result.output,
                        },
                    )
                    if result.terminal_payload is not None:
                        return {
                            **result.terminal_payload,
                            "steps": step,
                        }
                    continue

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

                if call.name == "checkpoint" and result.ok:
                    await self.events.append(
                        task.id,
                        task.session_id,
                        EventType.CHECKPOINT_CREATED,
                        {"output": result.output},
                    )

        raise RuntimeError(f"agent exceeded max steps ({self.max_steps})")
